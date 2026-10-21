from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.authentication.models import WorkerProfile
from apps.bookings.models import Booking, BookingSchedule
from apps.complaints.models import Complaint
from apps.payments.models import Payment
from apps.worker.models import BookingAssignment
from django.db import transaction

from .services import (
    notify_customer_booking_cancelled,
    notify_customer_booking_completed,
    notify_customer_booking_failed,
    notify_complaint_rejected,
    notify_complaint_resolved,
    notify_customer_worker_assigned,
    notify_payment_failed,
    notify_payment_success,
    notify_worker_profile_approved,
    notify_worker_profile_rejected,
)


def _cache_old_status(sender_model):
    def wrapper(sender, instance, **kwargs):
        if instance.pk:
            try:
                instance._old_status = sender_model.objects.get(pk=instance.pk).status
            except sender_model.DoesNotExist:
                instance._old_status = None
        else:
            instance._old_status = None
    return wrapper


# ===================== BOOKING =====================

pre_save.connect(_cache_old_status(Booking), sender=Booking)


@receiver(post_save, sender=Booking)
def handle_booking_status_change(sender, instance, created, **kwargs):
    old_status = getattr(instance, '_old_status', None)
    new_status = instance.status

    if old_status == new_status:
        return

    if new_status == Booking.Status.CANCELLED:
        if instance.cancelled_by_id != instance.customer_id:
            notify_customer_booking_cancelled(instance)

    elif new_status == Booking.Status.COMPLETED:
        notify_customer_booking_completed(instance)

    elif new_status == Booking.Status.FAILED:
        notify_customer_booking_failed(instance)


# ===================== BOOKING ASSIGNMENT (worker nhận lịch) =====================

pre_save.connect(_cache_old_status(BookingAssignment), sender=BookingAssignment)


@receiver(post_save, sender=BookingAssignment)
def handle_assignment_status_change(sender, instance, created, **kwargs):
    # Cờ do claim_booking_package gắn tạm trên instance (không lưu DB) để
    # bỏ qua thông báo lẻ từng buổi khi nhận nhiều buổi cùng lúc — nơi gọi
    # tự gửi 1 thông báo gộp sau vòng lặp (notify_customer_worker_assigned_batch).
    if getattr(instance, '_skip_assignment_notify', False):
        return

    old_status = getattr(instance, '_old_status', None)
    new_status = instance.status

    if old_status == new_status:
        return

    if new_status == BookingAssignment.Status.ACCEPTED:
        transaction.on_commit(lambda: notify_customer_worker_assigned(instance))


# ===================== PAYMENT =====================

pre_save.connect(_cache_old_status(Payment), sender=Payment)


@receiver(post_save, sender=Payment)
def handle_payment_status_change(sender, instance, created, **kwargs):
    old_status = getattr(instance, '_old_status', None)
    new_status = instance.status

    if old_status == new_status:
        return

    if new_status == Payment.Status.SUCCESS:
        notify_payment_success(instance)
    elif new_status == Payment.Status.FAILED:
        notify_payment_failed(instance)


# ===================== COMPLAINT =====================

pre_save.connect(_cache_old_status(Complaint), sender=Complaint)


@receiver(post_save, sender=Complaint)
def handle_complaint_status_change(sender, instance, created, **kwargs):
    old_status = getattr(instance, '_old_status', None)
    new_status = instance.status

    if old_status == new_status:
        return

    if new_status == Complaint.Status.RESOLVED:
        notify_complaint_resolved(instance)
    elif new_status == Complaint.Status.REJECTED:
        notify_complaint_rejected(instance)


# ===================== WORKER PROFILE =====================

pre_save.connect(_cache_old_status(WorkerProfile), sender=WorkerProfile, weak=False)


@receiver(post_save, sender=WorkerProfile)
def handle_worker_profile_status_change(sender, instance, created, **kwargs):
    old_status = getattr(instance, '_old_status', None)
    new_status = instance.status

    if old_status == new_status:
        return

    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from .models import Notification
    from .realtime import push_profile_review_changed, push_unread_count

    if new_status == WorkerProfile.Status.PENDING:
        name = f'{instance.user.last_name} {instance.user.first_name}'.strip() or instance.user.username
        admins = get_user_model().objects.filter(Q(role='ADMIN') | Q(is_superuser=True), is_active=True)
        for admin in admins:
            Notification.objects.create(user=admin, title='Hồ sơ mới chờ duyệt',
                message=f'{name} vừa gửi hồ sơ xét duyệt.', related_worker=instance)
            push_unread_count(admin.id)
    push_profile_review_changed(instance.pk)
    if new_status == WorkerProfile.Status.ACTIVE:
        notify_worker_profile_approved(instance)
    elif new_status == WorkerProfile.Status.REJECTED:
        notify_worker_profile_rejected(instance)