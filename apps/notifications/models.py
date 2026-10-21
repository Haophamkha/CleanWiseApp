from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Type(models.TextChoices):
        BOOKING = 'BOOKING', 'Đặt lịch'
        PAYMENT = 'PAYMENT', 'Thanh toán'
        ASSIGNMENT = 'ASSIGNMENT', 'Phân công'
        COMPLAINT = 'COMPLAINT', 'Khiếu nại'
        SYSTEM = 'SYSTEM', 'Hệ thống'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='notifications')
    title = models.CharField(max_length=150)
    message = models.TextField()
    type = models.CharField(max_length=30, choices=Type.choices, default=Type.SYSTEM)
    related_booking = models.ForeignKey('bookings.Booking', on_delete=models.DO_NOTHING, related_name='notifications', blank=True, null=True)
    related_schedule = models.ForeignKey(
        'bookings.BookingSchedule', on_delete=models.SET_NULL,
        related_name='+', blank=True, null=True,
    )
    related_worker = models.ForeignKey('authentication.WorkerProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    navigation_source = models.CharField(max_length=20, default='mine')
    target = models.CharField(max_length=20, blank=True, default='')
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notifications'
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class DeviceToken(models.Model):
    class Platform(models.TextChoices):
        IOS = 'IOS', 'iOS'
        ANDROID = 'ANDROID', 'Android'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='device_tokens')
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=10, choices=Platform.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'device_tokens'


class NotificationPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_preference',
    )
    push_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'notification_preferences'