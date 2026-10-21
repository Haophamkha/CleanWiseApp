import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.bookings.activity_service import record_booking_activity
from apps.bookings.expiry_service import cancel_unpaid_booking
from apps.bookings.models import Booking, BookingActivity
from apps.vouchers.voucher_service import mark_user_voucher_used
from apps.wallets import refund_service

from .models import Payment
from .payment_link_service import get_payos_client

logger = logging.getLogger(__name__)


def verify_and_parse_payos_webhook(raw_body: bytes):
    """Raise payos.WebhookError nếu chữ ký (HMAC-SHA256) sai."""
    return get_payos_client().webhooks.verify(raw_body)


def _find_payment(order_code):
    row = Payment.objects.filter(order_code=order_code).values('id', 'booking_id').first()
    if row:
        return row
    # Link cũ (trước bản vá) dùng payment.id làm orderCode
    return Payment.objects.filter(pk=order_code, order_code__isnull=True).values('id', 'booking_id').first()


def _return_to_wallet(*, payment, booking, amount, key, reason):
    payment.status = Payment.Status.REFUNDED
    payment.paid_at = timezone.now()
    payment.failure_reason = reason
    payment.save(update_fields=['status', 'paid_at', 'failure_reason', 'updated_at'])
    refund_service.refund_unmatched_payment(
        payment=payment, booking=booking, amount=amount, key=key,
        note=f'{reason} - {booking.booking_code}',
        reason=reason,
    )


@transaction.atomic
def handle_payos_webhook(webhook_data):
    # Chỉ xử lý giao dịch thành công ('00'). Webhook hủy/lỗi không được cộng tiền hay đổi trạng thái.
    code = getattr(webhook_data, 'code', '00')
    if str(code) != '00':
        logger.info('payOS webhook không phải giao dịch thành công: code=%s', code)
        return None

    order_code = webhook_data.order_code
    amount = Decimal(str(webhook_data.amount))

    candidate = _find_payment(order_code)
    if not candidate:
        # Không phải thanh toán đơn -> có thể là nạp tiền vào ví
        from apps.wallets import topup_service
        topup = topup_service.confirm_topup(
            order_code=order_code, amount=amount, reference=webhook_data.reference or '',
        )
        if topup:
            return topup
        # Tiền đã vào mà không có Payment/WalletTopup -> cần đối soát tay
        logger.error('payOS webhook: không có Payment/WalletTopup orderCode=%s amount=%s', order_code, amount)
        return None

    # Lock booking trước, payment sau
    booking = Booking.objects.select_for_update().get(pk=candidate['booking_id'])
    payment = Payment.objects.select_for_update().get(pk=candidate['id'])

    if payment.paid_at:
        return payment

    # Tiền đến khi payment/đơn không còn chờ thanh toán -> hoàn vào ví
    if (
        payment.status != Payment.Status.PENDING
        or booking.status in (Booking.Status.CANCELLED, Booking.Status.FAILED)
        or booking.payment_status != Booking.PaymentStatus.UNPAID
    ):
        _return_to_wallet(
            payment=payment, booking=booking, amount=amount, key=f'late:{payment.id}',
            reason='Thanh toán đến sau khi đơn đã hủy/hết hạn',
        )
        return payment

    # Chuyển thiếu -> hoàn toàn bộ vào ví, hủy đơn
    if amount < payment.amount:
        _return_to_wallet(
            payment=payment, booking=booking, amount=amount, key=f'under:{payment.id}',
            reason=f'Chuyển thiếu ({amount:,.0f}đ / {payment.amount:,.0f}đ)',
        )
        cancel_unpaid_booking(booking=booking, reason='Chuyển khoản thiếu tiền, đã hoàn vào ví.')
        return payment

    payment.status = Payment.Status.SUCCESS
    payment.transaction_code = webhook_data.reference or str(order_code)
    payment.paid_at = timezone.now()
    payment.save(update_fields=['status', 'transaction_code', 'paid_at', 'updated_at'])

    booking.payment_status = Booking.PaymentStatus.PAID
    booking.save(update_fields=['payment_status', 'updated_at'])
    if booking.user_voucher_id:
        mark_user_voucher_used(user_voucher_id=booking.user_voucher_id)
    record_booking_activity(
        booking=booking,
        event_type=BookingActivity.EventType.PAYMENT_UPDATED,
        message=f'Thanh toán đơn {booking.booking_code} thành công.',
        old_data={'status': Payment.Status.PENDING},
        new_data={'status': Payment.Status.SUCCESS, 'payment_id': payment.id},
    )

    from apps.worker.assignment_service import activate_preferred_worker_request
    activate_preferred_worker_request(booking)
    
    # Chuyển dư -> hoàn phần dư vào ví
    extra = amount - payment.amount
    if extra > 0:
        refund_service.refund_unmatched_payment(
            payment=payment, booking=booking, amount=extra, key=f'over:{payment.id}',
            note=f'Hoàn phần chuyển dư - {booking.booking_code}',
            reason='Bạn chuyển dư so với số tiền của đơn.',
        )
    return payment