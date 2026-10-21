from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.bookings.activity_service import record_booking_activity
from apps.bookings.models import Booking, BookingActivity
from apps.payments.models import Payment

from . import wallet_service
from .models import WalletTransaction

_VND = Decimal('1')


def _vnd(value):
    return Decimal(value).quantize(_VND, rounding=ROUND_HALF_UP)


def per_session_refund_amount(booking):
    """Số tiền khách thực trả cho 1 buổi (đã trừ voucher, chia đều)."""
    sessions = (booking.price_breakdown or {}).get('sessions_count') or booking.schedules.count()
    if not sessions or not booking.total_amount:
        return Decimal('0')
    return _vnd(booking.total_amount / sessions)


def _notify(booking, amount, reason):
    from apps.notifications.services import notify_customer_refund
    transaction.on_commit(lambda: notify_customer_refund(booking, amount, reason))


@transaction.atomic
def refund_booking(*, booking, key, note, amount=None, actor=None, reason=None):
    """
    Hoàn tiền đơn đã PAID vào ví khách. Idempotent theo `key`.
    amount=None -> hoàn toàn bộ phần còn lại. Tự chuyển REFUNDED khi đã hoàn đủ.
    Đừng tự set booking.payment_status trước khi gọi hàm này.
    """
    Booking.objects.select_for_update().only('id').get(pk=booking.pk)
    booking.refresh_from_db(fields=['payment_status', 'refunded_amount', 'total_amount'])

    if booking.payment_status != Booking.PaymentStatus.PAID:
        return None
    if WalletTransaction.objects.filter(idempotency_key=key).exists():
        return None

    remaining = (booking.total_amount or Decimal('0')) - (booking.refunded_amount or Decimal('0'))
    amount = remaining if amount is None else min(_vnd(amount), remaining)
    if amount <= 0:
        return None

    payment = booking.payments.filter(status=Payment.Status.SUCCESS).first()
    meta = {'payment_id': payment.id if payment else None, 'amount': str(amount), 'key': key}
    record_booking_activity(
        booking=booking, actor=actor,
        event_type=BookingActivity.EventType.REFUND_CREATED,
        message=f'Tạo hoàn tiền {amount:,.0f}đ.', metadata=meta,
    )
    wallet_service.credit_wallet(
        user=booking.customer, amount=amount, booking=booking, note=note, idempotency_key=key,
    )

    booking.refunded_amount = (booking.refunded_amount or Decimal('0')) + amount
    fields = ['refunded_amount', 'updated_at']
    if booking.refunded_amount >= booking.total_amount:
        booking.payment_status = Booking.PaymentStatus.REFUNDED
        fields.append('payment_status')
        booking.payments.filter(status=Payment.Status.SUCCESS).update(
            status=Payment.Status.REFUNDED, updated_at=timezone.now(),
        )
    booking.save(update_fields=fields)

    record_booking_activity(
        booking=booking, actor=actor,
        event_type=BookingActivity.EventType.REFUND_COMPLETED,
        message=f'Đã hoàn {amount:,.0f}đ vào ví khách hàng.', metadata=meta,
    )
    _notify(booking, amount, reason)
    return amount


@transaction.atomic
def refund_unmatched_payment(*, payment, booking, amount, key, note, reason=None):
    """Tiền thật đã vào nhưng không khớp đơn (đến muộn / thiếu / dư) -> cộng ví khách. Idempotent."""
    if WalletTransaction.objects.filter(idempotency_key=key).exists():
        return None
    amount = _vnd(amount)
    if amount <= 0:
        return None

    wallet_service.credit_wallet(
        user=booking.customer, amount=amount, booking=booking, note=note, idempotency_key=key,
    )
    record_booking_activity(
        booking=booking,
        event_type=BookingActivity.EventType.REFUND_COMPLETED,
        message=f'Đã hoàn {amount:,.0f}đ vào ví: {note}',
        metadata={'payment_id': payment.id, 'amount': str(amount), 'key': key},
    )
    _notify(booking, amount, reason)
    return amount


