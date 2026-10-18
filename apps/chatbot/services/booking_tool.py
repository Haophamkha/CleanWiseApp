import logging
import os
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import Http404
from django.utils import timezone
from rest_framework import serializers

from apps.addresses.models import CustomerAddress
from apps.bookings import booking_service
from apps.bookings.models import Booking, BookingSchedule
from apps.bookings.schedule_builder import build_schedules
from apps.bookings.service_data_validation import validate_service_data
from apps.payments.models import Payment
from apps.payments.payment_link_service import create_payos_payment_link
from apps.services.models import Service
from apps.vouchers.models import UserVoucher
from apps.vouchers.voucher_service import validate_and_calculate_voucher
from apps.wallets import refund_service
from apps.wallets.models import Wallet, WalletTransaction
from apps.wallets.wallet_service import get_or_create_wallet

from .queries import ToolQueryError, local_time, resolve_booking

logger = logging.getLogger(__name__)

DRAFT_TTL = 600
WALLET_PAGE_SIZE = 5
PAYMENT_LABELS = {'CASH': 'Tiền mặt', 'BANK_TRANSFER': 'Chuyển khoản', 'WALLET': 'Ví CleanWise'}


def _flatten(detail):
    if isinstance(detail, dict):
        return '; '.join(f'{k}: {_flatten(v)}' for k, v in detail.items())
    if isinstance(detail, (list, tuple)):
        return '; '.join(_flatten(v) for v in detail)
    return str(detail)


def _conversation_id(thread_id):
    # thread_id = cleanwise-chatbot:{conversation_id}:{run_id}:{attempt_id}
    return thread_id.split(':')[1]


def _draft_key(customer_id, thread_id):
    return f'chatbot:booking-draft:{customer_id}:{_conversation_id(thread_id)}'


def _cancel_key(customer_id, thread_id):
    return f'chatbot:cancel-draft:{customer_id}:{_conversation_id(thread_id)}'


def _money(amount):
    return f'{amount:,.0f}đ'.replace(',', '.')


def _fmt(dt):
    return timezone.localtime(dt).strftime('%H:%M, ngày %d/%m/%Y')


def _full_address(a):
    return ', '.join(part for part in (a.address_line, a.ward, a.city) if part)


def _worker_name(worker):
    return worker.get_full_name() or worker.username


def _get_customer(customer_id):
    return get_user_model().objects.get(pk=customer_id)


# ---------------------------------------------------------------- tra cứu

def list_my_addresses(customer_id):
    rows = CustomerAddress.objects.filter(customer_id=customer_id, is_active=True).order_by('-is_default', '-id')
    return {'results': [{'id': a.id, 'label': a.label, 'receiver_name': a.receiver_name,
                         'address_line': a.address_line, 'ward': a.ward, 'city': a.city,
                         'is_default': a.is_default} for a in rows[:10]]}


def list_my_vouchers(customer_id):
    now = timezone.now()
    rows = (UserVoucher.objects
            .filter(user_id=customer_id, status=UserVoucher.Status.AVAILABLE, voucher__is_active=True,
                    voucher__start_at__lte=now, voucher__end_at__gte=now)
            .select_related('voucher').order_by('voucher__end_at')[:10])
    return {'results': [{
        'code': uv.voucher.code, 'name': uv.voucher.name, 'description': (uv.voucher.description or '')[:200],
        'discount_type': uv.voucher.discount_type, 'discount_value': str(uv.voucher.discount_value),
        'max_discount_amount': str(uv.voucher.max_discount_amount) if uv.voucher.max_discount_amount else None,
        'min_order_amount': str(uv.voucher.min_order_amount), 'expires_at': local_time(uv.voucher.end_at),
    } for uv in rows],
        'note': 'Chỉ gồm voucher đã có trong ví của khách. Muốn nhận thêm voucher, khách làm trong ứng dụng.'}


def list_favorite_workers(customer_id, service_id, address_id):
    from apps.worker import favorite_worker_service
    rows = favorite_worker_service.list_favorite_workers(
        customer=_get_customer(customer_id), service_id=service_id, address_id=address_id)
    return {'results': [{'id': fav.worker_id, 'name': _worker_name(fav.worker)} for fav in list(rows)[:10]],
            'note': 'Chỉ gồm nhân viên yêu thích phù hợp dịch vụ và khu vực của địa chỉ đã chọn.'}


def get_wallet(customer_id, page=1):
    wallet = Wallet.objects.filter(user_id=customer_id).first()
    qs = (WalletTransaction.objects.filter(wallet__user_id=customer_id)
          .select_related('booking').order_by('-created_at', '-id'))
    count = qs.count()
    start = (page - 1) * WALLET_PAGE_SIZE
    results = [{
        'type': t.get_type_display(), 'direction': t.direction, 'amount': str(t.amount),
        'balance_after': str(t.balance_after), 'status': t.get_status_display(),
        'booking_code': t.booking.booking_code if t.booking else None,
        'note': (t.note or '')[:120], 'created_at': local_time(t.created_at),
    } for t in qs[start:start + WALLET_PAGE_SIZE]]
    return {'balance': str(wallet.balance) if wallet else '0', 'results': results, 'count': count,
            'page': page, 'has_next': start + WALLET_PAGE_SIZE < count,
            'observed_at': local_time(timezone.now())}


def get_payment_link(customer_id, booking_reference):
    booking = resolve_booking(customer_id, booking_reference)
    payment = Payment.objects.filter(
        booking=booking, booking__status=Booking.Status.PENDING,
        booking__payment_status=Booking.PaymentStatus.UNPAID,
        method=Payment.Method.BANK_TRANSFER, status=Payment.Status.PENDING).first()
    if not payment:
        raise ToolQueryError('Đơn này không có khoản chuyển khoản đang chờ (có thể đã thanh toán, đã hủy hoặc hết hạn).')
    try:
        link = create_payos_payment_link(
            payment,
            return_url=f'{settings.PAYOS_RETURN_URL}?bookingId={booking.id}',
            cancel_url=f'{settings.PAYOS_CANCEL_URL}?bookingId={booking.id}')
    except serializers.ValidationError as exc:
        raise ToolQueryError(_flatten(exc.detail)) from exc
    except Exception:
        logger.exception('Chatbot tạo link payOS lỗi (booking %s)', booking.id)
        raise ToolQueryError('Chưa tạo được link thanh toán lúc này. Hãy mở đơn trong ứng dụng để thanh toán.')
    return {'booking_id': booking.id, 'booking_code': booking.booking_code,
            'checkout_url': link['checkout_url'], 'amount': link['amount'], 'expires_at': link['expires_at'],
            'bank_bin': link['bank_bin'], 'account_number': link['account_number'],
            'account_name': link['account_name'], 'transfer_content': link['transfer_content'],
            'note': 'Link chỉ có hiệu lực 5 phút (hết hạn có thể lấy link mới). Đơn tự hủy nếu chưa trả trong 30 phút từ lúc tạo.'}


def get_reorder_template(customer_id, booking_reference):
    booking = resolve_booking(customer_id, booking_reference)
    service = booking.service
    if not service.is_active:
        raise ToolQueryError('Dịch vụ của đơn này không còn hoạt động.')

    def active_address(address_id):
        if not address_id:
            return None
        return CustomerAddress.objects.filter(pk=address_id, customer_id=customer_id, is_active=True).first()

    address = active_address(booking.address_id)
    delivery = active_address(booking.delivery_address_id)
    return {
        'service_id': service.id, 'service_name': service.name, 'service_data': booking.service_data,
        'address_id': address.id if address else None, 'address_label': address.label if address else None,
        'delivery_address_id': delivery.id if delivery else None,
        'note': booking.note,
        'instruction': 'Giữ các lựa chọn cũ nhưng PHẢI hỏi khách ngày giờ MỚI (đối chiếu key ngày/giờ trong form_schema '
                       'của get_service_details), không dùng lại ngày cũ. Địa chỉ cũ chỉ là gợi ý, vẫn hỏi khách xác nhận.',
    }


# ---------------------------------------------------------------- đặt lịch

def prepare_booking(customer_id, thread_id, service_id, service_data, address_id,
                    delivery_address_id, payment_method, note, voucher_code='', preferred_worker_id=None):
    service = Service.objects.filter(pk=service_id, is_active=True).first()
    if not service:
        raise ToolQueryError('Không tìm thấy dịch vụ đang hoạt động.')
    if payment_method not in PAYMENT_LABELS:
        raise ToolQueryError('Phương thức thanh toán không hợp lệ.')
    address = CustomerAddress.objects.filter(pk=address_id, customer_id=customer_id, is_active=True).first()
    if not address:
        raise ToolQueryError('Không tìm thấy địa chỉ trong tài khoản của khách.')
    delivery = None
    need_delivery = (service.form_schema or {}).get('address_count', 1) >= 2
    if need_delivery:
        delivery = CustomerAddress.objects.filter(pk=delivery_address_id, customer_id=customer_id,
                                                  is_active=True).first() if delivery_address_id else None
        if not delivery:
            raise ToolQueryError('Dịch vụ này cần thêm địa chỉ chuyển đến (delivery_address_id).')
    elif delivery_address_id:
        raise ToolQueryError('Dịch vụ này không cần địa chỉ chuyển đến.')

    customer = _get_customer(customer_id)

    worker = None
    if preferred_worker_id:
        from apps.worker.assignment_service import validate_preferred_worker
        try:
            worker = validate_preferred_worker(customer=customer, worker_id=preferred_worker_id,
                                               service=service, address=address)
        except serializers.ValidationError as exc:
            raise ToolQueryError(_flatten(exc.detail)) from exc

    try:
        validate_service_data(service.form_schema, service_data)
        schedules = build_schedules(service.form_schema, service_data)
        booking_service._validate_schedules(schedules)
        booking_service._validate_payment_method(
            getattr(Payment.Method, payment_method), form_schema=service.form_schema,
            sessions_count=len(schedules))
    except serializers.ValidationError as exc:
        raise ToolQueryError(_flatten(exc.detail)) from exc

    pricing = service.pricing_config or {}
    if not booking_service.is_computable_pricing(pricing):
        raise ToolQueryError('Dịch vụ cần báo giá thủ công, vui lòng đặt trong ứng dụng.')
    unit = booking_service.calculate_booking_price(service, service_data)
    if unit is None:
        raise ToolQueryError('Chưa đủ thông tin để tính giá dịch vụ.')
    sessions = len(schedules)
    discount_pct = Decimal('0')
    subtotal = unit
    if pricing.get('pricing_unit') == 'PER_SESSION':
        discount_pct = booking_service.get_package_discount_percent(service, service_data)
        subtotal = booking_service._q(unit * sessions * (Decimal('1') - discount_pct / Decimal('100')))
    if subtotal <= 0:
        raise ToolQueryError('Tổng tiền không hợp lệ, vui lòng đặt trong ứng dụng.')

    voucher_code = (voucher_code or '').strip().upper()
    discount = Decimal('0')
    total = subtotal
    if voucher_code:
        try:
            checked = validate_and_calculate_voucher(
                code=voucher_code, customer=customer, subtotal_amount=subtotal, error_field='voucher_code')
        except serializers.ValidationError as exc:
            raise ToolQueryError(_flatten(exc.detail)) from exc
        discount = checked['discount_amount']
        total = booking_service._q(subtotal - discount)
        if total <= 0:
            raise ToolQueryError('Tổng tiền sau voucher không hợp lệ, vui lòng chọn voucher khác hoặc đặt trong ứng dụng.')

    # Báo sớm nếu ví không đủ; việc trừ tiền thật vẫn nằm trong create_booking.
    if payment_method == 'WALLET':
        wallet, _ = get_or_create_wallet(customer)
        if wallet.balance < total:
            raise ToolQueryError(
                f'Số dư ví không đủ, còn thiếu {_money(total - wallet.balance)}. '
                'Đề nghị khách chọn chuyển khoản (hoặc tiền mặt nếu dịch vụ một buổi), '
                'hoặc nạp ví trong ứng dụng.')

    # Mỗi hội thoại chỉ giữ MỘT bản nháp mới nhất.
    cache.set(_draft_key(customer_id, thread_id), {
        'customer_id': customer_id, 'thread_id': thread_id, 'service_id': service.id,
        'service_data': service_data, 'address_id': address.id,
        'delivery_address_id': delivery.id if delivery else None,
        'payment_method': payment_method, 'note': note or None,
        'voucher_code': voucher_code or None,
        'preferred_worker_id': worker.id if worker else None}, DRAFT_TTL)
    logger.warning('[DRAFT] set key=%s pid=%s backend=%s', _draft_key(customer_id, thread_id),
                   os.getpid(), settings.CACHES['default']['BACKEND'])
    logger.warning('[DRAFT] set key=%s pid=%s backend=%s', _draft_key(customer_id, thread_id),
                   os.getpid(), settings.CACHES['default']['BACKEND'])

    first_start = _fmt(schedules[0]['scheduled_start'])
    last_start = _fmt(schedules[-1]['scheduled_start'])
    when = first_start if sessions == 1 else f'{sessions} buổi, từ {first_start} đến {last_start}'
    lines = [
        f'Dịch vụ: {service.name}',
        f'Thời gian: {when}',
        f'Địa chỉ: {_full_address(address)}',
    ]
    if delivery:
        lines.append(f'Địa chỉ chuyển đến: {_full_address(delivery)}')
    if worker:
        lines.append(f'Nhân viên ưu tiên: {_worker_name(worker)}')
    if discount_pct:
        lines.append(f'Giảm giá gói: {discount_pct}%')
    if voucher_code:
        lines.append(f'Tạm tính: {_money(subtotal)}')
        lines.append(f'Voucher {voucher_code}: -{_money(discount)}')
    lines.append(f'Tổng tiền: {_money(total)}')
    lines.append(f'Thanh toán: {PAYMENT_LABELS[payment_method]}')
    if note:
        lines.append(f'Ghi chú: {note}')
    if worker and payment_method == 'BANK_TRANSFER':
        lines.append('Lưu ý: yêu cầu nhân viên chỉ được gửi sau khi bạn thanh toán xong.')

    return {
        'created_booking': False,
        'confirmation_text': '\n'.join(lines),
        'service_name': service.name, 'sessions_count': sessions,
        'first_session_start': first_start, 'last_session_start': last_start,
        'address': _full_address(address),
        'delivery_address': _full_address(delivery) if delivery else None,
        'payment_method': PAYMENT_LABELS[payment_method],
        'voucher_code': voucher_code or None, 'discount_amount': str(discount),
        'preferred_worker': _worker_name(worker) if worker else None,
        'package_discount_percent': str(discount_pct), 'total_amount': str(total),
        'note': 'ĐÂY CHỈ LÀ BẢN NHÁP, CHƯA TẠO ĐƠN. Bạn PHẢI hiển thị NGUYÊN VĂN confirmation_text '
                'cho khách (không rút gọn, không tự viết lại), rồi hỏi "Bạn xác nhận đặt không?" và dừng. '
                'Chỉ gọi confirm_booking ở tin nhắn sau khi khách đồng ý.',
    }


def confirm_booking(customer_id, thread_id):
    key = _draft_key(customer_id, thread_id)
    draft = cache.get(key)
    logger.warning('[DRAFT] get key=%s found=%s pid=%s', key, bool(draft), os.getpid())
    if not draft or draft['customer_id'] != customer_id:
        raise ToolQueryError(
            'Chưa có bản nháp cho đơn này. KHÔNG xin lỗi khách và KHÔNG hỏi lại thông tin đã có. '
            'Hãy gọi prepare_booking ngay với đúng dữ liệu đã thống nhất, hiển thị nguyên văn '
            'confirmation_text và hỏi khách xác nhận.')
    if draft['thread_id'] == thread_id:
        raise ToolQueryError('Chưa có xác nhận của khách. Hãy tóm tắt, hỏi khách và chờ tin nhắn tiếp theo.')
    if not cache.delete(key):  # chỉ một lần xác nhận thắng
        raise ToolQueryError('Bản nháp này đã được xử lý.')
    customer = _get_customer(customer_id)
    try:
        booking = booking_service.create_booking(
            customer=customer, service_id=draft['service_id'], address_id=draft['address_id'],
            delivery_address_id=draft['delivery_address_id'], service_data=draft['service_data'],
            note=draft['note'], voucher_code=draft.get('voucher_code'),
            preferred_worker_id=draft.get('preferred_worker_id'),
            payment_method=getattr(Payment.Method, draft['payment_method']))
    except serializers.ValidationError as exc:
        cache.set(key, draft, DRAFT_TTL)  # trả lại nháp để khách sửa/thử lại
        raise ToolQueryError(_flatten(exc.detail)) from exc
    except Http404 as exc:
        cache.set(key, draft, DRAFT_TTL)
        raise ToolQueryError('Dịch vụ hoặc địa chỉ không còn khả dụng.') from exc
    next_step = ('Đơn chuyển khoản chưa có hiệu lực cho đến khi khách thanh toán. Hỏi khách có muốn lấy link/thông tin '
                 'chuyển khoản ngay không (get_payment_link).'
                 if draft['payment_method'] == 'BANK_TRANSFER' else '')
    return {'id': booking.id, 'booking_code': booking.booking_code, 'status': booking.status,
            'payment_status': booking.payment_status, 'total_amount': str(booking.total_amount),
            'discount_amount': str(booking.discount_amount),
            'payment_method': PAYMENT_LABELS[draft['payment_method']],
            'next_step': next_step}
# ---------------------------------------------------------------- hủy đơn / hủy buổi

def prepare_cancel(customer_id, thread_id, booking_reference, schedule_no, reason):
    booking = resolve_booking(customer_id, booking_reference)
    if booking.status not in booking_service.CANCELLABLE_STATUSES:
        raise ToolQueryError('Đơn ở trạng thái hiện tại không thể hủy.')

    now = timezone.now()
    min_gap = timedelta(hours=booking_service.CUSTOMER_MIN_CANCEL_HOURS)
    S = BookingSchedule.Status
    schedules = list(booking.schedules.order_by('sequence_no', 'id'))
    has_completed = any(s.status == S.COMPLETED for s in schedules)
    paid = booking.payment_status == Booking.PaymentStatus.PAID
    remaining = (booking.total_amount or Decimal('0')) - (booking.refunded_amount or Decimal('0'))
    per_session = refund_service.per_session_refund_amount(booking)

    if schedule_no is None:
        if any(s.status == S.IN_PROGRESS for s in schedules):
            raise ToolQueryError('Đơn đang được thực hiện, không thể hủy.')
        to_cancel = [s for s in schedules if s.status not in booking_service._DONE_SCHEDULE_STATUSES]
        if not to_cancel:
            raise ToolQueryError('Đơn không còn buổi nào để hủy.')
        is_unpaid_online = (booking.payment_status == Booking.PaymentStatus.UNPAID
                            and not booking.payments.filter(method=Payment.Method.CASH).exists())
        if len(schedules) > 1 and not is_unpaid_online:
            raise ToolQueryError('Đơn nhiều buổi không hủy cả đơn. Hãy hỏi khách muốn hủy buổi số mấy (hủy từng buổi).')
        if not is_unpaid_online:
            pending = [s for s in schedules if s.status == S.PENDING]
            if pending and min(s.scheduled_start for s in pending) - now < min_gap:
                raise ToolQueryError(f'Chỉ hủy được đơn trước giờ bắt đầu ít nhất {booking_service.CUSTOMER_MIN_CANCEL_HOURS} giờ.')
        refund = (per_session * len(to_cancel) if has_completed else remaining) if paid else Decimal('0')
        schedule_id, scope = None, f'Hủy cả đơn {booking.booking_code}'
    else:
        schedule = next((s for s in schedules if s.sequence_no == schedule_no), None)
        if not schedule:
            raise ToolQueryError(f'Đơn không có buổi số {schedule_no}.')
        if schedule.status != S.PENDING:
            raise ToolQueryError('Chỉ hủy được buổi chưa bắt đầu.')
        if schedule.scheduled_start - now < min_gap:
            raise ToolQueryError(f'Chỉ hủy được buổi trước giờ bắt đầu ít nhất {booking_service.CUSTOMER_MIN_CANCEL_HOURS} giờ.')
        others_active = any(s.id != schedule.id and s.status in (S.PENDING, S.IN_PROGRESS) for s in schedules)
        if (others_active and booking.payment_status == Booking.PaymentStatus.UNPAID
                and booking.payments.filter(method=Payment.Method.BANK_TRANSFER,
                                            status=Payment.Status.PENDING).exists()):
            raise ToolQueryError('Đơn chưa thanh toán. Hãy hủy cả đơn hoặc chờ đơn hết hạn.')
        refund = (remaining if (not others_active and not has_completed) else per_session) if paid else Decimal('0')
        schedule_id = schedule.id
        scope = f'Hủy buổi {schedule.sequence_no} ({_fmt(schedule.scheduled_start)}) của đơn {booking.booking_code}'

    refund = max(min(refund, remaining), Decimal('0'))
    reason = reason.strip()[:300]
    cache.set(_cancel_key(customer_id, thread_id), {
        'customer_id': customer_id, 'thread_id': thread_id, 'booking_id': booking.id,
        'schedule_id': schedule_id, 'reason': reason}, DRAFT_TTL)

    lines = [scope, f'Lý do: {reason}']
    if refund > 0:
        lines.append(f'Hoàn tiền dự kiến: {_money(refund)} vào ví CleanWise')
    else:
        lines.append('Không có hoàn tiền (đơn chưa thanh toán trước).')
    lines.append('Việc hủy không thể hoàn tác.')
    return {'cancelled': False, 'confirmation_text': '\n'.join(lines),
            'note': 'ĐÂY CHỈ LÀ BẢN NHÁP, CHƯA HỦY. Hỏi "Bạn xác nhận hủy không?" rồi dừng; '
                    'chỉ gọi confirm_cancel ở tin nhắn sau khi khách đồng ý.'}


def confirm_cancel(customer_id, thread_id):
    from apps.common.distributed_lock import distributed_lock
    key = _cancel_key(customer_id, thread_id)
    draft = cache.get(key)
    if not draft or draft['customer_id'] != customer_id:
        raise ToolQueryError('Không có yêu cầu hủy đang chờ hoặc đã hết hạn. Hãy chuẩn bị lại.')
    if draft['thread_id'] == thread_id:
        raise ToolQueryError('Chưa có xác nhận của khách. Hãy tóm tắt, hỏi khách và chờ tin nhắn tiếp theo.')
    if not cache.delete(key):
        raise ToolQueryError('Yêu cầu hủy này đã được xử lý.')
    customer = _get_customer(customer_id)
    try:
        if draft['schedule_id']:
            with distributed_lock(f"schedule:{draft['schedule_id']}"):
                schedule = booking_service.cancel_schedule_by_customer(
                    schedule_id=draft['schedule_id'], customer=customer, reason=draft['reason'])
            booking = Booking.objects.get(pk=schedule.booking_id)
        else:
            with distributed_lock(f"booking:{draft['booking_id']}"):
                booking = booking_service.cancel_booking(
                    booking_id=draft['booking_id'], customer=customer, reason=draft['reason'])
    except serializers.ValidationError as exc:
        raise ToolQueryError(_flatten(exc.detail)) from exc
    except Http404 as exc:
        raise ToolQueryError('Không tìm thấy đơn hoặc buổi cần hủy.') from exc
    return {'id': booking.id, 'booking_code': booking.booking_code, 'status': booking.status,
            'payment_status': booking.payment_status, 'refunded_amount': str(booking.refunded_amount)}