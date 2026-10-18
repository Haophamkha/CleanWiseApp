from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers

from apps.addresses.models import CustomerAddress
from apps.bookings.booking_service import (
    _validate_payment_method, _validate_schedules, calculate_booking_price,
    create_booking, get_package_discount_percent, is_computable_pricing,
)
from apps.bookings.schedule_builder import build_schedules
from apps.bookings.service_data_validation import validate_service_data
from apps.payments.models import Payment
from apps.services.models import Service
from apps.vouchers.voucher_service import validate_and_calculate_voucher
from ..models import ChatbotBookingDraft


def list_customer_addresses(customer_id):
    return list(CustomerAddress.objects.filter(customer_id=customer_id, is_active=True)
                .order_by('-is_default', '-created_at').values(
                    'id', 'label', 'receiver_name', 'address_line', 'ward', 'city', 'is_default'))


def prepare_booking(*, customer_id, conversation_id, run_id, service_id, address_id,
                    service_data, payment_method, delivery_address_id=None,
                    preferred_worker_id=None, voucher_code=''):
    customer = get_user_model().objects.get(pk=customer_id)
    service = get_object_or_404(Service, pk=service_id, is_active=True)
    address = get_object_or_404(CustomerAddress, pk=address_id, customer=customer, is_active=True)
    address_count = (service.form_schema or {}).get('address_count', 1)
    delivery = None
    if address_count >= 2:
        if not delivery_address_id:
            raise serializers.ValidationError({'delivery_address_id': 'Cần chọn địa chỉ nhận dịch vụ.'})
        delivery = get_object_or_404(CustomerAddress, pk=delivery_address_id, customer=customer, is_active=True)
    elif delivery_address_id:
        raise serializers.ValidationError({'delivery_address_id': 'Dịch vụ này không cần địa chỉ thứ hai.'})

    validate_service_data(service.form_schema, service_data)
    schedules = build_schedules(service.form_schema, service_data)
    _validate_schedules(schedules)
    _validate_payment_method(payment_method, form_schema=service.form_schema, sessions_count=len(schedules))

    if not is_computable_pricing(service.pricing_config):
        raise serializers.ValidationError({'service': 'Dịch vụ này chưa có quy tắc báo giá tự động.'})
    computed = calculate_booking_price(service, service_data)
    if computed is None:
        raise serializers.ValidationError({'service_data': 'Thiếu thông tin để tính giá.'})
    per_session = (service.pricing_config or {}).get('pricing_unit') == 'PER_SESSION'
    sessions = len(schedules)
    discount_percent = get_package_discount_percent(service, service_data) if per_session else Decimal('0')
    subtotal = (computed * sessions * (Decimal('1') - discount_percent / Decimal('100'))
                if per_session else computed)
    subtotal = subtotal.quantize(Decimal('0.01'))
    discount = Decimal('0')
    normalized_voucher = (voucher_code or '').strip().upper()
    if normalized_voucher:
        voucher_result = validate_and_calculate_voucher(
            code=normalized_voucher, customer=customer, subtotal_amount=subtotal,
            lock=False, error_field='voucher_code')
        discount = voucher_result['discount_amount']
    total = max(Decimal('0'), subtotal - discount).quantize(Decimal('0.01'))
    if total <= 0:
        raise serializers.ValidationError({'voucher_code': 'Tổng tiền sau ưu đãi phải lớn hơn 0.'})
    if payment_method == Payment.Method.WALLET:
        from apps.wallets.models import Wallet
        wallet = Wallet.objects.filter(user=customer).first()
        if wallet is None or wallet.balance < total:
            raise serializers.ValidationError({'payment_method': 'Số dư ví không đủ cho báo giá này. Hãy chọn phương thức khác.'})

    if preferred_worker_id:
        from apps.worker.assignment_service import validate_preferred_worker
        validate_preferred_worker(customer=customer, worker_id=preferred_worker_id,
                                  service=service, address=address)

    quote = {
        'service_name': service.name,
        'address_label': address.label,
        'address_city': address.city,
        'schedule_count': sessions,
        'scheduled_start': timezone.localtime(schedules[0]['scheduled_start']).isoformat(),
        'subtotal': str(subtotal), 'discount': str(discount), 'total': str(total),
        'voucher_code': normalized_voucher,
        'payment_method': payment_method,
    }
    ChatbotBookingDraft.objects.update_or_create(
        conversation_id=conversation_id,
        defaults={
            'customer': customer, 'service': service, 'address': address,
            'delivery_address': delivery,
            'preferred_worker_id': preferred_worker_id, 'service_data': service_data,
            'voucher_code': normalized_voucher, 'payment_method': payment_method,
            'quote': quote, 'prepared_run_id': run_id,
            'status': ChatbotBookingDraft.Status.AWAITING_CONFIRMATION,
            'booking': None, 'expires_at': timezone.now() + timedelta(minutes=10),
        },
    )
    return quote


@transaction.atomic
def confirm_booking(*, customer_id, conversation_id, run_id):
    customer = get_user_model().objects.get(pk=customer_id)
    draft = get_object_or_404(ChatbotBookingDraft.objects.select_for_update(),
                              conversation_id=conversation_id, customer=customer)
    if draft.status == ChatbotBookingDraft.Status.CONFIRMED and draft.booking_id:
        return draft.booking
    if draft.status != ChatbotBookingDraft.Status.AWAITING_CONFIRMATION:
        raise serializers.ValidationError({'booking': 'Không có bản nháp đang chờ xác nhận.'})
    if draft.prepared_run_id == run_id:
        raise serializers.ValidationError({'booking': 'Cần xác nhận ở tin nhắn tiếp theo.'})
    if draft.expires_at <= timezone.now():
        raise serializers.ValidationError({'booking': 'Bản nháp đã hết hạn. Hãy bắt đầu lại.'})
    booking = create_booking(
        customer=customer, service_id=draft.service_id, address_id=draft.address_id,
        delivery_address_id=draft.delivery_address_id, service_data=draft.service_data,
        note=None, preferred_worker_id=draft.preferred_worker_id,
        voucher_code=draft.voucher_code, payment_method=draft.payment_method,
    )
    draft.booking = booking
    draft.status = ChatbotBookingDraft.Status.CONFIRMED
    draft.save(update_fields=['booking', 'status', 'updated_at'])
    return booking


def discard_booking_draft(*, customer_id, conversation_id):
    customer = get_user_model().objects.get(pk=customer_id)
    ChatbotBookingDraft.objects.filter(
        customer=customer, conversation_id=conversation_id,
        status=ChatbotBookingDraft.Status.AWAITING_CONFIRMATION,
    ).update(status=ChatbotBookingDraft.Status.CANCELLED, updated_at=timezone.now())
