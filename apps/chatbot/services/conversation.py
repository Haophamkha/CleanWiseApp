import logging
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.ai_engine.models import ChatbotMessage, ChatbotSession
from ..agent.factory import run_agent
from ..agent.checkpoint import CHECKPOINT_SCHEMA
from ..agent.errors import failure_code
from ..exceptions import AgentUnavailable, ChatbotError
from ..models import ChatbotRun
from .response import build_cards

logger = logging.getLogger(__name__)


def get_conversation(customer, conversation_id):
    return get_object_or_404(ChatbotSession, pk=conversation_id, customer=customer)


def delete_conversation(customer, conversation_id):
    with transaction.atomic():
        conversation = get_object_or_404(ChatbotSession.objects.select_for_update(),
                                         pk=conversation_id, customer=customer)
        if conversation.runs.filter(status=ChatbotRun.Status.RUNNING).exists():
            raise ChatbotError('Hãy chờ tin nhắn đang xử lý hoàn tất.',
                               code='CONVERSATION_BUSY', status_code=409)
        # Checkpoints use the same database, so purge every attempt together
        # with the transcript in this transaction, including superseded retries.
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                for table in ('checkpoint_writes', 'checkpoint_blobs', 'checkpoints'):
                    qualified = f'{CHECKPOINT_SCHEMA}.{table}'
                    cursor.execute('SELECT to_regclass(%s)', [qualified])
                    if cursor.fetchone()[0] is not None:
                        cursor.execute(f'DELETE FROM {qualified} WHERE thread_id LIKE %s',
                                       [f'cleanwise-chatbot:{conversation.id}:%'])
        # ChatbotMessage.session uses DO_NOTHING; remove children explicitly.
        conversation.runs.all().delete()
        conversation.messages.all().delete()
        conversation.delete()


def committed_history(conversation):
    runs = list(conversation.runs.filter(status=ChatbotRun.Status.SUCCEEDED,
                                         assistant_message__isnull=False)
                .select_related('user_message', 'assistant_message')
                .order_by('-user_message_id')[:settings.CHATBOT_HISTORY_TURNS])
    pairs, size = [], 0
    for run in runs:
        pair = (run.user_message.message, run.assistant_message.message)
        pair_size = sum(map(len, pair))
        if size + pair_size > settings.CHATBOT_HISTORY_CHARACTERS:
            break
        pairs.append(pair)
        size += pair_size
    return list(reversed(pairs))


def _claim_run(customer, conversation_id, text, client_message_id):
    with transaction.atomic():
        conversation = get_object_or_404(ChatbotSession.objects.select_for_update(),
                                         pk=conversation_id, customer=customer)
        if conversation.status != ChatbotSession.Status.ACTIVE:
            raise ChatbotError('Hội thoại đã đóng. Vui lòng tạo hội thoại mới.', code='CONVERSATION_CLOSED', status_code=409)
        message = conversation.messages.filter(client_message_id=client_message_id).first()
        run = None
        if message:
            if message.message != text:
                raise ChatbotError('ID tin nhắn đã dùng cho nội dung khác.', code='MESSAGE_ID_REUSED', status_code=422)
            run = ChatbotRun.objects.filter(user_message=message).first()
            if run and run.status == ChatbotRun.Status.SUCCEEDED:
                return run, True

        now = timezone.now()
        active = conversation.runs.filter(status=ChatbotRun.Status.RUNNING).first()
        if active:
            if active.lease_expires_at > now:
                raise ChatbotError('Hội thoại đang xử lý một tin nhắn. Vui lòng chờ.', code='CONVERSATION_BUSY', status_code=409)
            active.status = ChatbotRun.Status.FAILED
            active.error_code = 'RUN_EXPIRED'
            active.finished_at = now
            active.save(update_fields=['status', 'error_code', 'finished_at'])
            active.user_message.status = ChatbotMessage.Status.FAILED
            active.user_message.save(update_fields=['status'])

        if message and conversation.messages.filter(sender_type='CUSTOMER', id__gt=message.id).exists():
            raise ChatbotError('Có tin nhắn mới hơn. Hãy gửi lại bằng ID mới.', code='OUTDATED_RETRY', status_code=409)
        if message is None:
            message = ChatbotMessage.objects.create(session=conversation, sender_type='CUSTOMER',
                                                    message=text, client_message_id=client_message_id,
                                                    status=ChatbotMessage.Status.PROCESSING)
        else:
            message.status = ChatbotMessage.Status.PROCESSING
            message.save(update_fields=['status'])

        # Lease exceeds the budget plus one in-flight provider call and cleanup.
        lease = now + timedelta(seconds=settings.CHATBOT_RUN_TIMEOUT + settings.CHATBOT_MODEL_TIMEOUT + 60)
        if run:
            run.status = ChatbotRun.Status.RUNNING
            run.attempt_id = uuid.uuid4()
            run.attempts += 1
            run.lease_expires_at = lease
            run.finished_at = None
            run.error_code = ''
            run.model_name = settings.CHATBOT_MODEL
            run.save()
        else:
            run = ChatbotRun.objects.create(conversation=conversation, user_message=message,
                                            model_name=settings.CHATBOT_MODEL, lease_expires_at=lease)
        if not conversation.title:
            conversation.title = text[:160]
        conversation.save(update_fields=['title', 'updated_at'])
        return run, False


def _complete(run, answer, cards, duration_ms):
    with transaction.atomic():
        # Lock order is the same as claim: conversation first, run second.
        ChatbotSession.objects.select_for_update().get(pk=run.conversation_id)
        current = ChatbotRun.objects.select_for_update().get(pk=run.pk)
        if current.status != ChatbotRun.Status.RUNNING or current.attempt_id != run.attempt_id:
            raise ChatbotError('Lượt xử lý đã hết hạn. Vui lòng tải lại lịch sử.', code='RUN_SUPERSEDED', status_code=409)
        bot = ChatbotMessage.objects.create(session_id=run.conversation_id, sender_type='BOT',
                                            message=answer.text, cards=cards, status='COMPLETED')
        current.user_message.status = ChatbotMessage.Status.COMPLETED
        current.user_message.save(update_fields=['status'])
        current.assistant_message = bot
        current.status = ChatbotRun.Status.SUCCEEDED
        current.usage = answer.usage
        current.duration_ms = duration_ms
        current.finished_at = timezone.now()
        current.save()
        ChatbotSession.objects.filter(pk=run.conversation_id).update(updated_at=timezone.now())
        return current


def _fail(run, error_code, duration_ms):
    with transaction.atomic():
        ChatbotSession.objects.select_for_update().get(pk=run.conversation_id)
        current = ChatbotRun.objects.select_for_update().get(pk=run.pk)
        if current.status == ChatbotRun.Status.RUNNING and current.attempt_id == run.attempt_id:
            current.status = ChatbotRun.Status.FAILED
            current.error_code = error_code
            current.duration_ms = duration_ms
            current.finished_at = timezone.now()
            current.save()
            current.user_message.status = ChatbotMessage.Status.FAILED
            current.user_message.save(update_fields=['status'])


def send_message(customer, conversation_id, text, client_message_id):
    try:
        run, replayed = _claim_run(customer, conversation_id, text, client_message_id)
    except IntegrityError as exc:
        raise ChatbotError('Hội thoại đang được cập nhật. Vui lòng thử lại.', code='CONVERSATION_BUSY', status_code=409) from exc
    if replayed:
        return run, True
    started = time.monotonic()
    try:
        answer = run_agent(user_id=customer.id, role=customer.role,
                           history=committed_history(run.conversation),
                           text=text, thread_id=run.checkpoint_thread_id)
        refs = answer.card_references
        if customer.role == 'WORKER':   # thẻ đơn/buổi của khách chưa dùng cho worker, chỉ giữ thẻ nguồn help
            refs = [r for r in refs if r.get('type') == 'help']
        cards = build_cards(customer.id, refs, role=customer.role)
        return _complete(run, answer, cards, int((time.monotonic() - started) * 1000)), False
    except ChatbotError:
        raise
    except Exception as exc:
        error_code = 'CHATBOT_NOT_CONFIGURED' if isinstance(exc, AgentUnavailable) else failure_code(exc)
        _fail(run, error_code, int((time.monotonic() - started) * 1000))
        # Provider exception text can contain request content/credentials. Log only type and run ID
        # (ValueError là thông điệp do code nội bộ viết nên được phép in).
        # MỚI: in traceback đầy đủ khi chạy DEBUG để tìm lỗi TypeError; prod vẫn không lộ nội dung.
        logger.warning('Chatbot run %s failed (%s, %s)%s', run.id, type(exc).__name__, error_code,
                       f': {exc}' if isinstance(exc, ValueError) else '',
                       exc_info=settings.DEBUG)
        message = {
            'CHATBOT_NOT_CONFIGURED': 'Chatbot chưa được cấu hình đầy đủ. Vui lòng thử lại sau.',
            'CHATBOT_PROVIDER_BUSY': 'Dịch vụ AI đang tạm thời quá tải. Vui lòng thử lại sau ít phút.',
            'CHATBOT_MODEL_TIMEOUT': 'Dịch vụ AI phản hồi quá lâu. Vui lòng thử lại.',
            'CHATBOT_RATE_LIMITED': 'Dịch vụ AI đã đạt giới hạn sử dụng. Vui lòng thử lại sau.',
            'CHATBOT_PROVIDER_CONFIG_ERROR': 'Cấu hình dịch vụ AI chưa hợp lệ. Vui lòng liên hệ hỗ trợ.',
        }.get(error_code, 'Chưa thể trả lời lúc này. Vui lòng thử lại với cùng ID tin nhắn.')
        raise ChatbotError(message, code=error_code) from exc