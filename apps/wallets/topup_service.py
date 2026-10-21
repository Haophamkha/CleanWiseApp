"""
Nạp tiền vào ví qua payOS (khách và nhân viên dùng chung).

TOPUP_MODE=mock  -> không gọi payOS, trả link giả; xác nhận bằng endpoint mock-confirm (chỉ khi DEBUG).
TOPUP_MODE=payos -> tạo payment link thật, webhook payOS cộng ví (có refresh_topup làm lưới an toàn khi mất webhook).
"""
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import serializers

from apps.payments.models import Payment

from . import wallet_service
from .models import WalletTopup, WalletTransaction

logger = logging.getLogger(__name__)

LINK_TTL_SECONDS = 15 * 60
EXPIRE_GRACE_MINUTES = 10
MAX_PENDING_TOPUPS = 5

_VND = Decimal('1')


def _vnd(value):
    return Decimal(value).quantize(_VND, rounding=ROUND_HALF_UP)


def _limit(name, default):
    return Decimal(str(getattr(settings, name, default)))


def topup_mode():
    mode = getattr(settings, 'TOPUP_MODE', 'payos')
    if mode not in ('mock', 'payos'):
        raise ImproperlyConfigured("TOPUP_MODE phải là 'mock' hoặc 'payos'.")
    if mode == 'mock' and not (settings.DEBUG or getattr(settings, 'PAYOUT_ALLOW_MOCK', False)):
        raise ImproperlyConfigured('TOPUP_MODE=mock chỉ được dùng khi DEBUG=True.')
    return mode


def _new_order_code():
    """
    orderCode dùng chung không gian với Payment của đơn đặt (~1.8e12), nên topup dùng bậc ~1.8e15
    (vẫn < 2^53 ~ 9e15) để không bao giờ đụng nhau; thêm 3 chữ số ngẫu nhiên chống trùng khi 2 request cùng ms.
    """
    for _ in range(5):
        code = int(time.time() * 1000) * 1000 + secrets.randbelow(1000)
        if not (
            WalletTopup.objects.filter(order_code=code).exists()
            or Payment.objects.filter(order_code=code).exists()
        ):
            return code
    raise RuntimeError('Không sinh được order_code')


def create_topup(*, user, amount):
    amount = _vnd(amount)
    minimum = _limit('WALLET_TOPUP_MIN', 50000)
    maximum = _limit('WALLET_TOPUP_MAX', 20000000)
    if amount < minimum:
        raise serializers.ValidationError({'amount': f'Số tiền nạp tối thiểu {minimum:,.0f}đ.'})
    if amount > maximum:
        raise serializers.ValidationError({'amount': f'Số tiền nạp tối đa {maximum:,.0f}đ mỗi lần.'})

    try:
        mode = topup_mode()
    except ImproperlyConfigured:
        logger.exception('Cấu hình TOPUP_MODE không hợp lệ')
        raise serializers.ValidationError({'detail': 'Chức năng nạp tiền tạm thời chưa khả dụng.'})

    pending = WalletTopup.objects.filter(
        user=user, status=WalletTopup.Status.PENDING, link_expires_at__gt=timezone.now(),
    ).count()
    if pending >= MAX_PENDING_TOPUPS:
        raise serializers.ValidationError({
            'detail': 'Bạn đang có quá nhiều lệnh nạp chưa thanh toán, vui lòng hoàn tất hoặc chờ hết hạn.',
        })

    expires_ts = int(time.time()) + LINK_TTL_SECONDS
    with transaction.atomic():
        topup = WalletTopup.objects.create(
            user=user, amount=amount, order_code=_new_order_code(),
            link_expires_at=datetime.fromtimestamp(expires_ts, tz=dt_timezone.utc),
        )

    if mode == 'mock':
        topup.payment_link_id = f'mock-{topup.order_code}'
        topup.checkout_url = f'https://mock.payos.local/pay/{topup.order_code}'
        topup.save(update_fields=['payment_link_id', 'checkout_url', 'updated_at'])
        return topup

    from payos.types import CreatePaymentLinkRequest

    from apps.payments.payment_link_service import _create_link, get_payos_client

    try:
        result = _create_link(get_payos_client(), CreatePaymentLinkRequest(
            order_code=topup.order_code,
            amount=int(amount),
            description=f'NAP{topup.pk}'[:25],
            return_url=settings.PAYOS_RETURN_URL,
            cancel_url=settings.PAYOS_CANCEL_URL,
            expired_at=expires_ts,
        ))
    except Exception:
        logger.exception('Tạo link nạp tiền payOS lỗi topup=%s', topup.pk)
        topup.status = WalletTopup.Status.CANCELLED
        topup.save(update_fields=['status', 'updated_at'])
        raise serializers.ValidationError({'detail': 'Không tạo được liên kết thanh toán, vui lòng thử lại.'})

    topup.payment_link_id = result.payment_link_id or ''
    topup.checkout_url = result.checkout_url or ''
    topup.qr_code = result.qr_code or ''
    topup.save(update_fields=['payment_link_id', 'checkout_url', 'qr_code', 'updated_at'])
    return topup


@transaction.atomic
def confirm_topup(*, order_code, amount, reference=''):
    """
    Webhook báo tiền đã vào -> cộng ví. Idempotent (khóa dòng + idempotency_key trên giao dịch ví).

    Cố ý KHÔNG kiểm tra status: tiền thật đã vào thì phải cộng, kể cả link đã hết hạn.
    Cộng theo số tiền thực nhận từ webhook. Trả None nếu orderCode không phải lệnh nạp.
    """
    topup = WalletTopup.objects.select_for_update().filter(order_code=order_code).first()
    if topup is None:
        return None
    if topup.paid_at:
        return topup

    amount = _vnd(amount)
    if amount <= 0:
        return topup
    if amount != topup.amount:
        logger.warning('Nạp tiền lệch số tiền topup=%s: yêu cầu %s, thực nhận %s', topup.pk, topup.amount, amount)

    tx = wallet_service.credit_wallet(
        user=topup.user, amount=amount, type=WalletTransaction.Type.TOPUP,
        note=f'Nạp tiền vào ví (mã {topup.order_code})',
        idempotency_key=f'topup:{topup.pk}',
    )
    topup.status = WalletTopup.Status.SUCCESS
    topup.paid_at = timezone.now()
    topup.transaction_code = (reference or str(order_code))[:100]
    topup.wallet_transaction = tx
    topup.save(update_fields=['status', 'paid_at', 'transaction_code', 'wallet_transaction', 'updated_at'])
    return topup


def refresh_topup(topup, *, force=False):
    """
    Lưới an toàn khi mất webhook: hỏi lại payOS cho lệnh nạp còn PENDING.
    Kiểm tra lại tên field (status / amount_paid) theo SDK payos 1.1.0 ở lần test đầu.
    """
    if topup.status != WalletTopup.Status.PENDING:
        return topup
    try:
        if topup_mode() != 'payos':
            return topup
    except ImproperlyConfigured:
        return topup
    if not force and not cache.add(f'topup_refresh:{topup.pk}', 1, 5):
        return topup

    from apps.payments.payment_link_service import get_payos_client

    try:
        info = get_payos_client().payment_requests.get(topup.order_code)
    except Exception:
        logger.warning('Hỏi trạng thái nạp tiền %s lỗi', topup.pk)
        return topup

    if str(getattr(info, 'status', '') or '').upper() == 'PAID':
        paid = getattr(info, 'amount_paid', None) or getattr(info, 'amount', None)
        if paid:
            return confirm_topup(
                order_code=topup.order_code, amount=paid, reference=f'SYNC{topup.order_code}',
            ) or topup
    return topup


def mock_confirm_topup(*, topup_id, user):
    if not settings.DEBUG or topup_mode() != 'mock':
        raise Http404
    topup = WalletTopup.objects.filter(pk=topup_id, user=user).first()
    if topup is None:
        raise Http404
    return confirm_topup(order_code=topup.order_code, amount=topup.amount, reference=f'MOCK{topup.order_code}')


def expire_pending_topups():
    """
    Link quá hạn thì hỏi payOS lần cuối (phòng mất webhook), vẫn PENDING thì đánh dấu EXPIRED.
    Nếu sau đó tiền vẫn về, confirm_topup vẫn cộng ví.
    """
    cutoff = timezone.now() - timedelta(minutes=EXPIRE_GRACE_MINUTES)
    topups = WalletTopup.objects.filter(
        status=WalletTopup.Status.PENDING, link_expires_at__lt=cutoff,
    ).order_by('link_expires_at')[:200]

    expired = 0
    for topup in topups:
        topup = refresh_topup(topup, force=True)
        if topup.status == WalletTopup.Status.PENDING:
            WalletTopup.objects.filter(pk=topup.pk, status=WalletTopup.Status.PENDING).update(
                status=WalletTopup.Status.EXPIRED, updated_at=timezone.now(),
            )
            expired += 1
    return expired