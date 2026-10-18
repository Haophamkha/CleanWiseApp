"""Small read-only projections; model input never determines the actor."""
import json
import unicodedata
from datetime import date, datetime, time, timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, Prefetch, Q
from django.utils import timezone

from apps.bookings.models import Booking, BookingSchedule
from apps.payments.models import Payment
from apps.services.models import Service
from apps.worker.models import BookingAssignment

PAGE_SIZE = 5
SCHEDULE_PAGE_SIZE = 10


class ToolQueryError(ValueError):
    pass


def local_time(value):
    return timezone.localtime(value).isoformat() if value else None


def normalized(value):
    value = value.lower().replace('đ', 'd')
    return ''.join(c for c in unicodedata.normalize('NFD', value) if not unicodedata.combining(c))


def search_services(query='', section_code='', page=1):
    qs = Service.objects.filter(is_active=True).order_by('id')
    if section_code:
        qs = qs.filter(section_code__iexact=section_code)
    # Accent-insensitive matching without requiring a database extension.
    candidates = list(qs.values('id', 'code', 'name', 'section_code', 'description')[:501])
    truncated = len(candidates) > 500
    candidates = candidates[:500]
    stop_words = {
        'toi', 'muon', 'can', 'tim', 'dat', 'dich', 'vu', 'gia', 'bao', 'nhieu',
        'nao', 'giup', 'cho', 'minh', 'cua', 'la', 'co', 've', 'va', 'mot',
        'tai', 'duoc', 'khong', 'xin', 'hay', 'xem', 'loai',
    }
    tokens = [token for token in normalized(query).split() if token not in stop_words]
    scored = []
    for item in candidates:
        haystack = normalized(' '.join(str(item[k]) for k in ('code', 'name', 'description')))
        matched = sum(token in haystack for token in tokens)
        # A service must match every meaningful term; matching only a generic
        # word such as "vệ sinh" must not surface unrelated catalog entries.
        if tokens and matched != len(tokens):
            continue
        score = matched
        item['description'] = item['description'][:350]
        scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1]['id']))
    start = (page - 1) * PAGE_SIZE
    results = [item for _, item in scored[start:start + PAGE_SIZE]]
    return {'results': results, 'count': len(scored), 'page': page,
            'has_next': start + PAGE_SIZE < len(scored), 'catalog_truncated': truncated}


def get_service_details(service_id):
    service = Service.objects.filter(pk=service_id, is_active=True).first()
    if not service:
        raise ToolQueryError('Không tìm thấy dịch vụ đang hoạt động.')
    payload = {'id': service.id, 'code': service.code, 'name': service.name,
               'description': service.description[:6000], 'section_code': service.section_code,
               'form_schema': service.form_schema, 'pricing_config': service.pricing_config,
               'pricing_note': 'Bảng giá cấu hình; không phải báo giá cuối cùng. Chưa áp dụng voucher hoặc tổng số buổi.'}
    if len(json.dumps(payload, cls=DjangoJSONEncoder, ensure_ascii=False)) > 30000:
        raise ToolQueryError('Thông tin dịch vụ quá dài; vui lòng mở màn hình chi tiết dịch vụ.')
    return payload


def owned_bookings(customer_id):
    return Booking.objects.filter(customer_id=customer_id).select_related('service')


def resolve_booking(customer_id, booking_reference):
    reference = str(booking_reference).strip()
    qs = owned_bookings(customer_id)
    booking = qs.filter(booking_code__iexact=reference).first()
    if booking is None and reference.isdecimal() and len(reference) <= 18:
        booking = qs.filter(pk=int(reference)).first()
    if booking is None:
        raise ToolQueryError('Không tìm thấy đơn trong tài khoản của bạn. Vui lòng kiểm tra lại mã đơn.')
    return booking


def booking_summary(booking):
    return {'id': booking.id, 'booking_code': booking.booking_code,
            'service_name': booking.service.name, 'status': booking.status,
            'status_label': booking.get_status_display(), 'payment_status': booking.payment_status,
            'payment_status_label': booking.get_payment_status_display(),
            'total_amount': str(booking.total_amount) if booking.total_amount is not None else None,
            'created_at': local_time(booking.created_at)}


def list_my_bookings(customer_id, status='', date_from='', date_to='', page=1,
                     payment_status=None, service_query='', limit=PAGE_SIZE):
    qs = owned_bookings(customer_id)
    if type(limit) is not int or not 1 <= limit <= PAGE_SIZE or type(page) is not int or page < 1:
        raise ToolQueryError('Số lượng hoặc trang đơn không hợp lệ.')
    if payment_status:
        if payment_status not in Booking.PaymentStatus.values:
            raise ToolQueryError('Trạng thái thanh toán không hợp lệ.')
        qs = qs.filter(payment_status=payment_status)
    if service_query.strip():
        tokens = normalized(service_query).split()
        service_ids = [item['id'] for item in Service.objects.values('id', 'name', 'code')
                       if all(token in normalized(item['name'] + ' ' + item['code']) for token in tokens)]
        qs = qs.filter(service_id__in=service_ids)
    if status:
        if status not in Booking.Status.values:
            raise ToolQueryError('Trạng thái đơn không hợp lệ.')
        qs = qs.filter(status=status)
    try:
        start_date = date.fromisoformat(date_from) if date_from else None
        end_date = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        raise ToolQueryError('Ngày phải có định dạng YYYY-MM-DD hợp lệ.') from exc
    if start_date and end_date and start_date > end_date:
        raise ToolQueryError('Ngày bắt đầu không được sau ngày kết thúc.')
    if start_date or end_date:
        schedules = BookingSchedule.objects.all()
        if start_date:
            start = timezone.make_aware(datetime.combine(start_date, time.min))
            schedules = schedules.filter(scheduled_start__gte=start)
        if end_date:
            try:
                end = timezone.make_aware(datetime.combine(end_date + timedelta(days=1), time.min))
            except OverflowError as exc:
                raise ToolQueryError('Ngày kết thúc nằm ngoài phạm vi hỗ trợ.') from exc
            schedules = schedules.filter(scheduled_start__lt=end)
        qs = qs.filter(pk__in=schedules.values('booking_id'))
    count = qs.count()
    start = (page - 1) * limit
    results = [booking_summary(b) for b in qs.order_by('-created_at', '-id')[start:start + limit]]
    return {'results': results, 'count': count, 'page': page, 'has_next': start + limit < count,
            'timezone': str(timezone.get_current_timezone()), 'observed_at': local_time(timezone.now())}


def schedule_queryset(booking):
    assignments = BookingAssignment.objects.filter(status=BookingAssignment.Status.ACCEPTED).select_related('worker')
    return booking.schedules.prefetch_related(Prefetch('assignments', queryset=assignments)).order_by('sequence_no', 'id')


def schedule_summary(schedule):
    assignment = next(iter(schedule.assignments.all()), None)
    worker = None
    if assignment:
        worker = {'id': assignment.worker_id,
                  'name': assignment.worker.get_full_name() or 'Nhân viên CleanWise'}
    return {'id': schedule.id, 'sequence_no': schedule.sequence_no, 'status': schedule.status,
            'status_label': schedule.get_status_display(), 'scheduled_start': local_time(schedule.scheduled_start),
            'scheduled_end': local_time(schedule.scheduled_end), 'actual_start': local_time(schedule.actual_start),
            'actual_end': local_time(schedule.actual_end), 'worker': worker,
            'has_accepted_worker': bool(assignment), 'has_checked_in': schedule.actual_start is not None}


def get_my_booking_schedules(customer_id, booking_reference, page=1):
    booking = resolve_booking(customer_id, booking_reference)
    qs = schedule_queryset(booking)
    count = qs.count()
    start = (page - 1) * SCHEDULE_PAGE_SIZE
    return {'booking_id': booking.id, 'booking_code': booking.booking_code,
            'results': [schedule_summary(s) for s in qs[start:start + SCHEDULE_PAGE_SIZE]],
            'count': count, 'page': page, 'has_next': start + SCHEDULE_PAGE_SIZE < count,
            'timezone': str(timezone.get_current_timezone()), 'observed_at': local_time(timezone.now())}


def get_my_booking_detail(customer_id, booking_reference):
    booking = resolve_booking(customer_id, booking_reference)
    data = booking_summary(booking)
    data.update({key: str(getattr(booking, key)) if getattr(booking, key) is not None else None
                 for key in ('subtotal_amount', 'discount_amount', 'refunded_amount')})
    data['schedule_counts'] = list(booking.schedules.values('status').annotate(count=Count('id')).order_by('status'))
    data['remaining_sessions'] = booking.schedules.filter(status__in=['PENDING', 'IN_PROGRESS']).count()
    next_session = schedule_queryset(booking).filter(
        Q(status='IN_PROGRESS') | Q(status='PENDING', scheduled_start__gte=timezone.now())
    ).order_by('scheduled_start', 'id').first()
    data['next_session'] = schedule_summary(next_session) if next_session else None
    data['schedules'] = [schedule_summary(s) for s in schedule_queryset(booking)[:SCHEDULE_PAGE_SIZE]]
    data['schedules_has_next'] = booking.schedules.count() > SCHEDULE_PAGE_SIZE
    payment = Payment.objects.filter(booking=booking, customer_id=customer_id).order_by('-created_at', '-id').first()
    data['latest_payment'] = None if not payment else {
        'method': payment.method, 'method_label': payment.get_method_display(),
        'status': payment.status, 'status_label': payment.get_status_display(),
        'amount': str(payment.amount), 'paid_at': local_time(payment.paid_at),
    }
    data['timezone'] = str(timezone.get_current_timezone())
    data['observed_at'] = local_time(timezone.now())
    return data
