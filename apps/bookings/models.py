from django.conf import settings
from django.db import models


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Chờ nhận việc'
        ASSIGNED = 'ASSIGNED', 'Đã có nhân viên nhận'
        IN_PROGRESS = 'IN_PROGRESS', 'Đang thực hiện'
        COMPLETED = 'COMPLETED', 'Hoàn thành toàn bộ'
        CANCELLED = 'CANCELLED', 'Đã hủy'
        FAILED = 'FAILED', 'Thất bại'

    class PaymentStatus(models.TextChoices):
        UNPAID = 'UNPAID', 'Chưa thanh toán'
        PAID = 'PAID', 'Đã thanh toán'
        REFUNDED = 'REFUNDED', 'Đã hoàn tiền'

    booking_code = models.CharField(max_length=30, unique=True)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='bookings')
    service = models.ForeignKey('services.Service', on_delete=models.DO_NOTHING, related_name='bookings')
    user_voucher = models.ForeignKey(
        'vouchers.UserVoucher',
        on_delete=models.DO_NOTHING,
        related_name='bookings',
        blank=True,
        null=True,
    )
    service_data = models.JSONField()
    address = models.ForeignKey('addresses.CustomerAddress', on_delete=models.DO_NOTHING, related_name='bookings')
    delivery_address = models.ForeignKey(
        'addresses.CustomerAddress',
        on_delete=models.DO_NOTHING,
        related_name='delivery_bookings',
        blank=True,
        null=True,
    )
    note = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    payment_status = models.CharField(max_length=30, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID)

    price_breakdown = models.JSONField(blank=True, null=True)
    subtotal_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        related_name='cancelled_bookings',
        blank=True,
        null=True,
    )
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancel_reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bookings'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(subtotal_amount__isnull=True) | models.Q(subtotal_amount__gte=0),
                name='bookings_subtotal_amount_check',
            ),
            models.CheckConstraint(condition=models.Q(discount_amount__gte=0), name='bookings_discount_amount_check'),
            models.CheckConstraint(
                condition=models.Q(total_amount__isnull=True) | models.Q(total_amount__gte=0),
                name='bookings_total_amount_check',
            ),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return self.booking_code


class BookingSchedule(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Chờ thực hiện'
        IN_PROGRESS = 'IN_PROGRESS', 'Đang làm buổi này'
        COMPLETED = 'COMPLETED', 'Đã xong buổi này'
        CANCELLED = 'CANCELLED', 'Hủy buổi này'
        MISSED = 'MISSED', 'Bỏ lỡ buổi này'

    booking = models.ForeignKey(Booking, on_delete=models.DO_NOTHING, related_name='schedules')
    sequence_no = models.IntegerField()
    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    actual_start = models.DateTimeField(blank=True, null=True)
    actual_end = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    note = models.TextField(blank=True, null=True)
    preferred_worker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='preferred_bookings',
        blank=True,
        null=True,
    )
    preferred_worker_expires_at = models.DateTimeField(blank=True, null=True)
    preferred_expiry_notified_at = models.DateTimeField(null=True, blank=True)
    completion_note = models.TextField(blank=True, null=True)

    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        related_name='cancelled_booking_schedules',
        blank=True,
        null=True,
    )
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancel_reason = models.TextField(blank=True, null=True)
    reminder_sent_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'booking_schedules'
        constraints = [
            models.UniqueConstraint(
                fields=['booking', 'sequence_no'],
                name='booking_schedules_booking_sequence_unique',
            ),
            models.CheckConstraint(
                condition=models.Q(sequence_no__gt=0),
                name='booking_schedules_sequence_no_check',
            ),
            models.CheckConstraint(
                condition=models.Q(scheduled_start__lt=models.F('scheduled_end')),
                name='booking_schedules_scheduled_time_check',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(actual_end__isnull=True)
                    | models.Q(actual_start__isnull=True)
                    | models.Q(actual_start__lte=models.F('actual_end'))
                ),
                name='booking_schedules_actual_time_check',
            ),
        ]
        indexes = [
            models.Index(fields=['scheduled_start'], name='bs_scheduled_start_idx'),
            models.Index(fields=['status', 'scheduled_start'], name='bs_status_start_idx'),
        ]
        ordering = ['scheduled_start']

    def __str__(self):
        return f"{self.booking} - buổi {self.sequence_no}"


class BookingScheduleImage(models.Model):
    class ImageType(models.TextChoices):
        BEFORE = 'BEFORE', 'Trước khi làm'
        AFTER = 'AFTER', 'Sau khi làm'
        ISSUE = 'ISSUE', 'Vấn đề phát sinh'
        OTHER = 'OTHER', 'Khác'

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        related_name='uploaded_schedule_images',
    )
    schedule = models.ForeignKey(BookingSchedule, on_delete=models.DO_NOTHING, related_name='images')
    image_type = models.CharField(max_length=20, choices=ImageType.choices, default=ImageType.OTHER)
    image = models.CharField(max_length=255)
    note = models.TextField(blank=True, null=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'booking_schedule_images'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(sort_order__gte=0),
                name='booking_schedule_images_sort_order_check',
            ),
        ]
        ordering = ['schedule_id', 'sort_order', 'created_at', 'id']

    def __str__(self):
        return f"{self.schedule} - {self.image_type}"


class BookingActivity(models.Model):
    class EventType(models.TextChoices):
        BOOKING_CREATED = 'BOOKING_CREATED', 'Tạo đơn'
        BOOKING_UPDATED = 'BOOKING_UPDATED', 'Cập nhật đơn'
        PAYMENT_UPDATED = 'PAYMENT_UPDATED', 'Cập nhật thanh toán'
        WORKER_CLAIMED = 'WORKER_CLAIMED', 'Nhân viên nhận việc'
        WORKER_ASSIGNED = 'WORKER_ASSIGNED', 'Admin gán nhân viên'
        WORKER_REASSIGNED = 'WORKER_REASSIGNED', 'Admin đổi nhân viên'
        WORKER_UNASSIGNED = 'WORKER_UNASSIGNED', 'Admin bỏ phân công'
        WORKER_CANCELLED = 'WORKER_CANCELLED', 'Nhân viên hủy nhận việc'
        SCHEDULE_RESCHEDULED = 'SCHEDULE_RESCHEDULED', 'Đổi lịch'
        CHECKED_IN = 'CHECKED_IN', 'Check-in'
        CHECKED_OUT = 'CHECKED_OUT', 'Check-out'
        BOOKING_CANCELLED = 'BOOKING_CANCELLED', 'Hủy đơn'
        BOOKING_FAILED = 'BOOKING_FAILED', 'Đơn thất bại'
        COMPLAINT_CREATED = 'COMPLAINT_CREATED', 'Tạo khiếu nại'
        REFUND_CREATED = 'REFUND_CREATED', 'Tạo yêu cầu hoàn tiền'
        REFUND_COMPLETED = 'REFUND_COMPLETED', 'Đã hoàn tiền'

    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='activities',
    )
    schedule = models.ForeignKey(
        BookingSchedule,
        on_delete=models.SET_NULL,
        related_name='activities',
        blank=True,
        null=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='booking_activities',
        blank=True,
        null=True,
    )
    event_type = models.CharField(max_length=40, choices=EventType.choices)
    message = models.TextField()
    old_data = models.JSONField(blank=True, null=True)
    new_data = models.JSONField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'booking_activities'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['booking', 'created_at'], name='ba_booking_created_idx'),
            models.Index(fields=['schedule', 'created_at'], name='ba_schedule_created_idx'),
            models.Index(fields=['event_type'], name='ba_event_type_idx'),
        ]

    def __str__(self):
        return f'{self.booking.booking_code} - {self.event_type}'
