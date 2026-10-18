import time
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from rest_framework.test import APITransactionTestCase
from .models import HelpArticle
from .services.knowledge import search_help
from .services.response import build_cards
from .agent.factory import create_customer_agent, extract_answer
from .agent.schemas import AgentContext
from .tests import ScriptedChatModel


class KnowledgeTests(APITransactionTestCase):
    def setUp(self):
        self.customer = get_user_model().objects.create_user(username='help-customer', email='help@example.test', role='CUSTOMER')
        self.client.force_authenticate(self.customer)
        call_command('import_chatbot_help', publish=True, verbosity=0)

    def test_seed_is_repeatable_and_answers_real_topics_with_sources(self):
        call_command('import_chatbot_help', publish=True, verbosity=0)
        self.assertEqual(HelpArticle.objects.count(), 10)
        self.assertEqual(HelpArticle.objects.filter(role='CUSTOMER').count(), 5)
        self.assertEqual(HelpArticle.objects.filter(role='WORKER').count(), 5)
        for query in ['nạp tiền vào ví', 'nap tien vao vi', 'chuyển khoản VietQR', 'đặt đơn định kỳ', 'thêm địa chỉ']:
            with self.subTest(query=query):
                data, refs = search_help(query)
                self.assertTrue(data['results'])
                self.assertTrue(refs)
                self.assertTrue(all(item['version'] == HelpArticle.objects.get(pk=item['article_id']).version
                                    for item in data['results']))
                self.assertTrue(all(card['action'] == 'OPEN_HELP_ARTICLE' for card in build_cards(self.customer.pk, refs)))
        data, refs = search_help('chẩn đoán bệnh đau dạ dày')
        self.assertEqual(data['results'], [])
        self.assertEqual(refs, [])

    def test_role_publication_and_effective_dates_filter_api_and_search(self):
        for slug, changes in [
            ('draft', {'status': 'DRAFT'}), ('worker', {'role': 'WORKER'}),
            ('future', {'effective_from': timezone.localdate() + timedelta(days=1)}),
            ('expired', {'effective_from': timezone.localdate() - timedelta(days=2),
                         'effective_until': timezone.localdate() - timedelta(days=1)}),
        ]:
            article = HelpArticle.objects.create(slug=slug, title='Hướng dẫn kiểm thử riêng',
                sections=[{'heading': 'Mật mã riêng', 'text': 'tuvungkiemthu riêng'}],
                source_document='test', status='PUBLISHED', **changes) if slug != 'draft' else HelpArticle.objects.create(
                    slug=slug, title='Bản nháp', sections=[{'heading': 'Mật mã riêng', 'text': 'tuvungkiemthu riêng'}],
                    source_document='test', **changes)
            self.assertEqual(self.client.get(f'/api/chatbot/help-articles/{article.pk}/').status_code, 404)
        self.assertEqual(search_help('tuvungkiemthu')[0]['results'], [])
        response = self.client.get('/api/chatbot/help-articles/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 5)

    def test_revisions_retire_old_sources_and_do_not_resurrect(self):
        old = HelpArticle.objects.get(slug='thanh-toan-va-vi')
        new = HelpArticle.objects.create(slug=old.slug, version=2, title=old.title,
            summary=old.summary, sections=[{'heading': 'Nạp tiền', 'text': 'Nạp tiền vào ví bằng hướng dẫn mới.'}],
            source_document=old.source_document, status='PUBLISHED')
        old.refresh_from_db()
        self.assertEqual(old.status, 'RETIRED')
        self.assertEqual(self.client.get(f'/api/chatbot/help-articles/{old.pk}/').status_code, 404)
        self.assertEqual(build_cards(self.customer.pk, [{'type': 'help', 'id': old.pk}]), [])
        new.status = 'RETIRED'
        new.save()
        self.assertFalse(any(ref['id'] in (old.pk, new.pk) for ref in search_help('nạp tiền vào ví')[1]))
        old.status = 'PUBLISHED'
        with self.assertRaises(ValidationError):
            old.save()

    def test_published_content_is_immutable(self):
        article = HelpArticle.objects.first()
        article.sections = [{'heading': 'Sửa', 'text': 'Nội dung mới'}]
        with self.assertRaises(ValidationError):
            article.save()

    def test_api_requires_authenticated_supported_role(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/chatbot/help-articles/').status_code, 401)
        worker = get_user_model().objects.create_user(username='help-worker', email='worker@example.test', role='WORKER')
        self.client.force_authenticate(worker)
        response = self.client.get('/api/chatbot/help-articles/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row['id'] for row in response.data},
                         set(HelpArticle.objects.filter(role='WORKER', status='PUBLISHED').values_list('id', flat=True)))

    def test_agent_tool_loop_keeps_sources_even_without_card_selection(self):
        model = ScriptedChatModel(replies=[
            AIMessage(content='', tool_calls=[{'name': 'search_help_articles', 'args': {'query': 'nạp tiền vào ví'},
                                               'id': 'help-search', 'type': 'tool_call'}]),
            AIMessage(content='Vào Cá nhân, mở Ví / Thẻ thanh toán rồi chọn Nạp tiền theo hướng dẫn thanh toán.'),
        ])
        agent = create_customer_agent(model, InMemorySaver())
        result = agent.invoke({'messages': [HumanMessage(content='Nạp ví thế nào?')]},
            config={'configurable': {'thread_id': 'help-tool-test'}},
            context=AgentContext(self.customer.pk, time.monotonic() + 30))
        answer = extract_answer(result)
        self.assertTrue(answer.card_references)
        self.assertEqual(answer.usage['tool_calls'], 1)
        self.assertTrue(all(ref['type'] == 'help' for ref in answer.card_references))
