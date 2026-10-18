"""
Rút tiền tự động: kiểm tra hợp lệ -> trừ ví -> gọi kênh chi -> chốt kết quả.

Chỉ gọi lại hàm có sẵn trong wallet_service (request_withdraw / request_worker_withdraw,
admin_confirm_withdraw / admin_reject_withdraw), không sửa logic ví cũ.

NGUYÊN TẮC TIỀN:
- Trừ ví TRƯỚC, gọi payOS SAU (ngoài transaction).
- Chỉ HOÀN ví khi chắc chắn tiền chưa đi: payOS báo FAILED, hoặc PayoutRejected ở lần gọi ĐẦU
  (chưa từng có lần nào "không rõ kết quả").
- Đã từng "không rõ kết quả" mà sau đó bị từ chối / treo quá lâu -> needs_review, người đối soát tay.
"""
import logging
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from rest_framework import serializers

from apps.common.encryption import decrypt_value
from apps.payments.models import UserPaymentMethod

from . import payout_client, wallet_service
from .models import WalletTransaction, WithdrawRequest

logger = logging.getLogger(__name__)

_VND = Decimal('1')

MAX_RETRY_AGE = timedelta(hours=2)        # chưa có payout_id mà quá lâu -> đối soát tay
MAX_PROCESSING_AGE = timedelta(hours=6)   # PENDING quá lâu -> đối soát tay


def _vnd(value):
    return Decimal(value).quantize(_VND, rounding=ROUND_HALF_UP)


def _limit(name, default):
    return Decimal(str(getattr(settings, name, default)))


def _is_worker(user):
    return getattr(user, 'role', None) == 'WORKER'


def _get_payout_method(user, method_id):
    """Khách lưu tài khoản ở nhóm PAYMENT, nhân viên ở nhóm PAYOUT (theo views hiện có)."""
    usage = UserPaymentMethod.UsageType.PAYOUT if _is_worker(user) else UserPaymentMethod.UsageType.PAYMENT
    qs = UserPaymentMethod.objects.filter(
        user=user, usage_type=usage, is_active=True,
        method_type=UserPaymentMethod.MethodType.BANK_ACCOUNT,
    )
    if method_id:
        method = qs.filter(pk=method_id).first()
    else:
        method = qs.filter(is_default=True).first() or qs.order_by('-created_at').first()

    if not method:
        raise serializers.ValidationError({
            'payment_method_id': 'Vui lòng thêm tài khoản ngân hàng nhận tiền trước khi rút.',
        })
    if not method.bank_bin or not method.account_number_encrypted:
        raise serializers.ValidationError({
            'payment_method_id': 'Tài khoản ngân hàng thiếu thông tin, vui lòng thêm lại.',
        })
    if method.verification_status != UserPaymentMethod.VerificationStatus.VERIFIED:
        raise serializers.ValidationError({
            'payment_method_id': 'Vui lòng xác minh tài khoản ngân hàng trước khi rút tiền.',
        })

    # Chống kẻ chiếm tài khoản thêm STK của hắn rồi rút sạch ví ngay.
    cooldown_hours = int(getattr(settings, 'PAYOUT_METHOD_COOLDOWN_HOURS', 24))
    if cooldown_hours > 0 and timezone.now() - method.created_at < timedelta(hours=cooldown_hours):
        raise serializers.ValidationError({
            'payment_method_id': f'Tài khoản ngân hàng mới thêm, bạn có thể rút sau {cooldown_hours} giờ.',
        })
    return method


def _validate_amount(amount):
    minimum = _limit('WALLET_WITHDRAW_MIN', 50000)
    maximum = _limit('WALLET_WITHDRAW_MAX', 2000000)
    if amount < minimum:
        raise serializers.ValidationError({'amount': f'Số tiền rút tối thiểu {minimum:,.0f}đ.'})
    if amount > maximum:
        raise serializers.ValidationError({'amount': f'Số tiền rút tối đa {maximum:,.0f}đ mỗi lần.'})


def _check_daily_limit(user, amount):
    """PHẢI gọi khi đang giữ khóa ví (trong transaction) để 2 request song song không cùng lọt qua."""
    daily_max = _limit('WALLET_WITHDRAW_DAILY_MAX', 5000000)
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    used = (
        WithdrawRequest.objects.filter(user=user, created_at__gte=start)
        .exclude(status=WithdrawRequest.Status.FAILED)
        .aggregate(total=Sum('amount'))['total']
    ) or Decimal('0')
    if used + amount > daily_max:
        raise serializers.ValidationError({
            'amount': f'Vượt hạn mức rút trong ngày ({daily_max:,.0f}đ). Hôm nay bạn đã rút {used:,.0f}đ.',
        })


# ------------------------------------------------------------------ tạo lệnh

def create_withdraw(*, user, amount, payment_method_id=None):
    """Trừ ví ngay rồi gửi lệnh chi. Trả về WithdrawRequest (SUCCESS / PROCESSING / FAILED)."""
    amount = _vnd(amount)
    _validate_amount(amount)
    method = _get_payout_method(user, payment_method_id)

    # Cấu hình sai thì từ chối TRƯỚC khi đụng tiền.
    try:
        mode = payout_client.ensure_ready()
    except (ImproperlyConfigured, ImportError):
        logger.exception('Cấu hình payout lỗi')
        raise serializers.ValidationError({'detail': 'Chức năng rút tiền tạm thời chưa khả dụng.'})

    with transaction.atomic():
        wallet, _ = wallet_service.get_or_create_wallet_locked(user)  # khóa ví: chặn rút song song
        if wallet.balance < amount:
            raise serializers.ValidationError({'amount': 'Số dư ví không đủ.'})
        _check_daily_limit(user, amount)

        request_fn = wallet_service.request_worker_withdraw if _is_worker(user) else wallet_service.request_withdraw
        tx = request_fn(user=user, amount=amount)
        tx.note = f'Rút tiền về {method.bank_name} {method.account_number_masked}'.strip()
        tx.save(update_fields=['note', 'updated_at'])

        withdraw = WithdrawRequest.objects.create(
            user=user, wallet_transaction=tx, amount=amount,
            reference_id=f'WD{uuid.uuid4().hex[:16].upper()}', payout_mode=mode,
            bank_bin=method.bank_bin, bank_name=method.bank_name,
            account_holder_name=method.account_holder_name,
            account_number_encrypted=method.account_number_encrypted,
            account_number_last4=method.account_number_last4,
        )

    # Gọi mạng NGOÀI transaction: tiền đã trừ và lệnh đã lưu, lỡ lỗi vẫn còn dấu vết để task xử lý tiếp.
    try:
        return submit_payout(withdraw.pk)
    except Exception:
        # Tuyệt đối không để user nhận 500 sau khi đã trừ ví (bấm lại sẽ rút thêm lần nữa).
        logger.exception('submit_payout lỗi ngoài dự kiến withdraw=%s', withdraw.pk)
        return WithdrawRequest.objects.get(pk=withdraw.pk)


# ------------------------------------------------------------------ gửi / chốt

def _flag_review(withdraw_id, note):
    logger.error('WITHDRAW CẦN ĐỐI SOÁT TAY id=%s: %s', withdraw_id, note)
    WithdrawRequest.objects.filter(
        pk=withdraw_id, status=WithdrawRequest.Status.PROCESSING,
    ).update(needs_review=True, review_note=note[:500], updated_at=timezone.now())
    return WithdrawRequest.objects.get(pk=withdraw_id)


def submit_payout(withdraw_id):
    withdraw = WithdrawRequest.objects.get(pk=withdraw_id)
    if withdraw.status != WithdrawRequest.Status.PROCESSING or withdraw.needs_review:
        return withdraw

    try:
        account_number = decrypt_value(withdraw.account_number_encrypted)
    except Exception:
        # Không dùng logger.exception: tránh traceback chụp biến cục bộ.
        logger.error('Không giải mã được STK withdraw=%s', withdraw.pk)
        if withdraw.had_unknown_result:
            return _flag_review(withdraw.pk, 'Không giải mã được STK sau lần gọi không rõ kết quả')
        return _finalize(withdraw.pk, success=False, reason='Không đọc được thông tin tài khoản nhận.')

    WithdrawRequest.objects.filter(pk=withdraw.pk).update(
        submit_attempts=F('submit_attempts') + 1, updated_at=timezone.now(),
    )
    try:
        result = payout_client.create_payout(
            reference_id=withdraw.reference_id,
            amount=withdraw.amount,
            description=f'RUT{withdraw.pk}',
            bank_bin=withdraw.bank_bin,
            account_number=account_number,
        )
    except payout_client.PayoutRejected as exc:
        if withdraw.had_unknown_result:
            # Lần trước không rõ kết quả -> payOS có thể ĐÃ chi. Tuyệt đối không hoàn ví.
            return _flag_review(withdraw.pk, f'Bị từ chối sau lần không rõ kết quả: {exc}')
        return _finalize(withdraw.pk, success=False, reason=str(exc))
    except payout_client.PayoutUnknown:
        # Chưa biết tiền đã đi chưa -> KHÔNG hoàn ví, task đồng bộ gọi lại bằng cùng idempotency key.
        WithdrawRequest.objects.filter(pk=withdraw.pk).update(
            had_unknown_result=True, updated_at=timezone.now(),
        )
        return WithdrawRequest.objects.get(pk=withdraw.pk)
    return _apply_result(withdraw.pk, result)


def _apply_result(withdraw_id, result):
    if result.payout_id:
        WithdrawRequest.objects.filter(
            pk=withdraw_id, status=WithdrawRequest.Status.PROCESSING,
        ).update(payout_id=result.payout_id, updated_at=timezone.now())
    if result.state == payout_client.SUCCESS:
        return _finalize(withdraw_id, success=True)
    if result.state == payout_client.FAILED:
        return _finalize(withdraw_id, success=False, reason=result.error or 'Ngân hàng từ chối giao dịch.')
    return WithdrawRequest.objects.get(pk=withdraw_id)


@transaction.atomic
def _finalize(withdraw_id, *, success, reason=''):
    """
    Idempotent: chỉ chốt khi còn PROCESSING, gọi lại nhiều lần không hoàn/cộng 2 lần.
    Tự kiểm tra giao dịch ví còn PENDING, nếu không thì dừng và báo đối soát (không đoán).
    """
    withdraw = WithdrawRequest.objects.select_for_update().get(pk=withdraw_id)
    if withdraw.status != WithdrawRequest.Status.PROCESSING:
        return withdraw

    tx = WalletTransaction.objects.select_for_update().get(pk=withdraw.wallet_transaction_id)
    if tx.status != WalletTransaction.Status.PENDING:
        logger.error('Withdraw %s: wallet tx %s đang ở trạng thái %s', withdraw.pk, tx.pk, tx.status)
        withdraw.needs_review = True
        withdraw.review_note = f'Wallet tx đang {tx.status}, kết quả payout success={success}'[:500]
        withdraw.save(update_fields=['needs_review', 'review_note', 'updated_at'])
        return withdraw

    if success:
        wallet_service.admin_confirm_withdraw(transaction_id=tx.pk)
        withdraw.status = WithdrawRequest.Status.SUCCESS
    else:
        wallet_service.admin_reject_withdraw(
            transaction_id=tx.pk,
            reason=f'Rút tiền thất bại, đã hoàn ví: {reason}'[:500],
        )
        withdraw.status = WithdrawRequest.Status.FAILED
        withdraw.failure_reason = reason[:500]

    withdraw.needs_review = False
    withdraw.completed_at = timezone.now()
    withdraw.save(update_fields=['status', 'failure_reason', 'needs_review', 'completed_at', 'updated_at'])
    return withdraw


def refresh_withdraw(withdraw, *, force=False):
    """Hỏi lại trạng thái 1 lệnh đang xử lý (task đồng bộ và khi FE hỏi chi tiết)."""
    if withdraw.status != WithdrawRequest.Status.PROCESSING or withdraw.needs_review:
        return withdraw
    # FE poll liên tục thì không để mỗi lần poll đều gọi payOS.
    if not force and not cache.add(f'wd_refresh:{withdraw.pk}', 1, 10):
        return withdraw

    age = timezone.now() - withdraw.created_at
    if not withdraw.payout_id:
        if age > MAX_RETRY_AGE:
            return _flag_review(withdraw.pk, 'Quá hạn retry mà chưa có payout_id')
        return submit_payout(withdraw.pk)

    try:
        result = payout_client.get_payout_status(withdraw.payout_id)
    except payout_client.PayoutUnknown:
        result = None
    if result is not None:
        withdraw = _apply_result(withdraw.pk, result)
        if withdraw.status != WithdrawRequest.Status.PROCESSING:
            return withdraw
    if age > MAX_PROCESSING_AGE:
        return _flag_review(withdraw.pk, 'PROCESSING quá lâu, payOS chưa trả trạng thái cuối')
    return withdraw


def sync_processing_withdraws(*, min_age_seconds=30, limit=100):
    """Task Celery: đẩy các lệnh PROCESSING về trạng thái cuối."""
    cutoff = timezone.now() - timedelta(seconds=min_age_seconds)
    ids = list(
        WithdrawRequest.objects.filter(
            status=WithdrawRequest.Status.PROCESSING,
            needs_review=False,
            payout_mode=payout_client.current_mode(),  # lệnh mock cũ không bị đem hỏi payOS thật
            created_at__lte=cutoff,
        ).order_by('created_at').values_list('pk', flat=True)[:limit]
    )
    for withdraw_id in ids:
        try:
            refresh_withdraw(WithdrawRequest.objects.get(pk=withdraw_id), force=True)
        except Exception:
            logger.exception('Đồng bộ lệnh rút %s lỗi', withdraw_id)
    return len(ids)


# ------------------------------------------------------------------ đối soát tay / cảnh báo

@transaction.atomic
def resolve_review(*, withdraw_id, success, admin_user, note):
    """
    Admin chốt lệnh needs_review SAU KHI đã tra dashboard payOS.
    success=True  -> payOS đã chi tiền thật  -> chốt SUCCESS (không hoàn ví)
    success=False -> payOS chưa chi tiền     -> chốt FAILED, hoàn ví
    """
    withdraw = WithdrawRequest.objects.select_for_update().get(pk=withdraw_id)
    if withdraw.status != WithdrawRequest.Status.PROCESSING or not withdraw.needs_review:
        raise serializers.ValidationError({'detail': 'Lệnh này không ở trạng thái cần đối soát.'})

    verdict = 'ĐÃ CHI (không hoàn ví)' if success else 'CHƯA CHI (hoàn ví)'
    withdraw.review_note = f'{withdraw.review_note} | [Admin {admin_user.username}] {verdict}: {note}'
    withdraw.save(update_fields=['review_note', 'updated_at'])

    result = _finalize(withdraw.pk, success=success, reason=f'Admin đối soát: {note}')
    if result.status == WithdrawRequest.Status.PROCESSING:
        raise serializers.ValidationError({'detail': 'Không chốt được: giao dịch ví không còn ở trạng thái chờ.'})
    return result


def alert_stuck_withdraws(*, processing_minutes=10):
    """Task Celery: ghi log ERROR (Sentry/Telegram bắt được) cho lệnh cần người xử lý."""
    cutoff = timezone.now() - timedelta(minutes=processing_minutes)
    review_ids = list(
        WithdrawRequest.objects.filter(
            status=WithdrawRequest.Status.PROCESSING, needs_review=True,
        ).values_list('pk', flat=True)
    )
    stuck_ids = list(
        WithdrawRequest.objects.filter(
            status=WithdrawRequest.Status.PROCESSING, needs_review=False, created_at__lt=cutoff,
        ).values_list('pk', flat=True)
    )
    if review_ids:
        logger.error('CẢNH BÁO: %s lệnh rút cần đối soát tay: %s', len(review_ids), review_ids)
    if stuck_ids:
        logger.error('CẢNH BÁO: %s lệnh rút PROCESSING quá %s phút: %s', len(stuck_ids), processing_minutes, stuck_ids)
    return len(review_ids), len(stuck_ids)
