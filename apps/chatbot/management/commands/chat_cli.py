import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.ai_engine.models import ChatbotSession
from apps.chatbot.exceptions import ChatbotError
from apps.chatbot.services.conversation import send_message
from apps.chatbot.services.embedding import embed_texts
from apps.chatbot.services.knowledge import keyword_hits, search_help, vector_hits


class Command(BaseCommand):
    help = 'Chat với chatbot ngay trên terminal. --retrieve "câu hỏi" để test riêng phần RAG.'

    def add_arguments(self, parser):
        parser.add_argument('--user', help='email hoặc username của khách hoặc nhân viên (role CUSTOMER hoặc WORKER)')
        parser.add_argument('--retrieve', help='chỉ test truy hồi, không gọi LLM')
        parser.add_argument('--role', default='CUSTOMER', choices=['CUSTOMER', 'WORKER'],
                            help='role dùng để lọc bài viết khi --retrieve (mặc định CUSTOMER)')

    def handle(self, *args, **options):
        if options['retrieve']:
            return self.retrieve(options['retrieve'], options['role'])
        if not options['user']:
            raise CommandError('Cần --user email_hoac_username')
        customer = get_user_model().objects.filter(
            Q(email=options['user']) | Q(username=options['user']),
            role__in=['CUSTOMER', 'WORKER']).first()
        if not customer:
            raise CommandError('Không tìm thấy khách hàng hoặc nhân viên.')
        conversation = ChatbotSession.objects.create(customer=customer)
        self.stdout.write(f'Hội thoại #{conversation.id} của {customer}. Gõ /new để mở hội thoại mới, /exit để thoát.')
        while True:
            try:
                text = input('\nBạn: ').strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text in ('/exit', '/quit'):
                break
            if text == '/new':
                conversation = ChatbotSession.objects.create(customer=customer)
                self.stdout.write(f'Hội thoại mới #{conversation.id}')
                continue
            if not text:
                continue
            try:
                run, _ = send_message(customer, conversation.id, text, uuid.uuid4())
            except ChatbotError as exc:
                self.stdout.write(self.style.ERROR(f'Lỗi: {exc.detail}'))
                continue
            bot = run.assistant_message
            self.stdout.write(f'\nBot: {bot.message}')
            for card in bot.cards:
                self.stdout.write(self.style.WARNING(f"  [thẻ] {card['type']}: {card.get('title')}"))
            u = run.usage
            self.stdout.write(f"  (model_calls={u.get('model_calls')}, tool_calls={u.get('tool_calls')}, "
                              f"tokens={u.get('total_tokens')}, {run.duration_ms}ms)")

    def retrieve(self, query, role='CUSTOMER'):
        vec = None
        try:
            vec = embed_texts([query], 'RETRIEVAL_QUERY')[0]  # chỉ embed 1 lần
        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f'Embedding lỗi: {type(exc).__name__}, code={getattr(exc, "code", None)}'))
        self.stdout.write(f'--- Từ khóa (role={role}) ---')
        for article, index, section in keyword_hits(query, role=role):
            self.stdout.write(f"{article.slug} #{index}: {section['heading']}")
        self.stdout.write('--- Vector (distance, càng nhỏ càng giống) ---')
        if vec is not None:
            for chunk in vector_hits(query, query_vector=vec, role=role):
                self.stdout.write(f'{chunk.distance:.3f}  {chunk.article.slug} #{chunk.section_index}: {chunk.heading}')
        self.stdout.write('--- Kết quả cuối (sau rerank RRF) ---')
        data, _ = search_help(query, query_vector=vec, role=role)
        for item in data['results']:
            self.stdout.write(f"{item['title']} / {item['section']}")