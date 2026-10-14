import json
import logging
import time

from django.conf import settings
from django.utils import timezone
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    ToolCallLimitMiddleware,
    before_model,
    wrap_model_call,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from .checkpoint import open_checkpointer
from .errors import retryable_model_error
from .prompts import SYSTEM_PROMPT, WORKER_COMPLAINT_PROMPT, WORKER_PROMPT
from .schemas import AgentAnswer, AgentContext
from .tools import CUSTOMER_TOOLS
from .worker_tools import WORKER_COMPLAINT_ENABLED, WORKER_TOOLS
from ..exceptions import AgentUnavailable

logger = logging.getLogger(__name__)  # MỚI

@before_model
def enforce_deadline(state: AgentState, runtime: Runtime[AgentContext]):
    if time.monotonic() > runtime.context.deadline:
        raise TimeoutError('Chatbot run deadline exceeded')
    return None

@wrap_model_call
def enforce_model_budget(request, handler):
    remaining = request.runtime.context.deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Chatbot run deadline exceeded')
    return handler(request.override(model_settings={
        **request.model_settings,
        'timeout': min(settings.CHATBOT_MODEL_TIMEOUT, remaining),
    }))

def _build_agent(model, checkpointer, *, tools, prompt, name):
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=prompt + '\nThời điểm hiện tại: ' + timezone.localtime().isoformat(),
        context_schema=AgentContext,
        checkpointer=checkpointer,
        middleware=[
            enforce_deadline,
            ModelCallLimitMiddleware(
                run_limit=settings.CHATBOT_MAX_MODEL_CALLS,
                exit_behavior='error',
            ),
            ToolCallLimitMiddleware(
                run_limit=settings.CHATBOT_MAX_TOOL_CALLS,
                exit_behavior='error',
            ),
            ModelRetryMiddleware(
                max_retries=1,
                retry_on=retryable_model_error,
                initial_delay=1,
                max_delay=2,
                on_failure='error',
            ),
            enforce_model_budget,
        ],
        name=name,
    )

def create_customer_agent(model, checkpointer):
    return _build_agent(
        model,
        checkpointer,
        tools=CUSTOMER_TOOLS,
        prompt=SYSTEM_PROMPT,
        name='cleanwise_customer_assistant',
    )

def create_worker_agent(model, checkpointer):
    prompt = WORKER_PROMPT + (
        WORKER_COMPLAINT_PROMPT if WORKER_COMPLAINT_ENABLED else ''
    )
    return _build_agent(
        model,
        checkpointer,
        tools=WORKER_TOOLS,
        prompt=prompt,
        name='cleanwise_worker_assistant',
    )

def extract_answer(result):
    messages = result['messages']
    final = messages[-1]

    if not isinstance(final, AIMessage) or final.tool_calls:
        raise ValueError('Agent did not produce a final answer')

    content = final.content

    if isinstance(content, list):
        content = '\n'.join(
            block.get('text', '')
            for block in content
            if isinstance(block, dict) and block.get('type') == 'text'
        )

    if not isinstance(content, str) or not content.strip() or len(content) > 12000:
        # MỚI: log lý do để biết model trả rỗng vì MAX_TOKENS, SAFETY hay ngẫu nhiên.
        logger.warning(
            'Empty/invalid agent content: finish_reason=%s usage=%s content=%r',
            (final.response_metadata or {}).get('finish_reason'),
            final.usage_metadata,
            content if not isinstance(content, str) else content[:200],
        )
        raise ValueError('Agent returned invalid content')

    candidates = {}
    detail_references = []
    mentioned_references = []
    selected_references = None
    source_references = []
    address_map_references = []
    usage = {
        'input_tokens': 0,
        'output_tokens': 0,
        'total_tokens': 0,
        'model_calls': 0,
        'tool_calls': 0,
    }

    # Count only messages generated after the current user input, excluding
    # committed transcript pairs that are replayed as context.
    current_start = max(
        i for i, message in enumerate(messages)
        if isinstance(message, HumanMessage)
    ) + 1

    if settings.DEBUG:
        for message in messages[current_start:]:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    logger.warning('[TRACE] tool_call %s args=%s', call['name'],
                                   json.dumps(call['args'], ensure_ascii=False)[:600])
            elif isinstance(message, ToolMessage):
                logger.warning('[TRACE] tool_result %s: %s', message.name, str(message.content)[:600])
        logger.warning('[TRACE] final_answer: %s', content[:500])

    for message in messages[current_start:]:
        if isinstance(message, AIMessage):
            usage['model_calls'] += 1
            for key in ('input_tokens', 'output_tokens', 'total_tokens'):
                usage[key] += int((message.usage_metadata or {}).get(key, 0))

        if isinstance(message, ToolMessage):
            usage['tool_calls'] += 1
            artifact = message.artifact or {}

            if isinstance(artifact, dict):
                for ref in artifact.get('cards', []):
                    if (
                        isinstance(ref, dict)
                        and ref.get('type') == 'address_map'
                    ):
                        if not address_map_references:
                            address_map_references.append({
                                'type': 'address_map'
                            })
                        continue

                    if (
                        isinstance(ref, dict)
                        and ref.get('type') in (
                            'service',
                            'booking',
                            'schedule',
                            'help',
                        )
                        and type(ref.get('id')) is int
                    ):
                        candidates[(ref['type'], ref['id'])] = ref

                        if (
                            ref['type'] == 'help'
                            and ref not in source_references
                        ):
                            source_references.append(ref)

                if message.name in (
                    'get_my_booking_detail',
                    'get_service_details',
                    'get_my_booking_schedules',
                ):
                    detail_references = artifact.get('cards', [])

                if message.name == 'list_my_bookings':
                    try:
                        data = json.loads(message.content)
                        for item in data.get('results', []):
                            if (
                                item.get('booking_code')
                                and item['booking_code'].casefold()
                                in content.casefold()
                            ):
                                mentioned_references.append({
                                    'type': 'booking',
                                    'id': item['id'],
                                })
                    except (ValueError, TypeError, AttributeError):
                        pass

                if (
                    message.name == 'select_response_cards'
                    and 'selected_cards' in artifact
                ):
                    # The model can only select entities actually read in this turn.
                    selected_references = []

                    for ref in artifact['selected_cards']:
                        if (
                            not isinstance(ref, dict)
                            or type(ref.get('id')) is not int
                        ):
                            continue

                        key = (ref.get('type'), ref['id'])

                        if (
                            key in candidates
                            and candidates[key] not in selected_references
                        ):
                            selected_references.append(candidates[key])

    if selected_references is not None:
        references = selected_references
    else:
        # Fail conservatively when the model omits selection: never expose an
        # entire discovery list beside an answer about a single order.
        references = mentioned_references or detail_references

        if not references and len(candidates) == 1:
            references = list(candidates.values())

    # Always preserve retrieved source provenance, even if the model omits card selection.
    references = (
        source_references
        + address_map_references
        + [
            ref for ref in references
            if ref not in source_references
        ]
    )

    return AgentAnswer(
        text=content.strip(),
        card_references=references[:5],
        usage=usage,
    )

def run_agent(*, user_id, role, history, text, thread_id):
    if not settings.CHATBOT_ENABLED or not settings.CHATBOT_GOOGLE_API_KEY:
        raise AgentUnavailable(
            'Gemini key is not configured or chatbot is disabled.'
        )

    from langchain_google_genai import ChatGoogleGenerativeAI

    model = ChatGoogleGenerativeAI(
        model=settings.CHATBOT_MODEL,
        api_key=settings.CHATBOT_GOOGLE_API_KEY,
        vertexai=False,
        timeout=settings.CHATBOT_MODEL_TIMEOUT,
        max_retries=0,
        max_tokens=settings.CHATBOT_MAX_OUTPUT_TOKENS,
    )

    messages = []

    for user_text, assistant_text in history:
        messages.extend([
            HumanMessage(content=user_text),
            AIMessage(content=assistant_text),
        ])

    messages.append(HumanMessage(content=text))

    is_worker = role == 'WORKER'

    context = AgentContext(
        deadline=time.monotonic() + settings.CHATBOT_RUN_TIMEOUT,
        thread_id=thread_id,
        customer_id=None if is_worker else user_id,
        worker_id=user_id if is_worker else None,
        role=role,
    )

    with open_checkpointer() as saver:
        agent = (
            create_worker_agent if is_worker else create_customer_agent
        )(model, saver)

        result = agent.invoke(
            {'messages': messages},
            config={
                'configurable': {'thread_id': thread_id},
                'recursion_limit': settings.CHATBOT_MAX_MODEL_CALLS * 4 + 10,
            },
            context=context,
        )

    return extract_answer(result)