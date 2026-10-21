from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers

from apps.addresses.models import CustomerAddress
from apps.notifications.services import (
    create_notification_with_push,
    fmt_time,
    notify_customer_schedule_completed,
    notify_customer_worker_removed,
    notify_worker_booking_cancelled,
    notify_worker_removed_from_schedule,
)
from apps.notifications.models import Notification
from apps.payments.models import Payment
from apps.vouchers.voucher_service import release_user_voucher
from apps.wallets import earning_service, wallet_service
from apps.worker.models import BookingAssignment
from .booking_service import cancel_booking_core

from .activity_service import record_booking_activity
from .models import Booking, BookingActivity, BookingSchedule


@transaction.atomic
def update_booking_by_admin(*, booking_id, actor, validated_data):
    booking = get_object_or_404(
        Booking.objects.select_for_update().select_related('service'),
        pk=booking_id,
    )
    if booking.status in (Booking.Status.CANCELLED, Booking.Status.COMPLETED, Booking.Status.FAILED):
        raise serializers.ValidationError({'booking': 'Không thể sửa đơn đã kết thúc.'})

    old_data = {}
    new_data = {}
    update_fields = []

    if 'note' in validated_data:
        old_data['note'] = booking.note
        booking.note = validated_data['note'] or None
        new_data['note'] = booking.note
        update_fields.append('note')

    if 'address_id' in validated_data:
        address = get_object_or_404(
            CustomerAddress,
            pk=validated_data['address_id'],
            customer=booking.customer,
            is_active=True,
        )
        from apps.worker.assignment_service import _covers, _worker_area_index

        assigned = BookingAssignment.objects.filter(
            schedule__booking=booking,
            status=BookingAssignment.Status.ACCEPTED,
            schedule__status__in=(BookingSchedule.Status.PENDING, BookingSchedule.Status.IN_PROGRESS),
        ).select_related('worker')
        uncovered = sorted({
            a.worker.get_full_name() or a.worker.username
            for a in assigned
            if not _covers(_worker_area_index(a.worker), address)
        })
        if uncovered:
            raise serializers.ValidationError({
                'address_id': 'Nhân viên không hoạt động ở khu vực mới: '
                              f'{", ".join(uncovered)}. Hãy bỏ phân công trước khi đổi địa chỉ.',
            })
        old_data['address_id'] = booking.address_id
        booking.address = address
        new_data['address_id'] = address.id
        update_fields.append('address')

    if 'delivery_address_id' in validated_data:
        address_count = (booking.service.form_schema or {}).get('address_count', 1)
        delivery_id = validated_data['delivery_address_id']
        if address_count >= 2 and not delivery_id:
            raise serializers.ValidationError(
                {'delivery_address_id': 'Dịch vụ này cần địa chỉ chuyển đến.'}
            )
        if address_count < 2 and delivery_id:
            raise serializers.ValidationError(
                {'delivery_address_id': 'Dịch vụ này không sử dụng địa chỉ chuyển đến.'}
            )
        delivery = None
        if delivery_id:
            delivery = get_object_or_404(
                CustomerAddress,
                pk=delivery_id,
                customer=booking.customer,
                is_active=True,
            )
        old_data['delivery_address_id'] = booking.delivery_address_id
        booking.delivery_address = delivery
        new_data['delivery_address_id'] = delivery.id if delivery else None
        update_fields.append('delivery_address')

    booking.save(update_fields=[*update_fields, 'updated_at'])
    record_booking_activity(
        booking=booking,
        actor=actor,
        event_type=BookingActivity.EventType.BOOKING_UPDATED,
        message=f'Admin cập nhật đơn {booking.booking_code}.',
        old_data=old_data,
        new_data=new_data,
    )
    return booking


@transaction.atomic
def update_schedule_by_admin(*, schedule_id, actor, validated_data):
    schedule = get_object_or_404(
        BookingSchedule.objects.select_for_update(of=('self',)).select_related('booking'),
        pk=schedule_id,
    )
    if schedule.status != BookingSchedule.Status.PENDING:
        raise serializers.ValidationError({'schedule': 'Chỉ được sửa buổi chưa bắt đầu.'})

    start = validated_data.get('scheduled_start', schedule.scheduled_start)
    end = validated_data.get('scheduled_end', schedule.scheduled_end)
    changing_time = start != schedule.scheduled_start or end != schedule.scheduled_end
    if start >= end:
        raise serializers.ValidationError({'scheduled_end': 'Giờ kết thúc phải sau giờ bắt đầu.'})
    if changing_time and start <= timezone.now():
        raise serializers.ValidationError({'scheduled_start': 'Thời gian mới phải ở tương lai.'})

    current_assignment = schedule.assignments.filter(
        status=BookingAssignment.Status.ACCEPTED,
    ).select_related('worker').first()
    if changing_time and current_assignment:
        conflict = BookingSchedule.objects.filter(
            assignments__worker=current_assignment.worker,
            assignments__status=BookingAssignment.Status.ACCEPTED,
            status__in=(BookingSchedule.Status.PENDING, BookingSchedule.Status.IN_PROGRESS),
            scheduled_start__lt=end,
            scheduled_end__gt=start,
        ).exclude(pk=schedule.pk).exists()
        if conflict:
            raise serializers.ValidationError(
                {'scheduled_start': 'Lịch mới bị trùng với công việc khác của nhân viên.'}
            )

    old_data = {
        'scheduled_start': schedule.scheduled_start.isoformat(),
        'scheduled_end': schedule.scheduled_end.isoformat(),
        'note': schedule.note,
    }
    schedule.scheduled_start = start
    schedule.scheduled_end = end
    if 'note' in validated_data:
        schedule.note = validated_data['note'] or None
    schedule.reminder_sent_at = None if changing_time else schedule.reminder_sent_at
    schedule.save(update_fields=[
        'scheduled_start', 'scheduled_end', 'note', 'reminder_sent_at', 'updated_at',
    ])

    new_data = {
        'scheduled_start': schedule.scheduled_start.isoformat(),
        'scheduled_end': schedule.scheduled_end.isoformat(),
        'note': schedule.note,
        'reason': validated_data.get('reason'),
    }
    record_booking_activity(
        booking=schedule.booking,
        schedule=schedule,
        actor=actor,
        event_type=(
            BookingActivity.EventType.SCHEDULE_RESCHEDULED
            if changing_time else BookingActivity.EventType.BOOKING_UPDATED
        ),
        message=(
            f'Admin đổi lịch buổi {schedule.sequence_no}.'
            if changing_time else f'Admin cập nhật buổi {schedule.sequence_no}.'
        ),
        old_data=old_data,
        new_data=new_data,
    )

    if changing_time:
        from apps.worker.invitation_service import finish_invitation
        for invitation in schedule.assignments.filter(status='PENDING', assigned_by__isnull=False):
            finish_invitation(invitation, 'CANCELLED', actor, 'Buổi làm đã đổi lịch. Admin cần gửi lời mời mới.')
        if schedule.preferred_worker_id:
            now = timezone.now()
            schedule.preferred_worker_expires_at = now
            schedule.preferred_expiry_notified_at = now
            schedule.save(update_fields=[
                'preferred_worker_expires_at', 'preferred_expiry_notified_at', 'updated_at',
            ])
        why = validated_data.get('reason')
        message = (
            f'Buổi {schedule.sequence_no} của đơn {schedule.booking.booking_code} đổi sang '
            f'{fmt_time(schedule.scheduled_start)}.' + (f' Lý do: {why}.' if why else '')
        )
        create_notification_with_push(
            user=schedule.booking.customer, title='Lịch dịch vụ đã thay đổi',
            message=message, type=Notification.Type.BOOKING,
            related_booking=schedule.booking, related_schedule=schedule,
        )
        if current_assignment:
            create_notification_with_push(
                user=current_assignment.worker, title='Lịch làm việc đã thay đổi',
                message=message, type=Notification.Type.ASSIGNMENT,
                related_booking=schedule.booking, related_schedule=schedule,
            )
    return schedule


@transaction.atomic
def unassign_worker_by_admin(*, schedule_id, actor, reason):
    schedule = get_object_or_404(
        BookingSchedule.objects.select_for_update(of=('self',)).select_related('booking'),
        pk=schedule_id,
    )
    if schedule.status != BookingSchedule.Status.PENDING:
        raise serializers.ValidationError({'schedule': 'Chỉ được bỏ phân công buổi chưa bắt đầu.'})
    assignment = schedule.assignments.select_for_update(of=('self',)).filter(
        status=BookingAssignment.Status.ACCEPTED,
    ).select_related('worker').first()
    if not assignment:
        raise serializers.ValidationError({'assignment': 'Buổi này chưa có nhân viên.'})

    earning_service.release_cash_commission(assignment=assignment)
    assignment.status = BookingAssignment.Status.CANCELLED
    assignment.response_note = reason
    assignment.responded_at = timezone.now()
    assignment.save(update_fields=['status', 'response_note', 'responded_at', 'updated_at'])
    notify_worker_removed_from_schedule(schedule, assignment.worker, reason)
    notify_customer_worker_removed(schedule, assignment.worker)

    booking = schedule.booking
    if booking.status == Booking.Status.ASSIGNED:
        booking.status = Booking.Status.PENDING
        booking.save(update_fields=['status', 'updated_at'])

    record_booking_activity(
        booking=booking,
        schedule=schedule,
        actor=actor,
        event_type=BookingActivity.EventType.WORKER_UNASSIGNED,
        message=f'Admin bỏ phân công nhân viên khỏi buổi {schedule.sequence_no}.',
        old_data={'worker_id': assignment.worker_id, 'assignment_id': assignment.id},
        new_data={'reason': reason},
    )
    return assignment


@transaction.atomic
def cancel_booking_by_admin(*, booking_id, actor, reason):
    booking = get_object_or_404(Booking.objects.select_for_update(), pk=booking_id)
    return cancel_booking_core(booking=booking, actor=actor, reason=reason, by_admin=True)


@transaction.atomic
def complete_schedule_by_admin(*, schedule_id, actor, reason, completion_note=None):
    ref = get_object_or_404(BookingSchedule.objects.only('booking_id'), pk=schedule_id)
    booking = Booking.objects.select_for_update(of=('self',)).get(pk=ref.booking_id)
    schedule = get_object_or_404(
        BookingSchedule.objects.select_for_update(of=('self',)).select_related('booking'),
        pk=schedule_id,
    )
    if schedule.status != BookingSchedule.Status.IN_PROGRESS:
        raise serializers.ValidationError(
            {'schedule': 'Chỉ được xác nhận thủ công cho buổi đang thực hiện.'}
        )
    assignment = schedule.assignments.filter(
        status=BookingAssignment.Status.ACCEPTED,
    ).select_related('worker').first()
    if not assignment:
        raise serializers.ValidationError({'assignment': 'Buổi này không có nhân viên phụ trách.'})

    now = timezone.now()
    schedule.actual_end = now
    schedule.status = BookingSchedule.Status.COMPLETED
    schedule.completion_note = completion_note or f'Admin xác nhận: {reason}'
    schedule.save(update_fields=['actual_end', 'status', 'completion_note', 'updated_at'])

    earning_service.record_schedule_earning(
        schedule=schedule,
        booking=booking,
        worker=assignment.worker,
    )
    still_active = booking.schedules.filter(
        status__in=(BookingSchedule.Status.PENDING, BookingSchedule.Status.IN_PROGRESS),
    ).exclude(pk=schedule.pk).exists()
    if not still_active and booking.status == Booking.Status.IN_PROGRESS:
        booking.status = Booking.Status.COMPLETED
        booking.save(update_fields=['status', 'updated_at'])
    if booking.status != Booking.Status.COMPLETED:
        notify_customer_schedule_completed(schedule)

    record_booking_activity(
        booking=booking,
        schedule=schedule,
        actor=actor,
        event_type=BookingActivity.EventType.CHECKED_OUT,
        message=f'Admin xác nhận hoàn thành thủ công buổi {schedule.sequence_no}.',
        old_data={'status': BookingSchedule.Status.IN_PROGRESS},
        new_data={
            'status': BookingSchedule.Status.COMPLETED,
            'reason': reason,
            'completion_note': schedule.completion_note,
        },
        metadata={'manual': True},
    )
    return schedule
