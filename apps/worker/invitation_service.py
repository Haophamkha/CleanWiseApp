"""Admin invitations use pending assignments; accepting uses normal claim checks."""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.bookings.activity_service import record_booking_activity
from apps.bookings.models import BookingActivity, BookingSchedule
from apps.notifications.models import Notification
from apps.notifications.push import send_push_to_user
from apps.notifications.realtime import push_unread_count
from .models import BookingAssignment


def _notify(*, user, title, message, type, related_booking, related_schedule, extra_data=None):
    notification = Notification.objects.create(
        user=user, title=title, message=message, type=type,
        related_booking=related_booking, related_schedule=related_schedule,
        navigation_source=(extra_data or {}).get('source', 'mine'),
    )
    def deliver():
        push_unread_count(user.id)
        send_push_to_user(user, title, message, {
            'notification_id': notification.id, 'type': type,
            'booking_id': related_booking.id, 'schedule_id': related_schedule.id, 'view': 'session',
            **(extra_data or {}),
        })
    transaction.on_commit(deliver, robust=True)
    return notification


def active_invitation(schedule, worker=None):
    query = schedule.assignments.filter(status='PENDING', expired_at__gt=timezone.now())
    if worker is not None:
        query = query.filter(worker=worker)
    return query.first()


def finish_invitation(invitation, status, actor=None, reason=''):
    invitation.status = status
    invitation.response_note = reason
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=['status', 'response_note', 'responded_at', 'updated_at'])
    labels = {'ACCEPTED': 'đã nhận việc', 'REJECTED': 'đã từ chối',
              'EXPIRED': 'đã hết hạn', 'CANCELLED': 'đã bị thu hồi'}
    schedule = invitation.schedule
    label = 'đã đóng vì buổi làm đã có người nhận' if status == 'CANCELLED' and reason == 'Tự động hủy do đã có nhân viên khác nhận việc.' else labels[status]
    who = invitation.worker.get_full_name() or invitation.worker.username
    message = f'Lời mời buổi {schedule.sequence_no} đơn {schedule.booking.booking_code} {label}.'
    if status in ('REJECTED', 'CANCELLED') and reason:
        message += f' Lý do: {reason}'
    titles = {
        'ACCEPTED': f'{who} đã nhận lời mời',
        'REJECTED': f'{who} đã từ chối lời mời',
        'EXPIRED': 'Lời mời đã hết hạn',
        'CANCELLED': 'Lời mời đã bị thu hồi',
    }
    record_booking_activity(
        booking=schedule.booking, schedule=schedule, actor=actor,
        event_type=BookingActivity.EventType.BOOKING_UPDATED,
        message=message,
        new_data={'invitation_id': invitation.id, 'worker_id': invitation.worker_id, 'status': status},
    )
    recipient = invitation.worker if status == 'CANCELLED' else invitation.assigned_by
    if status == 'EXPIRED':
        _notify(user=invitation.worker, title='Lời mời đã hết hạn', message=message,
                type=Notification.Type.ASSIGNMENT, related_booking=schedule.booking,
                related_schedule=schedule, extra_data={'source': 'available', 'view': 'session'})
    if recipient:
        _notify(
            user=recipient, title=titles[status], message=message,
            type=Notification.Type.ASSIGNMENT, related_booking=schedule.booking,
            related_schedule=schedule, extra_data={'source': 'available'},
        )
    return invitation


@transaction.atomic
def send_invitations(*, schedule_id, worker_ids, admin_user, response_minutes=15):
    from .assignment_service import (
        User, _lock_booking_of_schedule, validate_worker_for_schedule,
        list_available_workers_for_schedule,
    )
    _lock_booking_of_schedule(schedule_id)
    schedule = get_object_or_404(BookingSchedule.objects.select_for_update().select_related('booking'), pk=schedule_id)
    if not worker_ids or len(worker_ids) > 100 or len(set(worker_ids)) != len(worker_ids):
        raise ValidationError({'worker_ids': 'Chọn từ 1 đến 100 nhân viên, không trùng lặp.'})
    workers = list(User.objects.filter(pk__in=worker_ids, role='WORKER', is_active=True).order_by('id'))
    if len(workers) != len(worker_ids):
        raise ValidationError({'worker_ids': 'Có nhân viên không tồn tại hoặc không còn hoạt động.'})
    for worker in workers:
        validate_worker_for_schedule(schedule=schedule, worker=worker)
    if schedule.assignments.filter(status='ACCEPTED').exists():
        raise ValidationError({'schedule': 'Buổi đã có nhân viên. Hãy bỏ phân công trước khi mời người khác.'})
    now = timezone.now()
    for old in schedule.assignments.filter(status='PENDING'):
        if old.expired_at and old.expired_at > now:
            raise ValidationError({'schedule': 'Buổi đang có lời mời chờ phản hồi. Hãy thu hồi trước.'})
        finish_invitation(old, 'EXPIRED')
    if schedule.preferred_worker_id and schedule.preferred_worker_expires_at and schedule.preferred_worker_expires_at > now:
        raise ValidationError({'schedule': 'Khách hàng đang có lời mời ưu tiên. Hãy thu hồi trước.'})
    if response_minutes not in (15, 30, 60):
        raise ValidationError({'response_minutes': 'Chọn thời hạn 15, 30 hoặc 60 phút.'})
    expires = min(now + timedelta(minutes=response_minutes), schedule.scheduled_start - timedelta(minutes=30))
    if expires <= now:
        raise ValidationError({'schedule': 'Cần gửi lời mời trước giờ làm ít nhất 30 phút.'})
    eligible_ids = {w.id for w in list_available_workers_for_schedule(schedule_id=schedule.id)}
    if any(worker.id not in eligible_ids for worker in workers):
        raise ValidationError({'worker_ids': 'Có nhân viên không còn đủ điều kiện. Hãy tải lại danh sách.'})
    invitations = []
    for worker in workers:
        invitation = BookingAssignment.objects.create(
            schedule=schedule, worker=worker, assigned_by=admin_user,
            status='PENDING', expired_at=expires,
        )
        record_booking_activity(
            booking=schedule.booking, schedule=schedule, actor=admin_user,
            event_type=BookingActivity.EventType.BOOKING_UPDATED,
            message=f'Admin mời {worker.get_full_name() or worker.username} nhận buổi {schedule.sequence_no}.',
            new_data={'invitation_id': invitation.id, 'worker_id': worker.id, 'expires_at': expires.isoformat()},
        )
        _notify(
            user=worker, title='Lời mời nhận việc từ admin',
            message=f'Bạn được mời nhận buổi {schedule.sequence_no}, đơn {schedule.booking.booking_code}. Vui lòng phản hồi trước {timezone.localtime(expires):%H:%M %d/%m}.',
            type=Notification.Type.ASSIGNMENT, related_booking=schedule.booking,
            related_schedule=schedule, extra_data={'source': 'available', 'view': 'session', 'invitation_id': invitation.id},
        )
        invitations.append(invitation)

    return invitations


def send_invitation(*, schedule_id, worker_id, admin_user, response_minutes=15):
    return send_invitations(schedule_id=schedule_id, worker_ids=[worker_id],
                            admin_user=admin_user, response_minutes=response_minutes)[0]

@transaction.atomic
def withdraw_invitation(*, schedule_id, actor, reason):
    from .assignment_service import _lock_booking_of_schedule
    _lock_booking_of_schedule(schedule_id)
    schedule = get_object_or_404(BookingSchedule.objects.select_for_update(), pk=schedule_id)
    invitations = list(schedule.assignments.filter(status='PENDING', expired_at__gt=timezone.now()))
    if invitations:
        for invitation in invitations:
            finish_invitation(invitation, 'CANCELLED', actor, reason)
        return invitations[0]
    if schedule.preferred_worker_id and schedule.preferred_worker_expires_at and schedule.preferred_worker_expires_at > timezone.now():
        now = timezone.now()
        schedule.preferred_worker_expires_at = now
        schedule.preferred_expiry_notified_at = now
        schedule.save(update_fields=[
            'preferred_worker_expires_at', 'preferred_expiry_notified_at', 'updated_at',
        ])
        record_booking_activity(
            booking=schedule.booking, schedule=schedule, actor=actor,
            event_type=BookingActivity.EventType.BOOKING_UPDATED,
            message=f'Admin thu hồi lời mời ưu tiên của khách cho buổi {schedule.sequence_no}.',
            new_data={'reason': reason},
        )
        from apps.notifications.services import notify_customer_preferred_withdrawn
        _notify(
            user=schedule.preferred_worker, title='Lời chỉ định đã bị thu hồi',
            message=f'Lời chỉ định của khách cho buổi {schedule.sequence_no} đơn '
                    f'{schedule.booking.booking_code} đã bị CleanWise thu hồi. Lý do: {reason}',
            type=Notification.Type.ASSIGNMENT, related_booking=schedule.booking,
            related_schedule=schedule, extra_data={'source': 'available'},
        )
        notify_customer_preferred_withdrawn(
            schedule.booking, schedule.preferred_worker, schedule,
        )
        return None
    raise ValidationError({'schedule': 'Không có lời mời đang hiệu lực.'})


@transaction.atomic
def respond_invitation(*, invitation_id, worker, accept):
    from .assignment_service import User, _lock_booking_of_schedule, claim_schedule
    User.objects.select_for_update().get(pk=worker.pk)
    ref = get_object_or_404(BookingAssignment, pk=invitation_id, worker=worker, assigned_by__isnull=False)
    _lock_booking_of_schedule(ref.schedule_id)
    invitation = BookingAssignment.objects.select_for_update().get(pk=ref.pk)
    if accept and invitation.status == 'ACCEPTED':
        return invitation
    if not accept and invitation.status == 'REJECTED':
        return invitation
    if invitation.status != 'PENDING' or not invitation.expired_at or invitation.expired_at <= timezone.now():
        raise ValidationError({'invitation': 'Lời mời đã hết hạn hoặc không còn hiệu lực.'})
    if accept:
        return claim_schedule(schedule_id=invitation.schedule_id, worker=worker, invitation_id=invitation.id)
    return finish_invitation(invitation, 'REJECTED', worker)


def expire_invitations():
    from .assignment_service import _lock_booking_of_schedule, OPEN_BOOKING_STATUSES
    invalid = Q(expired_at__lte=timezone.now()) | ~Q(schedule__status='PENDING') | ~Q(schedule__booking__status__in=OPEN_BOOKING_STATUSES)
    ids = list(BookingAssignment.objects.filter(status='PENDING', assigned_by__isnull=False).filter(invalid).values_list('id', 'schedule_id'))
    for invitation_id, schedule_id in ids:
        with transaction.atomic():
            _lock_booking_of_schedule(schedule_id)
            invitation = BookingAssignment.objects.select_for_update().get(pk=invitation_id)
            if invitation.status == 'PENDING':
                finish_invitation(invitation, 'EXPIRED')


def close_other_invitations(schedule, assignment, actor):
    for invitation in schedule.assignments.filter(status='PENDING').exclude(pk=assignment.pk):
        finish_invitation(invitation, 'CANCELLED', actor, 'Tự động hủy do đã có nhân viên khác nhận việc.')
