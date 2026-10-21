import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers

from apps.wallets import wallet_service
from apps.services.models import Service
from apps.addresses.models import CustomerAddress
from apps.vouchers.voucher_service import (
    mark_user_voucher_used,
    release_user_voucher,
    reserve_user_voucher,
    validate_and_calculate_voucher,
)
from apps.notifications.services import notify_worker_booking_cancelled, notify_worker_schedule_cancelled
from apps.wallets.models import WalletTransaction
import secrets

from apps.payments.models import Payment

from .activity_service import record_booking_activity
from .models import Booking, BookingActivity, BookingSchedule
from .service_data_validation import validate_service_data
from .schedule_builder import build_schedules

from apps.wallets import earning_service, refund_service
from apps.worker.models import BookingAssignment

from datetime import timedelta

CUSTOMER_MIN_CANCEL_HOURS = 6

CANCELLABLE_STATUSES = (Booking.Status.PENDING, Booking.Status.ASSIGNED, Booking.Status.IN_PROGRESS)
_DONE_SCHEDULE_STATUSES = (
    BookingSchedule.Status.COMPLETED,
    BookingSchedule.Status.CANCELLED,
    BookingSchedule.Status.MISSED,
)

PRICE_DRIVEN_KEYS = ('base_prices', 'price_matrix', 'unit_prices')
TWO_PLACES = Decimal('0.01')


def _q(amount):
    if amount is None:
        return None
    return Decimal(amount).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def is_computable_pricing(pricing_config):
    return any(key in (pricing_config or {}) for key in PRICE_DRIVEN_KEYS)


def _resolve_flat_options(field, service_data):
    options = field.get('options')
    if not options:
        return []
    if 'when' not in options[0]:
        return options
    options_by = field.get('options_by')
    if not options_by:
        return []
    current_value = service_data.get(options_by)
    for group in options:
        if group.get('when', {}).get(options_by) == current_value:
            return group.get('items', [])
    return []


def _find_field_by_option_values(fields, candidate_values, service_data, exclude_key=None):
    for field in fields:
        if field.get('key') == exclude_key:
            continue
        options = _resolve_flat_options(field, service_data)
        if not options:
            continue
        if any(option.get('value') in candidate_values for option in options):
            return field
    return None


def get_unit_price_for_item(pricing_config, item_fields, item):
    if not pricing_config.get('unit_prices'):
        return None
    node = pricing_config['unit_prices']
    for sub in item_fields:
        if sub.get('type') in ('QUANTITY', 'BOOLEAN'):
            continue
        if not isinstance(node, dict):
            return None
        key = sub['key']
        value = item.get(key)
        if value is not None and value in node:
            node = node[value]
        else:
            return None
    try:
        return Decimal(str(node))
    except (TypeError, ValueError, ArithmeticError):
        return None


def get_item_total_price(pricing_config, item_fields, item):
    unit_price = get_unit_price_for_item(pricing_config, item_fields, item)
    if unit_price is None:
        return None
    qty = item.get('quantity', 1)
    try:
        qty = Decimal(str(qty))
    except (TypeError, ValueError, ArithmeticError):
        return None
    total = unit_price * qty
    for field in item_fields:
        if field.get('type') == 'BOOLEAN' and item.get(field['key']):
            surcharge = pricing_config.get(f"{field['key']}_price")
            if surcharge is not None:
                try:
                    total += (Decimal(str(surcharge)) * qty)
                except (TypeError, ValueError, ArithmeticError):
                    pass
    return _q(total)


def calculate_booking_price(service, service_data):
    """
    Trả về:
    - Nếu pricing_config.pricing_unit == 'PER_SESSION': giá của MỘT buổi
      (base_prices[duration] + additional_services đã chọn). Việc nhân
      với số buổi và trừ % giảm gói được xử lý riêng ở create_booking().
    - Ngược lại: tổng giá trọn gói như trước (price_matrix / unit_prices...).
    """
    pricing_config = service.pricing_config or {}
    fields = (service.form_schema or {}).get('fields', [])
    total = Decimal('0')
    has_base = False

    if pricing_config.get('price_matrix'):
        price_matrix = pricing_config['price_matrix']
        outer_field = _find_field_by_option_values(fields, list(price_matrix.keys()), service_data)
        outer_value = service_data.get(outer_field['key']) if outer_field else None
        inner_map = price_matrix.get(outer_value) if outer_value else None
        if inner_map:
            inner_field = _find_field_by_option_values(
                fields, list(inner_map.keys()), service_data,
                exclude_key=(outer_field['key'] if outer_field else None),
            )
            inner_value = service_data.get(inner_field['key']) if inner_field else None
            price = inner_map.get(inner_value)
            if price is not None:
                try:
                    total += Decimal(str(price))
                    has_base = True
                except (TypeError, ValueError, ArithmeticError):
                    pass

    elif pricing_config.get('base_prices'):
        base_prices = pricing_config['base_prices']
        field = _find_field_by_option_values(fields, list(base_prices.keys()), service_data)
        value = service_data.get(field['key']) if field else None
        price = base_prices.get(value)
        if price is not None:
            try:
                total += Decimal(str(price))
                has_base = True
            except (TypeError, ValueError, ArithmeticError):
                pass

    for key, value in pricing_config.items():
        if key.endswith('_surcharge') and isinstance(value, dict):
            field = _find_field_by_option_values(fields, list(value.keys()), service_data)
            selected_value = service_data.get(field['key']) if field else None
            price = value.get(selected_value)
            if price is not None:
                try:
                    total += Decimal(str(price))
                except (TypeError, ValueError, ArithmeticError):
                    pass

    if pricing_config.get('additional_services'):
        additional_services = pricing_config['additional_services']
        field = next((f for f in fields if f.get('type') == 'MULTI_SELECT'), None)
        selected = service_data.get(field['key'], []) if field else []
        for value in selected:
            price = additional_services.get(value)
            if price is not None:
                try:
                    total += Decimal(str(price))
                except (TypeError, ValueError, ArithmeticError):
                    pass

    if pricing_config.get('unit_prices'):
        group_field = next((f for f in fields if f.get('type') == 'REPEATABLE_GROUP'), None)
        if group_field:
            group_key = group_field['key']
            items = service_data.get(group_key, [])
            for item in items:
                item_total = get_item_total_price(pricing_config, group_field.get('item_fields', []), item)
                if item_total is not None:
                    total += item_total
                    has_base = True

    if not has_base and total == 0:
        return None
    return _q(total)


def get_package_discount_percent(service, service_data):
    """
    % giảm theo thời hạn gói, đọc từ pricing_config.package_discount_percent
    (key khớp option value của field package_duration, vd "2_MONTHS": 5).

    Chỉ áp dụng cho dịch vụ pricing_unit == 'PER_SESSION'. Dịch vụ không
    khai báo package_discount_percent, hoặc không match được field/giá trị
    -> trả về 0% (không giảm), không raise lỗi.
    """
    pricing_config = service.pricing_config or {}
    discount_map = pricing_config.get('package_discount_percent')
    if not discount_map:
        return Decimal('0')

    fields = (service.form_schema or {}).get('fields', [])
    field = _find_field_by_option_values(fields, list(discount_map.keys()), service_data)
    value = service_data.get(field['key']) if field else None
    percent = discount_map.get(value)
    if percent is None:
        return Decimal('0')
    try:
        return Decimal(str(percent))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal('0')


_CODE_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'


def _generate_booking_code():
    day = timezone.localtime().strftime('%y%m%d')
    for _ in range(5):
        suffix = ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(5))
        code = f'CW-{day}-{suffix}'
        if not Booking.objects.filter(booking_code=code).exists():
            return code
    raise serializers.ValidationError({'booking': 'Không tạo được mã đơn, vui lòng thử lại.'})


def _validate_schedules(schedules):
    if not schedules:
        raise serializers.ValidationError({'schedules': 'Cần ít nhất 1 buổi làm việc.'})

    now = timezone.now()
    open_t = settings.BOOKING_OPEN_TIME
    last_t = settings.BOOKING_LAST_START_TIME

    for idx, schedule in enumerate(schedules, start=1):
        if schedule['scheduled_start'] <= now:
            raise serializers.ValidationError(
                {'schedules': f'Buổi {idx}: thời gian bắt đầu phải ở tương lai.'}
            )
        if schedule['scheduled_start'] >= schedule['scheduled_end']:
            raise serializers.ValidationError(
                {'schedules': f'Buổi {idx}: giờ bắt đầu phải trước giờ kết thúc.'}
            )
        local_start = timezone.localtime(schedule['scheduled_start']).time()
        if not (open_t <= local_start <= last_t):
            raise serializers.ValidationError(
                {'schedules': f'Buổi {idx}: giờ bắt đầu phải trong khoảng '
                              f'{open_t:%H:%M} đến {last_t:%H:%M}.'}
            )
def _validate_payment_method(payment_method, *, form_schema, sessions_count):
    allowed = {Payment.Method.CASH, Payment.Method.BANK_TRANSFER, Payment.Method.WALLET}
    if payment_method not in allowed:
        raise serializers.ValidationError({'payment_method': 'Phương thức thanh toán không hợp lệ.'})

    is_multi = (form_schema or {}).get('schedule_type', 'ONCE') != 'ONCE' or sessions_count > 1
    if is_multi and payment_method == Payment.Method.CASH:
        raise serializers.ValidationError(
            {'payment_method': 'Dịch vụ định kỳ/nhiều buổi phải thanh toán trước (chuyển khoản hoặc ví).'}
        )


@transaction.atomic
def create_booking(
    *,
    customer,
    service_id,
    address_id,
    delivery_address_id=None,
    service_data,
    note=None,
    voucher_code=None,
    preferred_worker_id=None,
    payment_method,
    actor=None,
):
    """
    Tạo Booking và Payment trong cùng một transaction.

    Nếu một trong hai thao tác thất bại,
    toàn bộ transaction sẽ rollback.

    Lưu ý: không còn nhận `schedules` từ client — được BE tự sinh
    bên dưới từ service_data + service.form_schema.schedule_type
    (xem schedule_builder.py). Lý do: mỗi loại dịch vụ có cấu trúc
    lịch khác nhau (1 buổi cụ thể / định kỳ theo thứ...), để FE tự
    tính dễ lệch logic giữa các nơi dùng lại.

    ĐỔI: bỏ select_for_update() trên Service và CustomerAddress —
    hàm này chỉ ĐỌC 2 bảng này, không ghi/sửa gì lên chúng.

    ĐỔI: dịch vụ pricing_unit == 'PER_SESSION' (dọn dẹp định kỳ) tính giá
    = unit_price (giá 1 buổi) × sessions_count × (1 - package_discount_percent/100),
    thay vì trước đây dùng price_matrix cố định theo package_duration mà
    không phụ thuộc số ngày/tuần khách chọn (lỗ hổng giá đã sửa).

    ĐỔI: price_breakdown['unit_price'] giờ lưu giá/buổi ĐÃ TRỪ
    package_discount_percent (= subtotal / sessions_count), không còn là
    giá gốc trước giảm. Lý do: giảm giá gói (cam kết dài hạn) là mức giá
    thật của buổi làm, worker cùng chịu — khác với voucher (ưu đãi riêng
    cho khách, app gánh, không trừ vào lương worker vì unit_price tính từ
    subtotal chứ không phải total_amount). Giá trị này chốt 1 lần lúc tạo
    booking nên không đổi khi khách hủy bớt buổi khác trong gói. Giá gốc
    trước giảm được lưu riêng ở 'base_unit_price' để tham khảo/đối soát.
    """


    service = get_object_or_404(
        Service.objects,
        pk=service_id,
        is_active=True,
    )

    address = get_object_or_404(
        CustomerAddress.objects,
        pk=address_id,
        customer=customer,
        is_active=True,
    )

    address_count = (service.form_schema or {}).get('address_count', 1)
    delivery_address = None

    if address_count >= 2:
        if not delivery_address_id:
            raise serializers.ValidationError(
                {'delivery_address_id': 'Dịch vụ này cần chọn địa chỉ chuyển đến.'}
            )
        delivery_address = get_object_or_404(
            CustomerAddress.objects,
            pk=delivery_address_id,
            customer=customer,
            is_active=True,
        )
    elif delivery_address_id:
        raise serializers.ValidationError(
            {'delivery_address_id': 'Dịch vụ này không cần địa chỉ chuyển đến.'}
        )

    preferred_worker = None
    if preferred_worker_id:
        from apps.worker.assignment_service import validate_preferred_worker
        preferred_worker = validate_preferred_worker(
            customer=customer, worker_id=preferred_worker_id,
            service=service, address=address,
        )
    validate_service_data(
        service.form_schema,
        service_data,
    )

    schedules = build_schedules(service.form_schema, service_data)
    _validate_schedules(schedules)
    _validate_payment_method(
        payment_method, form_schema=service.form_schema, sessions_count=len(schedules),
    )

    pricing_config = service.pricing_config or {}
    per_session = pricing_config.get('pricing_unit') == 'PER_SESSION'
    sessions_count = len(schedules)

    subtotal = None
    unit_price = None
    effective_unit_price = None
    package_discount_percent = Decimal('0')

    if is_computable_pricing(pricing_config):
        computed = calculate_booking_price(service, service_data)

        if computed is None:
            raise serializers.ValidationError(
                {
                    'service_data':
                    'Chưa đủ thông tin để tính giá '
                    'dịch vụ.'
                }
            )

        if per_session:
            # computed = giá gốc 1 buổi (base_prices[duration] + additional_services),
            # CHƯA trừ giảm giá gói — chỉ dùng để tính subtotal, KHÔNG dùng
            # để trả lương worker.
            unit_price = computed
            package_discount_percent = get_package_discount_percent(service, service_data)
            gross_subtotal = unit_price * sessions_count
            subtotal = _q(
                gross_subtotal * (Decimal('1') - package_discount_percent / Decimal('100'))
            )
            # Giá/buổi THẬT SỰ dùng để trả worker: đã trừ giảm giá gói,
            # chia đều trên subtotal (không dùng total vì total còn trừ
            # thêm voucher, voucher app gánh riêng, worker không chịu).
            effective_unit_price = (
                _q(subtotal / sessions_count) if sessions_count else unit_price
            )
        else:
            subtotal = computed

    discount = Decimal('0')
    user_voucher = None

    if voucher_code:
        if subtotal is None:
            raise serializers.ValidationError(
                {
                    'voucher_code':
                    'Dịch vụ này cần báo giá thủ công, '
                    'chưa thể áp dụng voucher.'
                }
            )

        result = validate_and_calculate_voucher(
            code=voucher_code,
            customer=customer,
            subtotal_amount=subtotal,
            lock=True,
            error_field='voucher_code',
        )

        user_voucher = result['user_voucher']
        discount = result['discount_amount']

    total = (
        _q(subtotal - discount)
        if subtotal is not None
        else None
    )

    if total is None:
        raise serializers.ValidationError(
            {
                'payment_method':
                'Booking chưa có giá thanh toán. '
                'Vui lòng chỉ chọn phương thức thanh toán '
                'sau khi dịch vụ có giá.'
            }
        )

    if total <= 0:
        raise serializers.ValidationError(
            {
                'payment_method':
                'Tổng tiền thanh toán phải lớn hơn 0.'
            }
        )

    booking_code = _generate_booking_code()

    booking = Booking.objects.create(
        booking_code=booking_code,
        customer=customer,
        service=service,
        user_voucher=user_voucher,
        service_data=service_data,
        address=address,
        delivery_address=delivery_address,
        note=note,
        status=Booking.Status.PENDING,

        payment_status=Booking.PaymentStatus.UNPAID,

        subtotal_amount=subtotal,
        discount_amount=discount,
        total_amount=total,

        price_breakdown={
            'unit_price': (
                str(effective_unit_price)
                if effective_unit_price is not None
                else (str(unit_price) if unit_price is not None else None)
            ),
            'base_unit_price': str(unit_price) if unit_price is not None else None,
            'sessions_count': sessions_count,
            'package_discount_percent': str(package_discount_percent),
            'subtotal_amount': (
                str(subtotal)
                if subtotal is not None
                else None
            ),
            'discount_amount': str(discount),
            'total_amount': (
                str(total)
                if total is not None
                else None
            ),
            'pricing_config_snapshot':
                service.pricing_config,
            'voucher': (
                {
                    'code': user_voucher.voucher.code,
                    'name': user_voucher.voucher.name,
                }
                if user_voucher
                else None
            ),
        },
    )

    BookingSchedule.objects.bulk_create([
        BookingSchedule(
            booking=booking,
            sequence_no=idx,
            scheduled_start=schedule['scheduled_start'],
            scheduled_end=schedule['scheduled_end'],
            preferred_worker=preferred_worker,
        )
        for idx, schedule in enumerate(schedules, start=1)
    ])

    payment = Payment.objects.create(
        customer=customer,
        booking=booking,
        amount=total,
        method=payment_method,
        status=Payment.Status.PENDING,
        transaction_code=(
            booking_code if payment_method == Payment.Method.BANK_TRANSFER else None
        ),
    )       

    if user_voucher:
        reserve_user_voucher(user_voucher_id=user_voucher.id)

    if payment_method == Payment.Method.WALLET:
        wallet_service.debit_wallet(
            user=customer, amount=total, type=WalletTransaction.Type.PAYMENT,
            booking=booking, note=f'Thanh toán đơn {booking_code}',
        )
        payment.status = Payment.Status.SUCCESS
        payment.paid_at = timezone.now()
        payment.save(update_fields=['status', 'paid_at', 'updated_at'])
        booking.payment_status = Booking.PaymentStatus.PAID
        booking.save(update_fields=['payment_status', 'updated_at'])
        if user_voucher:
            mark_user_voucher_used(user_voucher_id=user_voucher.id)

    # Ví / tiền mặt: đơn nhận việc được ngay -> bắt đầu thời gian ưu tiên.
    # Chuyển khoản: kích hoạt khi thanh toán xong (xem webhook).
    if preferred_worker and payment_method in (Payment.Method.CASH, Payment.Method.WALLET):
        from apps.worker.assignment_service import activate_preferred_worker_request
        activate_preferred_worker_request(booking)
        
    creator = actor or customer
    record_booking_activity(
        booking=booking,
        actor=creator,
        event_type=BookingActivity.EventType.BOOKING_CREATED,
        message=(
            f'Admin tạo đơn {booking.booking_code} thay khách hàng.'
            if actor is not None and actor != customer
            else f'Khách hàng tạo đơn {booking.booking_code}.'
        ),
        new_data={'customer_id': customer.id, 'service_id': service.id},
    )

    from apps.notifications.services import notify_booking_success
    transaction.on_commit(lambda: notify_booking_success(booking))
    return booking


@transaction.atomic
def cancel_booking_core(*, booking, actor, reason, by_admin=False):
    """Hủy mọi buổi CHƯA làm của đơn. Caller phải đã select_for_update booking."""
    who = 'Admin' if by_admin else 'Khách hàng'
    previous_status = booking.status

    if booking.status not in CANCELLABLE_STATUSES:
        raise serializers.ValidationError({'booking': 'Đơn hàng không thể hủy ở trạng thái hiện tại.'})
    if booking.schedules.filter(status=BookingSchedule.Status.IN_PROGRESS).exists():
        raise serializers.ValidationError({'booking': 'Đơn đang được thực hiện, không thể hủy.'})

    to_cancel = list(booking.schedules.select_for_update().exclude(status__in=_DONE_SCHEDULE_STATUSES))
    if not to_cancel:
        raise serializers.ValidationError({'booking': 'Đơn không còn buổi nào để hủy.'})

    has_completed = booking.schedules.filter(status=BookingSchedule.Status.COMPLETED).exists()
    now = timezone.now()

    # [Bug 4] Hủy phân công, nhả hoa hồng giữ chỗ, báo nhân viên
    assignments = list(
        BookingAssignment.objects.select_for_update(of=('self',)).filter(
            schedule__in=to_cancel,
            status__in=(BookingAssignment.Status.ACCEPTED, BookingAssignment.Status.PENDING),
        ).select_related('worker', 'schedule')
    )
    for assignment in assignments:
        was_accepted = assignment.status == BookingAssignment.Status.ACCEPTED
        assignment.status = BookingAssignment.Status.CANCELLED
        assignment.response_note = f'{who} hủy đơn.'
        assignment.responded_at = now
        assignment.save(update_fields=['status', 'response_note', 'responded_at', 'updated_at'])
        if was_accepted:
            earning_service.release_cash_commission(assignment=assignment)
            notify_worker_booking_cancelled(
                assignment.schedule, assignment.worker, by_admin=by_admin, reason=reason,
            )

    booking.status = Booking.Status.CANCELLED
    booking.cancelled_by = actor
    booking.cancelled_at = now
    booking.cancel_reason = reason
    booking.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'cancel_reason', 'updated_at'])

    BookingSchedule.objects.filter(pk__in=[s.pk for s in to_cancel]).update(
        status=BookingSchedule.Status.CANCELLED,
        cancelled_by=actor, cancelled_at=now, cancel_reason=reason, updated_at=now,
    )
    booking.payments.filter(status=Payment.Status.PENDING).update(
        status=Payment.Status.CANCELLED,
        failure_reason=f'Booking đã bị {who.lower()} hủy.',
        updated_at=now,
    )

    # [Bug 3, 7] Hoàn qua hàm idempotent. Đã làm xong buổi nào thì không hoàn buổi đó.
    if booking.payment_status == Booking.PaymentStatus.PAID:
        amount = (
            refund_service.per_session_refund_amount(booking) * len(to_cancel)
            if has_completed else None
        )
        refund_service.refund_booking(
            booking=booking, amount=amount, actor=actor,
            key=f'refund:booking:{booking.id}:cancel',
            note=f'Hoàn tiền hủy đơn {booking.booking_code}',
            reason='Admin đã hủy đơn.' if by_admin else 'Bạn đã hủy đơn.',
        )

    # Đã làm một phần thì không trả lại voucher
    if booking.user_voucher_id and not has_completed:
        release_user_voucher(user_voucher_id=booking.user_voucher_id, allow_used=True)

    record_booking_activity(
        booking=booking,
        actor=actor,
        event_type=BookingActivity.EventType.BOOKING_CANCELLED,
        message=f'{who} hủy đơn {booking.booking_code}.',
        old_data={'status': previous_status},
        new_data={'status': Booking.Status.CANCELLED, 'reason': reason, 'cancelled_schedules': len(to_cancel)},
    )
    return booking


@transaction.atomic
def cancel_booking(*, booking_id, customer, reason):
    booking = get_object_or_404(Booking.objects.select_for_update(), pk=booking_id, customer=customer)
    is_unpaid_online = (
        booking.payment_status == Booking.PaymentStatus.UNPAID
        and not booking.payments.filter(method=Payment.Method.CASH).exists()
    )
    if booking.schedules.count() > 1 and not is_unpaid_online:
        raise serializers.ValidationError({
            'booking': 'Đơn nhiều buổi không hủy cả đơn. Vui lòng hủy từng buổi.',
        })

    # Đơn chưa trả tiền online thì không có tiền để hoàn và sẽ tự hết hạn,
    # nên không áp giới hạn giờ. Các đơn còn lại phải hủy trước giờ làm 6 giờ.
    if not is_unpaid_online:
        earliest_start = (
            booking.schedules
            .filter(status=BookingSchedule.Status.PENDING)
            .order_by('scheduled_start')
            .values_list('scheduled_start', flat=True)
            .first()
        )
        if earliest_start and earliest_start - timezone.now() < timedelta(hours=CUSTOMER_MIN_CANCEL_HOURS):
            raise serializers.ValidationError({
                'booking': f'Chỉ hủy được đơn trước giờ bắt đầu ít nhất {CUSTOMER_MIN_CANCEL_HOURS} giờ.',
            })

    return cancel_booking_core(booking=booking, actor=customer, reason=reason)


@transaction.atomic
def cancel_schedule_by_customer(*, schedule_id, customer, reason):
    ref = get_object_or_404(
        BookingSchedule.objects.only('booking_id'), pk=schedule_id, booking__customer=customer,
    )
    booking = Booking.objects.select_for_update().get(pk=ref.booking_id)
    schedule = BookingSchedule.objects.select_for_update().get(pk=schedule_id)

    if booking.status not in CANCELLABLE_STATUSES:
        raise serializers.ValidationError({'schedule': 'Đơn hàng không thể hủy ở trạng thái hiện tại.'})
    if schedule.status != BookingSchedule.Status.PENDING:
        raise serializers.ValidationError({'schedule': 'Chỉ hủy được buổi chưa bắt đầu.'})
    if schedule.scheduled_start - timezone.now() < timedelta(hours=CUSTOMER_MIN_CANCEL_HOURS):
        raise serializers.ValidationError({
            'schedule': f'Chỉ hủy được buổi trước giờ bắt đầu ít nhất {CUSTOMER_MIN_CANCEL_HOURS} giờ.'
        })

    others_active = booking.schedules.filter(
        status__in=(BookingSchedule.Status.PENDING, BookingSchedule.Status.IN_PROGRESS),
    ).exclude(pk=schedule.pk).exists()

    if (
        others_active
        and booking.payment_status == Booking.PaymentStatus.UNPAID
        and booking.payments.filter(
            method=Payment.Method.BANK_TRANSFER, status=Payment.Status.PENDING,
        ).exists()
    ):
        raise serializers.ValidationError({
            'schedule': 'Đơn chưa thanh toán. Hãy hủy cả đơn hoặc chờ đơn hết hạn.',
        })

    now = timezone.now()

    assignments = list(
        BookingAssignment.objects.select_for_update(of=('self',)).filter(
            schedule=schedule,
            status__in=(BookingAssignment.Status.ACCEPTED, BookingAssignment.Status.PENDING),
        ).select_related('worker')
    )
    for assignment in assignments:
        was_accepted = assignment.status == BookingAssignment.Status.ACCEPTED
        assignment.status = BookingAssignment.Status.CANCELLED
        assignment.response_note = 'Khách hủy buổi.'
        assignment.responded_at = now
        assignment.save(update_fields=['status', 'response_note', 'responded_at', 'updated_at'])
        if was_accepted:
            earning_service.release_cash_commission(assignment=assignment)
            notify_worker_schedule_cancelled(schedule, assignment.worker)

    schedule.status = BookingSchedule.Status.CANCELLED
    schedule.cancelled_by = customer
    schedule.cancelled_at = now
    schedule.cancel_reason = reason
    schedule.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'cancel_reason', 'updated_at'])

    has_completed = booking.schedules.filter(status=BookingSchedule.Status.COMPLETED).exists()

    if booking.payment_status == Booking.PaymentStatus.PAID:
        amount = (
            None if (not others_active and not has_completed)
            else refund_service.per_session_refund_amount(booking)
        )
        refund_service.refund_booking(
            booking=booking, amount=amount, actor=customer,
            key=f'refund:schedule:{schedule.id}:customer_cancel',
            note=f'Hoàn tiền hủy buổi {schedule.sequence_no} - {booking.booking_code}',
            reason=f'Bạn đã hủy buổi {schedule.sequence_no}.',
        )

    record_booking_activity(
        booking=booking, schedule=schedule, actor=customer,
        event_type=BookingActivity.EventType.BOOKING_UPDATED,
        message=f'Khách hàng hủy buổi {schedule.sequence_no}.',
        new_data={'reason': reason},
    )

    if others_active:
        from apps.worker.assignment_service import _sync_booking_status_after_claim
        _sync_booking_status_after_claim(booking)
        return schedule

    booking.payments.filter(status=Payment.Status.PENDING).update(
        status=Payment.Status.CANCELLED, failure_reason='Khách hàng đã hủy.', updated_at=now,
    )
    if has_completed:
        booking.status = Booking.Status.COMPLETED
        booking.save(update_fields=['status', 'updated_at'])
    else:
        booking.status = Booking.Status.CANCELLED
        booking.cancelled_by = customer
        booking.cancelled_at = now
        booking.cancel_reason = reason
        booking.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'cancel_reason', 'updated_at'])
        if booking.user_voucher_id:
            release_user_voucher(user_voucher_id=booking.user_voucher_id, allow_used=True)
    return schedule