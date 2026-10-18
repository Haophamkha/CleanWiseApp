import time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .agent.factory import extract_answer, run_agent
from .agent.schemas import AgentContext
from .agent.tools import confirm_booking_draft
from .agent.worker_tools import WORKER_TOOLS


class MergedAgentTests(SimpleTestCase):
    @override_settings(CHATBOT_ENABLED=True, CHATBOT_GOOGLE_API_KEY='test-key')
    def test_customer_and_worker_dispatch_preserves_trusted_context(self):
        for role in ('CUSTOMER', 'WORKER'):
            with self.subTest(role=role), patch(
                'langchain_google_genai.ChatGoogleGenerativeAI'
            ), patch(
                'apps.chatbot.agent.factory.generic_booking_service_choices', return_value=None
            ) as catalog, patch(
                'apps.chatbot.agent.factory.open_checkpointer'
            ), patch(
                'apps.chatbot.agent.factory.create_customer_agent'
            ) as customer_agent, patch(
                'apps.chatbot.agent.factory.create_worker_agent'
            ) as worker_agent:
                selected = worker_agent if role == 'WORKER' else customer_agent
                selected.return_value.invoke.return_value = {
                    'messages': [HumanMessage(content='hello'), AIMessage(content='reply')],
                }
                answer = run_agent(
                    customer_id=7, role=role, conversation_id=11, run_id='run-1',
                    history=[], text='hello', thread_id='thread-1',
                )
                self.assertEqual(answer.text, 'reply')
                context = selected.return_value.invoke.call_args.kwargs['context']
                self.assertEqual(context.role, role)
                self.assertEqual(context.conversation_id, 11)
                self.assertEqual(context.run_id, 'run-1')
                self.assertEqual(context.user_text, 'hello')
                self.assertEqual(context.thread_id, 'thread-1')
                if role == 'WORKER':
                    self.assertIsNone(context.customer_id)
                    self.assertEqual(context.worker_id, 7)
                    customer_agent.assert_not_called()
                    catalog.assert_not_called()
                else:
                    self.assertEqual(context.customer_id, 7)
                    self.assertIsNone(context.worker_id)
                    worker_agent.assert_not_called()

    def test_map_picker_card_survives_empty_model_selection(self):
        answer = extract_answer({'messages': [
            HumanMessage(content='address'),
            ToolMessage(content='ok', tool_call_id='map', name='open_address_map_picker',
                        artifact={'cards': [{'type': 'address_map'}]}),
            ToolMessage(content='ok', tool_call_id='select', name='select_response_cards',
                        artifact={'selected_cards': []}),
            AIMessage(content='Choose your address.'),
        ]})
        self.assertEqual(answer.card_references, [{'type': 'address_map'}])

    def test_booking_confirmation_rejects_a_reply_with_changes(self):
        runtime = SimpleNamespace(context=AgentContext(
            customer_id=7, deadline=time.monotonic() + 10,
            user_text='được, nhưng đổi sang 9 giờ',
        ))
        with patch('apps.chatbot.agent.tools.create_confirmed_booking') as create:
            content, artifact = confirm_booking_draft.func(runtime=runtime)
        self.assertIn('EXPLICIT_CONFIRMATION_REQUIRED', content)
        self.assertEqual(artifact, {'cards': []})
        create.assert_not_called()

    def test_worker_tool_schemas_convert_without_exposing_runtime(self):
        from langchain_google_genai._function_utils import convert_to_genai_function_declarations

        converted = convert_to_genai_function_declarations(WORKER_TOOLS)
        self.assertTrue(converted)
        for tool in WORKER_TOOLS:
            properties = tool.args_schema.model_json_schema().get('properties', {})
            self.assertNotIn('runtime', properties)
            self.assertNotIn('worker_id', properties)
