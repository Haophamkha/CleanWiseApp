from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.bookings.models import BookingSchedule

from .assignment_service import (
    expire_unclaimed_schedules,
    handle_missed_checkouts,
)
from .models import BookingAssignment

REMINDER_LEAD_MINUTES = 60


@shared_task
def expire_unclaimed_schedules_task():
    expire_unclaimed_schedules()


@shared_task
def handle_missed_checkouts_task():
    handle_missed_checkouts()


@shared_task
def send_schedule_reminders_task():
    from apps.notifications.services import (
        notify_customer_schedule_reminder,
        notify_worker_schedule_reminder,
    )

    now = timezone.now()
    accepted = BookingAssignment.objects.filter(
        schedule=OuterRef('pk'),
        status=BookingAssignment.Status.ACCEPTED,
    )
    schedules = (
        BookingSchedule.objects
        .filter(
            status=BookingSchedule.Status.PENDING,
            reminder_sent_at__isnull=True,
            scheduled_start__gt=now,
            scheduled_start__lte=now + timedelta(minutes=REMINDER_LEAD_MINUTES),
        )
        .filter(Exists(accepted))
        .select_related('booking', 'booking__customer')
    )

    for schedule in list(schedules):
        with transaction.atomic():
            # Chỉ một lần chạy giành được quyền gửi cho mỗi buổi
            claimed = BookingSchedule.objects.filter(
                pk=schedule.pk, reminder_sent_at__isnull=True,
            ).update(reminder_sent_at=now)
            if not claimed:
                continue

            assignment = (
                schedule.assignments
                .filter(status=BookingAssignment.Status.ACCEPTED)
                .select_related('worker')
                .first()
            )
            notify_customer_schedule_reminder(schedule)
            if assignment:
                notify_worker_schedule_reminder(schedule, assignment.worker)
                
                
@shared_task
def notify_preferred_expired_task():
    from apps.notifications.services import notify_customer_preferred_expired
    from .assignment_service import OPEN_BOOKING_STATUSES

    now = timezone.now()
    accepted = BookingAssignment.objects.filter(
        schedule=OuterRef('pk'), status=BookingAssignment.Status.ACCEPTED,
    )
    schedules = (
        BookingSchedule.objects
        .filter(
            status=BookingSchedule.Status.PENDING,
            preferred_worker__isnull=False,
            preferred_worker_expires_at__lte=now,
            preferred_expiry_notified_at__isnull=True,
            booking__status__in=OPEN_BOOKING_STATUSES,
        )
        .exclude(Exists(accepted))
        .select_related('booking', 'booking__customer', 'preferred_worker')
    )
    seen = set()
    for schedule in list(schedules):
        claimed = BookingSchedule.objects.filter(
            pk=schedule.pk, preferred_expiry_notified_at__isnull=True,
        ).update(preferred_expiry_notified_at=now)
        if claimed and schedule.booking_id not in seen:
            seen.add(schedule.booking_id)
            notify_customer_preferred_expired(schedule.booking, schedule.preferred_worker)


@shared_task
def expire_unpaid_bookings_task():
    from apps.bookings.expiry_service import expire_unpaid_bookings
    expire_unpaid_bookings()
    

@shared_task
def release_held_earnings_task():
    from apps.wallets.earning_service import release_held_earnings
    release_held_earnings()
    
    

@shared_task
def process_pending_cash_payments_task():
    from apps.worker import checkin_service
    checkin_service.process_pending_cash_payments()