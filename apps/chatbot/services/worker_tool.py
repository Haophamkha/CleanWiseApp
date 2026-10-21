import logging
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.utils import timezone
from rest_framework import serializers

from apps.authentication.models import WorkerProfile
from apps.authentication.worker_profile import get_worker_profile_completeness
from apps.bookings.models import Booking, BookingSchedule, BookingScheduleImage
from apps.common.distributed_lock import distributed_lock
from apps.complaints.models import Complaint, ComplaintIssueType
from apps.complaints.serializers import AUTO_CANCEL_COMPLAINT_HOURS, ComplaintCreateSerializer
from apps.notifications.realtime import push_complaint_changed
from apps.reviews.models import Review
from apps.reviews.review_service import worker_review_summary
from apps.wallets import earning_service, wallet_service
from apps.wallets.models import Wallet, WithdrawRequest, WorkerEarning
from apps.wallets.worker_earnings import _range_bounds, _resolve_period, _sum
from apps.worker import checkin_service
from apps.worker.assignment_service import claim_booking_package, decline_preferred_request
from apps.worker.constants import AUTO_CANCEL_NOTE
from apps.worker.invitation_service import respond_invitation
from apps.worker.models import BookingAssignment

from .booking_tool import _conversation_id, _flatten, _fmt, _full_address, _money, get_wallet
from .queries import ToolQueryError, local_time

logger = logging.getLogger(__name__)

DRAFT_TTL = 600
PAGE_SIZE = 5
PAYMENT_LABEL = {'CASH': 'Khách trả tiền mặt (nhân viên thu, hệ thống giữ hoa hồng)',
                 'ONLINE': 'Khách đã thanh toán online'}


def _lt(dt):
    return local_time(dt) if dt else None


def _get_worker(worker_id):
    return get_user_model().objects.get(pk=worker_id, role='WORKER')


def _parse_day(value, field):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise ToolQueryError(f'{field} phải có dạng YYYY-MM-DD.')


def _take_draft(key, worker_id, thread_id, what):
    """Cùng mẫu với bên khách: bản nháp chỉ được xác nhận ở lượt SAU lượt tạo nháp."""
    draft = cache.get(key)
    if not draft or draft['worker_id'] != worker_id:
        raise ToolQueryError(f'Không có {what} đang chờ hoặc đã hết hạn. Hãy chuẩn bị lại.')
    if draft['thread_id'] == thread_id:
        raise ToolQueryError('Chưa có xác nhận của nhân viên. Hãy tóm tắt, hỏi và chờ tin nhắn tiếp theo.')
    if not cache.delete(key):
        raise ToolQueryError(f'{what.capitalize()} này đã được xử lý.')
    return draft


# ---------------------------------------------------------------- lịch làm việc

def _schedule_row(assignment):
    s = assignment.schedule
    b = s.booking
    method = earning_service._detect_payment_method(b)
    I = BookingScheduleImage.ImageType
    row = {
        'booking_code': b.booking_code, 'schedule_no': s.sequence_no,
        'service': b.service.name, 'status': s.get_status_display(), 'status_code': s.status,
        'start': _lt(s.scheduled_start), 'end': _lt(s.scheduled_end),
        'actual_start': _lt(s.actual_start), 'actual_end': _lt(s.actual_end),
        'customer_name': b.customer.get_full_name() or 'Khách hàng',
        'address': _full_address(b.address),
        'delivery_address': _full_address(b.delivery_address) if b.delivery_address else None,
        'booking_note': b.note, 'schedule_note': s.note,
        'payment': PAYMENT_LABEL[method],
    }
    if s.status == BookingSchedule.Status.PENDING:
        row['check_in_from'] = _lt(s.scheduled_start - timedelta(minutes=checkin_service.CHECKIN_EARLY_MINUTES))
        row['check_in_until'] = _lt(s.scheduled_start + timedelta(minutes=checkin_service.CHECKIN_LATE_MINUTES))
        row['check_in_note'] = 'Check-in cần GPS nên chỉ làm được trong ứng dụng.'
    elif s.status == BookingSchedule.Status.IN_PROGRESS:
        row['check_out_until'] = _lt(s.scheduled_end + timedelta(minutes=checkin_service.CHECKOUT_GRACE_MINUTES))
        row['before_images'] = s.images.filter(image_type=I.BEFORE).count()
        row['after_images'] = s.images.filter(image_type=I.AFTER).count()
    elif s.status == BookingSchedule.Status.MISSED:
        row['complaint_deadline'] = _lt(s.scheduled_end + timedelta(hours=AUTO_CANCEL_COMPLAINT_HOURS))
    return row


def get_my_schedule(worker_id, status=None, date_from='', date_to='', page=1):
    S = BookingSchedule.Status
    A = BookingAssignment
    if status == S.MISSED:
        # Buổi nhân viên đã nhận nhưng bị hệ thống tự hủy vì không check-in.
        base = A.objects.filter(worker_id=worker_id, status=A.Status.CANCELLED,
                                response_note=AUTO_CANCEL_NOTE, schedule__status=S.MISSED)
    else:
        base = A.objects.filter(worker_id=worker_id, status=A.Status.ACCEPTED)
    qs = base.select_related('schedule__booking__service', 'schedule__booking__customer',
                             'schedule__booking__address', 'schedule__booking__delivery_address')
    start, end = _parse_day(date_from, 'date_from'), _parse_day(date_to, 'date_to')
    if start or end:
        start, end = start or end, end or start
        if end < start:
            raise ToolQueryError('date_to phải từ date_from trở đi.')
        lower, upper = _range_bounds(start, end)
        qs = qs.filter(schedule__scheduled_start__gte=lower, schedule__scheduled_start__lt=upper)
    if status:
        qs = qs.filter(schedule__status=status)
    elif not (start or end):
        qs = qs.filter(schedule__status__in=[S.PENDING, S.IN_PROGRESS])
    newest_first = status in (S.COMPLETED, S.CANCELLED, S.MISSED)
    qs = qs.order_by('-schedule__scheduled_start' if newest_first else 'schedule__scheduled_start')
    count = qs.count()
    offset = (page - 1) * PAGE_SIZE
    return {'results': [_schedule_row(a) for a in qs[offset:offset + PAGE_SIZE]],
            'count': count, 'page': page, 'has_next': offset + PAGE_SIZE < count,
            'observed_at': _lt(timezone.now())}


# ---------------------------------------------------------------- lời mời

def list_pending_invitations(worker_id):
    now = timezone.now()
    invitations = (BookingAssignment.objects
                   .filter(worker_id=worker_id, status=BookingAssignment.Status.PENDING,
                           assigned_by__isnull=False, expired_at__gt=now)
                   .select_related('schedule__booking__service', 'schedule__booking__address')
                   .order_by('expired_at')[:10])
    rows = []
    for inv in invitations:
        s, b = inv.schedule, inv.schedule.booking
        method = earning_service._detect_payment_method(b)
        gross = earning_service.calculate_gross_amount(s, b)
        commission = earning_service.calculate_commission(gross)
        rows.append({
            'invitation_id': inv.id, 'booking_code': b.booking_code, 'schedule_no': s.sequence_no,
            'service': b.service.name, 'start': _lt(s.scheduled_start), 'end': _lt(s.scheduled_end),
            'area': ', '.join(p for p in (b.address.ward, b.address.city) if p),
            'respond_before': _lt(inv.expired_at), 'payment': PAYMENT_LABEL[method],
            'gross_amount': str(gross), 'commission_amount': str(commission),
            'worker_amount': str(gross - commission),
        })
    # Yêu cầu đích danh từ khách: nhận/từ chối qua prepare_preferred_response (theo booking_code).
    requests = (BookingSchedule.objects
                .filter(preferred_worker_id=worker_id, preferred_worker_expires_at__gt=now,
                        status=BookingSchedule.Status.PENDING)
                .exclude(assignments__status=BookingAssignment.Status.ACCEPTED)
                .select_related('booking__service', 'booking__address').order_by('scheduled_start')[:10])
    customer_requests = [{
        'booking_code': s.booking.booking_code, 'schedule_no': s.sequence_no,
        'service': s.booking.service.name, 'start': _lt(s.scheduled_start),
        'area': ', '.join(p for p in (s.booking.address.ward, s.booking.address.city) if p),
        'respond_before': _lt(s.preferred_worker_expires_at),
    } for s in requests]
    return {'invitations': rows, 'customer_requests': customer_requests,
            'note': 'invitations: nhận/từ chối bằng prepare_invitation_response (theo invitation_id). '
                    'customer_requests (khách chọn đích danh): nhận/từ chối bằng '
                    'prepare_preferred_response (theo booking_code).'}


def _invite_key(worker_id, thread_id):
    return f'chatbot:worker-invite-draft:{worker_id}:{_conversation_id(thread_id)}'


def prepare_invitation_response(worker_id, thread_id, invitation_id, action):
    inv = (BookingAssignment.objects
           .filter(pk=invitation_id, worker_id=worker_id, status=BookingAssignment.Status.PENDING,
                   assigned_by__isnull=False, expired_at__gt=timezone.now())
           .select_related('schedule__booking__service', 'schedule__booking__address').first())
    if not inv:
        raise ToolQueryError('Không tìm thấy lời mời còn hiệu lực (có thể đã hết hạn hoặc đã có người nhận).')
    s, b = inv.schedule, inv.schedule.booking
    if s.status != BookingSchedule.Status.PENDING:
        raise ToolQueryError('Buổi làm này không còn chờ nhận việc.')
    accept = action == 'ACCEPT'
    lines = [f'{"Nhận" if accept else "Từ chối"} lời mời buổi {s.sequence_no} của đơn {b.booking_code}',
             f'Dịch vụ: {b.service.name}',
             f'Thời gian: {_fmt(s.scheduled_start)}',
             f'Khu vực: {", ".join(p for p in (b.address.ward, b.address.city) if p)}']
    if accept:
        gross = earning_service.calculate_gross_amount(s, b)
        commission = earning_service.calculate_commission(gross)
        if earning_service._detect_payment_method(b) == 'CASH':
            wallet = Wallet.objects.filter(user_id=worker_id).first()
            balance = wallet.balance if wallet else Decimal('0')
            if balance < commission:
                raise ToolQueryError(
                    f'Ví không đủ để giữ chỗ hoa hồng {_money(commission)} (đơn tiền mặt). '
                    f'Số dư hiện tại {_money(balance)}. Đề nghị nhân viên nạp ví trong ứng dụng rồi nhận lại.')
            lines.append(f'Đơn tiền mặt: khách trả trực tiếp. Hệ thống giữ chỗ hoa hồng {_money(commission)} từ ví ngay khi nhận.')
        else:
            lines.append(f'Thu nhập dự kiến: {_money(gross - commission)} (đã trừ hoa hồng {_money(commission)}).')
    cache.set(_invite_key(worker_id, thread_id), {
        'worker_id': worker_id, 'thread_id': thread_id, 'invitation_id': inv.id, 'accept': accept,
        'booking_code': b.booking_code, 'schedule_no': s.sequence_no}, DRAFT_TTL)
    return {'done': False, 'confirmation_text': '\n'.join(lines),
            'note': 'ĐÂY CHỈ LÀ BẢN NHÁP, CHƯA PHẢN HỒI LỜI MỜI. Hỏi xác nhận rồi dừng; '
                    'chỉ gọi confirm_invitation_response ở tin nhắn sau khi nhân viên đồng ý.'}


def _ensure_chat(assignment):
    from apps.chat.service import ensure_chat_for_assignment
    try:
        ensure_chat_for_assignment(assignment)
    except Exception:
        logger.exception('ensure_chat_for_assignment failed')


def confirm_invitation_response(worker_id, thread_id):
    draft = _take_draft(_invite_key(worker_id, thread_id), worker_id, thread_id, 'yêu cầu phản hồi lời mời')
    try:
        invitation = respond_invitation(invitation_id=draft['invitation_id'],
                                        worker=_get_worker(worker_id), accept=draft['accept'])
    except serializers.ValidationError as exc:
        raise ToolQueryError(_flatten(exc.detail)) from exc
    except Http404 as exc:
        raise ToolQueryError('Không tìm thấy lời mời.') from exc
    if draft['accept']:
        _ensure_chat(invitation)
    return {'accepted': draft['accept'], 'booking_code': draft['booking_code'],
            'schedule_no': draft['schedule_no']}


# ---------------------------------------------------------------- yêu cầu đích danh từ khách

def _preferred_key(worker_id, thread_id):
    return f'chatbot:worker-preferred-draft:{worker_id}:{_conversation_id(thread_id)}'


def _my_preferred_schedules(worker_id, booking_reference):
    now = timezone.now()
    return list(BookingSchedule.objects.filter(
        booking__booking_code__iexact=booking_reference.strip(), preferred_worker_id=worker_id,
        preferred_worker_expires_at__gt=now, status=BookingSchedule.Status.PENDING,
    ).select_related('booking__service', 'booking__address').order_by('scheduled_start'))


def prepare_preferred_response(worker_id, thread_id, booking_reference, action):
    schedules = _my_preferred_schedules(worker_id, booking_reference)
    if not schedules:
        raise ToolQueryError('Không có yêu cầu đích danh còn hiệu lực cho đơn này (có thể đã hết hạn).')
    b = schedules[0].booking
    accept = action == 'ACCEPT'
    lines = [f'{"Nhận" if accept else "Từ chối"} yêu cầu đích danh của khách cho đơn {b.booking_code}',
             f'Dịch vụ: {b.service.name}',
             f'Khu vực: {", ".join(p for p in (b.address.ward, b.address.city) if p)}',
             f'Số buổi: {len(schedules)}'] + [
             f'Buổi {s.sequence_no}: {_fmt(s.scheduled_start)}' for s in schedules[:6]]
    if len(schedules) > 6:
        lines.append(f'... và {len(schedules) - 6} buổi nữa')
    if accept and earning_service._detect_payment_method(b) == 'CASH':
        total = sum((earning_service.calculate_commission(earning_service.calculate_gross_amount(s, b))
                     for s in schedules), Decimal('0'))
        wallet = Wallet.objects.filter(user_id=worker_id).first()
        balance = wallet.balance if wallet else Decimal('0')
        if balance < total:
            raise ToolQueryError(f'Ví không đủ để giữ chỗ hoa hồng {_money(total)} (đơn tiền mặt). '
                                 f'Số dư hiện tại {_money(balance)}. Đề nghị nạp ví trong ứng dụng rồi nhận lại.')
        lines.append(f'Đơn tiền mặt: hệ thống giữ chỗ hoa hồng {_money(total)} từ ví ngay khi nhận.')
    cache.set(_preferred_key(worker_id, thread_id), {
        'worker_id': worker_id, 'thread_id': thread_id, 'booking_id': b.id, 'booking_code': b.booking_code,
        'schedule_ids': [s.id for s in schedules], 'accept': accept}, DRAFT_TTL)
    return {'done': False, 'confirmation_text': '\n'.join(lines),
            'note': 'ĐÂY CHỈ LÀ BẢN NHÁP, CHƯA PHẢN HỒI. Hỏi xác nhận rồi dừng; '
                    'chỉ gọi confirm_preferred_response ở tin nhắn sau khi nhân viên đồng ý.'}


def confirm_preferred_response(worker_id, thread_id):
    draft = _take_draft(_preferred_key(worker_id, thread_id), worker_id, thread_id, 'yêu cầu đích danh')
    worker = _get_worker(worker_id)
    try:
        with distributed_lock(f"booking:{draft['booking_id']}"):
            if draft['accept']:
                res = claim_booking_package(booking_id=draft['booking_id'], worker=worker,
                                            schedule_ids=draft['schedule_ids'])
            else:
                decline_preferred_request(booking_id=draft['booking_id'], worker=worker)
                res = None
    except serializers.ValidationError as exc:
        raise ToolQueryError(_flatten(exc.detail)) from exc
    except Http404 as exc:
        raise ToolQueryError('Không tìm thấy đơn.') from exc
    if not res:
        return {'accepted': False, 'booking_code': draft['booking_code']}
    for assignment in res['claimed']:
        _ensure_chat(assignment)
    return {'accepted': True, 'booking_code': draft['booking_code'],
            'claimed_count': len(res['claimed']), 'skipped_count': len(res['skipped']),
            'skipped_reasons': [str(s['reason']) for s in res['skipped']][:3]}


# ---------------------------------------------------------------- thu nhập

def _period_bounds(period):
    if period == 'today':
        start = end = timezone.localdate()
    else:
        start, end = _resolve_period(period)
    return start, end, *_range_bounds(start, end)


def get_my_earnings(worker_id, period='week'):
    start, end, lower, upper = _period_bounds(period)
    mine = WorkerEarning.objects.filter(worker_id=worker_id, voided_at__isnull=True)
    E = WorkerEarning.PaymentMethod
    agg = mine.filter(completed_at__gte=lower, completed_at__lt=upper).aggregate(
        jobs=Count('id'), gross=_sum('gross_amount'), income=_sum('worker_amount'),
        commission=_sum('commission_amount'),
        online_income=_sum('worker_amount', filter=Q(payment_method=E.ONLINE)),
        cash_commission=_sum('commission_amount', filter=Q(payment_method=E.CASH)))
    owed = mine.filter(payment_method=E.CASH, settled_at__isnull=True).aggregate(t=_sum('commission_amount'))['t']
    pending = mine.filter(payment_method=E.ONLINE, wallet_credited_at__isnull=True).aggregate(t=_sum('worker_amount'))['t']
    return {'period': period, 'start': start.isoformat(), 'end': end.isoformat(),
            'completed_jobs': agg['jobs'], 'gross_amount': str(agg['gross']),
            'income_after_commission': str(agg['income']), 'commission_total': str(agg['commission']),
            'online_income': str(agg['online_income']), 'cash_commission': str(agg['cash_commission']),
            'commission_owed_all_time': str(owed), 'pending_release_all_time': str(pending),
            'note': f'pending_release: thu nhập đơn online chưa vào ví, được cộng sau '
                    f'{earning_service.EARNING_HOLD_HOURS} giờ nếu không có khiếu nại đang mở. '
                    'commission_owed: hoa hồng đơn tiền mặt chưa được trừ.',
            'observed_at': _lt(timezone.now())}


def get_earning_history(worker_id, period='week', page=1):
    _, _, lower, upper = _period_bounds(period)
    qs = (WorkerEarning.objects
          .filter(worker_id=worker_id, voided_at__isnull=True, completed_at__gte=lower, completed_at__lt=upper)
          .select_related('booking__service').order_by('-completed_at'))
    count = qs.count()
    offset = (page - 1) * PAGE_SIZE
    return {'results': [{
        'booking_code': e.booking.booking_code, 'service': e.booking.service.name,
        'completed_at': _lt(e.completed_at), 'payment': PAYMENT_LABEL[e.payment_method],
        'gross_amount': str(e.gross_amount), 'commission_amount': str(e.commission_amount),
        'worker_amount': str(e.worker_amount),
        'wallet_status': ('Đã vào ví' if e.wallet_credited_at else
                          ('Đã trừ hoa hồng' if e.settled_at else 'Chưa vào ví / chưa tất toán')),
    } for e in qs[offset:offset + PAGE_SIZE]], 'count': count, 'page': page,
        'has_next': offset + PAGE_SIZE < count}


# ---------------------------------------------------------------- ví và rút tiền

def get_worker_wallet(worker_id, page=1):
    data = get_wallet(worker_id, page)  # dùng chung, lọc theo user_id
    balance = Decimal(data['balance'])
    escrow = wallet_service.MIN_WORKER_ESCROW_BALANCE
    data['min_escrow_balance'] = str(escrow)
    data['withdrawable_now'] = str(max(balance - escrow, Decimal('0')))
    data['withdraw_limits'] = {
        'min_per_request': str(getattr(settings, 'WALLET_WITHDRAW_MIN', 50000)),
        'max_per_request': str(getattr(settings, 'WALLET_WITHDRAW_MAX', 2000000)),
        'max_per_day': str(getattr(settings, 'WALLET_WITHDRAW_DAILY_MAX', 5000000))}
    if page == 1:
        data['recent_withdrawals'] = [{
            'amount': str(w.amount), 'status': w.get_status_display(),
            'bank': f'{w.bank_name} {w.account_number_masked}'.strip(),
            'created_at': _lt(w.created_at),
            'failure_reason': w.failure_reason or None,
        } for w in WithdrawRequest.objects.filter(user_id=worker_id).order_by('-created_at')[:5]]
    data['note'] = ('Nhân viên phải giữ lại tối thiểu mức ký quỹ trong ví. Việc rút tiền thực hiện trong ứng dụng, '
                    'trợ lý chỉ xem. Tài khoản ngân hàng mới thêm có thể phải chờ một thời gian mới rút được.')
    return data


# ---------------------------------------------------------------- đánh giá, hồ sơ, khiếu nại

def get_my_reviews(worker_id):
    worker = _get_worker(worker_id)
    recent = (Review.objects.filter(assignment__worker_id=worker_id, is_visible=True)
              .select_related('assignment__schedule__booking__service').order_by('-created_at')[:5])
    return {**worker_review_summary(worker), 'recent': [{
        'rating': r.rating, 'comment': r.comment,
        'booking_code': r.assignment.schedule.booking.booking_code,
        'service': r.assignment.schedule.booking.service.name, 'created_at': _lt(r.created_at),
    } for r in recent]}


def get_my_profile(worker_id):
    profile = (WorkerProfile.objects.select_related('user', 'registered_service')
               .filter(user_id=worker_id).first())
    if not profile:
        raise ToolQueryError('Chưa có hồ sơ nhân viên.')
    areas = [f'{wa.area.name}, {wa.area.city}' for wa in
             profile.user.working_areas.filter(area__is_active=True).select_related('area')[:30]]
    return {'full_name': profile.user.get_full_name() or profile.user.username,
            'status': profile.get_status_display(), 'status_code': profile.status,
            'rejection_reason': profile.rejection_reason if profile.status == 'REJECTED' else None,
            'rejected_fields': profile.rejected_fields if profile.status == 'REJECTED' else None,
            'registered_service': profile.registered_service.name if profile.registered_service else None,
            'working_areas': areas, 'average_rating': str(profile.average_rating),
            'total_completed_jobs': profile.total_completed_jobs,
            'completeness': get_worker_profile_completeness(profile)}


def list_my_complaints(worker_id, page=1):
    qs = (Complaint.objects.filter(reporter_id=worker_id)
          .select_related('booking', 'issue_type').order_by('-created_at'))
    count = qs.count()
    offset = (page - 1) * PAGE_SIZE
    return {'results': [{
        'complaint_id': c.id, 'booking_code': c.booking.booking_code, 'issue': c.issue_type.name,'code': c.code,
        'status': c.get_status_display(), 'created_at': _lt(c.created_at),
        'resolution_note': c.resolution_note if c.status in ('RESOLVED', 'REJECTED') else None,
        'worker_delta': str(c.worker_delta) if c.status == 'RESOLVED' else None,
    } for c in qs[offset:offset + PAGE_SIZE]], 'count': count, 'page': page,
        'has_next': offset + PAGE_SIZE < count,
        'note': 'Chỉ gồm khiếu nại do chính nhân viên gửi.'}


# ---------------------------------------------------------------- check-out
# Check-in KHÔNG làm qua trợ lý: cần GPS của điện thoại (bán kính 300 m quanh địa chỉ khách).

def _checkout_key(worker_id, thread_id):
    return f'chatbot:worker-checkout-draft:{worker_id}:{_conversation_id(thread_id)}'


def _find_in_progress(worker_id, booking_reference, schedule_no):
    qs = (BookingSchedule.objects
          .filter(booking__booking_code__iexact=booking_reference.strip(),
                  status=BookingSchedule.Status.IN_PROGRESS,
                  assignments__worker_id=worker_id,
                  assignments__status=BookingAssignment.Status.ACCEPTED)
          .select_related('booking__service').distinct().order_by('sequence_no'))
    if schedule_no is not None:
        qs = qs.filter(sequence_no=schedule_no)
    found = list(qs)
    if not found:
        raise ToolQueryError('Không có buổi nào đang thực hiện (đã check-in, chưa check-out) '
                             'của nhân viên ứng với đơn này.')
    if len(found) > 1:
        raise ToolQueryError('Đơn có nhiều buổi đang thực hiện. Hỏi nhân viên check-out buổi số mấy.')
    return found[0]


def prepare_check_out(worker_id, thread_id, booking_reference, schedule_no, completion_note):
    s = _find_in_progress(worker_id, booking_reference, schedule_no)
    b = s.booking
    now = timezone.now()
    deadline = s.scheduled_end + timedelta(minutes=checkin_service.CHECKOUT_GRACE_MINUTES)
    if now > deadline:
        raise ToolQueryError(f'Đã quá hạn tự check-out ({_fmt(deadline)}). Hệ thống sẽ tự hoàn thành buổi này.')
    I = BookingScheduleImage.ImageType
    before = s.images.filter(image_type=I.BEFORE).count()
    after = s.images.filter(image_type=I.AFTER).count()
    if not before or not after:
        missing = ' và '.join(n for n, c in (('ảnh trước khi làm', before), ('ảnh sau khi làm', after)) if not c)
        raise ToolQueryError(f'Chưa thể check-out: còn thiếu {missing}. Nhân viên cần tải ảnh lên trong ứng dụng '
                             '(trợ lý không nhận ảnh) rồi quay lại.')
    note = (completion_note or '').strip()
    lines = [f'Check-out buổi {s.sequence_no} của đơn {b.booking_code}',
             f'Dịch vụ: {b.service.name}',
             f'Giờ dự kiến kết thúc: {_fmt(s.scheduled_end)}',
             f'Ảnh đã tải: {before} ảnh trước, {after} ảnh sau']
    if note:
        lines.append(f'Ghi chú hoàn thành: {note}')
    cache.set(_checkout_key(worker_id, thread_id), {
        'worker_id': worker_id, 'thread_id': thread_id, 'schedule_id': s.id,
        'booking_code': b.booking_code, 'schedule_no': s.sequence_no, 'note': note}, DRAFT_TTL)
    return {'done': False, 'confirmation_text': '\n'.join(lines),
            'note': 'ĐÂY CHỈ LÀ BẢN NHÁP, CHƯA CHECK-OUT. Hỏi xác nhận rồi dừng; '
                    'chỉ gọi confirm_check_out ở tin nhắn sau khi nhân viên đồng ý.'}


def confirm_check_out(worker_id, thread_id):
    draft = _take_draft(_checkout_key(worker_id, thread_id), worker_id, thread_id, 'yêu cầu check-out')
    try:
        schedule = checkin_service.check_out(schedule_id=draft['schedule_id'], worker=_get_worker(worker_id),
                                             completion_note=draft['note'] or None)
    except serializers.ValidationError as exc:
        raise ToolQueryError(_flatten(exc.detail)) from exc
    except Http404 as exc:
        raise ToolQueryError('Không tìm thấy buổi làm việc.') from exc
    return {'checked_out': True, 'booking_code': draft['booking_code'],
            'schedule_no': draft['schedule_no'], 'actual_end': _lt(schedule.actual_end),
            'note': 'Thu nhập buổi này đã được ghi nhận; hỏi thu nhập thì tra get_my_earnings.'}


# ---------------------------------------------------------------- tạo khiếu nại

def list_complaint_issue_types():
    rows = ComplaintIssueType.objects.filter(is_active=True, applies_to__in=['WORKER', 'ANY'])
    return {'results': [{'id': t.id, 'code': t.code, 'name': t.name, 'description': t.description,
                         'stage': t.get_stage_display()} for t in rows],
            'note': 'Mỗi loại chỉ hợp lệ ở đúng giai đoạn của đơn (trước/đang/sau khi làm). '
                    'Nếu prepare_complaint báo không phù hợp trạng thái thì chọn loại khác cùng nhân viên.'}


def _complaint_key(worker_id, thread_id):
    return f'chatbot:worker-complaint-draft:{worker_id}:{_conversation_id(thread_id)}'


def _complaint_serializer(worker, booking_id, schedule_id, issue_type_id, content):
    # Dùng đúng serializer của API; nó chỉ cần request.user.
    return ComplaintCreateSerializer(
        data={'booking': booking_id, 'schedule': schedule_id, 'issue_type': issue_type_id, 'content': content},
        context={'request': SimpleNamespace(user=worker)})


def prepare_complaint(worker_id, thread_id, booking_reference, schedule_no, issue_type_id, content):
    worker = _get_worker(worker_id)
    A = BookingAssignment
    mine_q = Q(status=A.Status.ACCEPTED) | Q(status=A.Status.CANCELLED, response_note=AUTO_CANCEL_NOTE)
    booking = Booking.objects.filter(booking_code__iexact=booking_reference.strip(),
                                     schedules__assignments__worker_id=worker_id).distinct().first()
    if not booking:
        raise ToolQueryError('Không tìm thấy đơn mà nhân viên đã nhận. Hỏi lại mã đơn.')
    mine = list(BookingSchedule.objects.filter(
        booking=booking, assignments__in=A.objects.filter(worker_id=worker_id).filter(mine_q)
    ).distinct().order_by('sequence_no'))
    if not mine:
        raise ToolQueryError('Nhân viên không có buổi nào thuộc đơn này.')
    if schedule_no is None and len(mine) > 1:
        raise ToolQueryError('Đơn có nhiều buổi của nhân viên. Hỏi nhân viên khiếu nại buổi số mấy.')
    schedule = mine[0] if schedule_no is None else next((s for s in mine if s.sequence_no == schedule_no), None)
    if not schedule:
        raise ToolQueryError('Nhân viên không có buổi này trong đơn.')
    try:
        with transaction.atomic():   # chỉ validate, không ghi gì
            ser = _complaint_serializer(worker, booking.id, schedule.id, issue_type_id, content.strip())
            ser.is_valid(raise_exception=True)
    except serializers.ValidationError as exc:
        raise ToolQueryError(_flatten(exc.detail)) from exc
    issue = ComplaintIssueType.objects.get(pk=issue_type_id)
    cache.set(_complaint_key(worker_id, thread_id), {
        'worker_id': worker_id, 'thread_id': thread_id, 'booking_id': booking.id,
        'schedule_id': schedule.id, 'issue_type_id': issue.id, 'content': content.strip()}, DRAFT_TTL)
    return {'done': False, 'confirmation_text': '\n'.join([
        f'Báo sự cố đơn {booking.booking_code}, buổi {schedule.sequence_no} ({_fmt(schedule.scheduled_start)})',
        f'Loại sự cố: {issue.name}', f'Nội dung: {content.strip()}']),
        'note': 'ĐÂY CHỈ LÀ BẢN NHÁP, CHƯA GỬI. Hỏi "Bạn xác nhận gửi báo cáo không?" rồi DỪNG.'}


def confirm_complaint(worker_id, thread_id):
    draft = _take_draft(_complaint_key(worker_id, thread_id), worker_id, thread_id, 'báo cáo sự cố')
    worker = _get_worker(worker_id)
    try:
        with transaction.atomic():
            ser = _complaint_serializer(worker, draft['booking_id'], draft['schedule_id'],
                                        draft['issue_type_id'], draft['content'])
            ser.is_valid(raise_exception=True)
            complaint = ser.save()
    except serializers.ValidationError as exc:
        raise ToolQueryError(_flatten(exc.detail)) from exc
    try:
        push_complaint_changed(complaint.id)
    except Exception:
        logger.exception('push_complaint_changed failed')
    return {'complaint_id': complaint.id, 'status': complaint.get_status_display(),'code': complaint.code,
            'booking_code': complaint.booking.booking_code}