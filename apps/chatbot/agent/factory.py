import json
import re
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
from ..services import queries
from ..services.queries import normalized

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


def strip_internal_reference_payload(text):
    """Remove model-emitted card-selection JSON from customer-visible text."""
    decoder = json.JSONDecoder()
    spans = []
    cursor = 0
    while True:
        start = text.find('{', cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        references = value.get('references') if isinstance(value, dict) else None
        if (isinstance(references, list)
                and all(isinstance(ref, dict) and ref.get('type') in ('service', 'booking', 'schedule', 'help')
                        and type(ref.get('id')) is int for ref in references)):
            # JSONDecoder.raw_decode returns an absolute end offset into text.
            spans.append((start, end))
            cursor = end
        else:
            cursor = start + 1
    for start, end in reversed(spans):
        text = text[:start] + '\n' + text[end:]
    text = '\n'.join(line.rstrip() for line in text.splitlines()).strip()
    return re.sub(r'\n{3,}', '\n\n', text)


def service_name_is_mentioned(name, answer):
    """Match service names despite punctuation and harmless wording changes."""
    name_tokens = set(re.findall(r'\w+', normalized(name)))
    answer_tokens = set(re.findall(r'\w+', normalized(answer)))
    return bool(name_tokens) and name_tokens.issubset(answer_tokens)


def generic_booking_service_choices(text, history=()):
    """Present the catalog for broad service questions or unspecified booking requests."""
    normalized_text = normalized(text)
    booking_intent = any(phrase in normalized_text for phrase in (
        'dat lich', 'dat dich vu', 'muon dat', 'can dat', 'muon su dung dich vu',
    ))
    catalog_intent = any(phrase in normalized_text for phrase in (
        'nhung dich vu nao', 'danh sach dich vu', 'liet ke dich vu',
        'cac dich vu', 'dich vu nao', 'dich vu gi', 'co dich vu gi',
    ))
    if not booking_intent and not catalog_intent:
        return None

    pages = []
    page = 1
    while len(pages) < 10:
        services = queries.search_services('', page=page)
        pages.extend(services['results'])
        if not services['has_next'] or not services['results']:
            break
        page += 1
    if booking_intent and not catalog_intent:
        recent_context = ' '.join([text] + [part for pair in history[-3:] for part in pair])
        if any(service_name_is_mentioned(item['name'], recent_context) for item in pages):
            return None

    names = [item['name'] for item in pages]
    if not names:
        return AgentAnswer(
            text='Hiện mình chưa tìm thấy dịch vụ đang hoạt động. Bạn thử lại sau nhé.',
            usage={'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
                   'model_calls': 0, 'tool_calls': 0},
        )
    greeting = 'Chào bạn! ' if not history else ''
    text = (greeting + 'CleanWise hiện có 2 hình thức dịch vụ:\n'
            'Gói lẻ: Phù hợp khi bạn cần đặt dịch vụ một lần theo nhu cầu cụ thể.\n'
            'Gói tháng: Phù hợp khi bạn muốn sử dụng định kỳ theo lịch và số buổi đã chọn; '
            'chỉ áp dụng cho dịch vụ có hỗ trợ đăng ký định kỳ.\n\n'
            'Các dịch vụ đang hoạt động: ' + '; '.join(names) + '.')
    if services['has_next']:
        text += ' Danh sách còn dịch vụ khác; bạn có thể yêu cầu xem thêm.'
    text += '\n\n' + ('Bạn muốn đặt dịch vụ nào? Hãy chọn một thẻ bên dưới để xem chi tiết.'
                       if booking_intent else 'Bạn có thể chọn một thẻ bên dưới để xem chi tiết dịch vụ.')
    return AgentAnswer(
        text=text,
        card_references=[{'type': 'service', 'id': item['id']} for item in pages],
        usage={'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
               'model_calls': 0, 'tool_calls': 0},
    )


def address_management_answer(text, history=()):
    """Give concrete customer-app steps for managing saved addresses."""
    normalized_text = normalized(text)
    address_intent = ('dia chi' in normalized_text and any(verb in normalized_text.split()
                     for verb in ('them', 'tao', 'luu', 'cap', 'sua', 'chinh')))
    if not address_intent:
        return None
    greeting = 'Chào bạn! ' if not history else ''
    answer = (
        greeting + 'Bạn có thể thêm địa chỉ mới trong app CleanWise theo các bước sau:\n'
        '1. Vào Cá nhân > Địa chỉ của tôi > Thêm địa chỉ.\n'
        '2. Chọn Chọn trên bản đồ hoặc Nhập thủ công.\n'
        '3. Điền nhãn địa chỉ, tên và số điện thoại người nhận cùng địa chỉ cụ thể, rồi nhấn Thêm địa chỉ để lưu.\n'
        'Với dịch vụ chuyển nhà, hãy lưu riêng địa chỉ chuyển đi và địa chỉ chuyển đến. Sau đó quay lại trang dịch vụ và chọn đúng địa chỉ ở từng mục.'
    )
    return AgentAnswer(text=answer, usage={
        'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
        'model_calls': 0, 'tool_calls': 0,
    })


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
    content = strip_internal_reference_payload(content)
    if not content:
        content = 'Bạn vui lòng cho biết rõ dịch vụ muốn xem hoặc đặt lịch nhé.'
    candidates = {}
    detail_references = []
    mentioned_references = []
    mentioned_service_references = []
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
                if message.name == 'search_services':
                    try:
                        data = json.loads(message.content)
                        for item in data.get('results', []):
                            name = item.get('name')
                            ref = {'type': 'service', 'id': item.get('id')}
                            if (isinstance(name, str) and name.strip()
                                    and service_name_is_mentioned(name, content)
                                    and candidates.get(('service', ref['id'])) == ref
                                    and ref not in mentioned_service_references):
                                mentioned_service_references.append(ref)
                    except (ValueError, TypeError, AttributeError):
                        pass
                if message.name == 'select_response_cards' and 'selected_cards' in artifact:
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
        # A service card must be named in the natural-language answer. When the
        # reply offers several named services, show those choices instead of
        # trusting a potentially arbitrary single model selection.
        named_services = {('service', ref['id']) for ref in mentioned_service_references}
        references = [
            ref for ref in selected_references
            if ref.get('type') != 'service' or ('service', ref['id']) in named_services
        ]
        if mentioned_service_references:
            references.extend(ref for ref in mentioned_service_references if ref not in references)
    else:
        # Fail conservatively when the model omits selection: never expose an
        # entire discovery list beside an answer about a single order.
        references = mentioned_references or mentioned_service_references or detail_references
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

def run_agent(*, history, text, thread_id, user_id=None, role='CUSTOMER',
              customer_id=None, conversation_id=0, run_id=''):
    if not settings.CHATBOT_ENABLED or not settings.CHATBOT_GOOGLE_API_KEY:
        raise AgentUnavailable(
            'Gemini key is not configured or chatbot is disabled.'
        )

    user_id = user_id if user_id is not None else customer_id
    if role == 'CUSTOMER':
        service_choices = generic_booking_service_choices(text, history)
        if service_choices is not None:
            return service_choices

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
        conversation_id=conversation_id,
        run_id=str(run_id),
        user_text=text,
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
