# apps/complaints/models.py

from django.conf import settings
from django.db import models
from apps.common.codes import generate_code

class ComplaintIssueType(models.Model):
    class Stage(models.TextChoices):
        BEFORE_SERVICE = 'BEFORE_SERVICE', 'Trước khi thực hiện'
        IN_SERVICE = 'IN_SERVICE', 'Đang thực hiện'
        AFTER_SERVICE = 'AFTER_SERVICE', 'Sau khi hoàn thành'
        ANY = 'ANY', 'Mọi thời điểm'

    class AppliesTo(models.TextChoices):
        CUSTOMER = 'CUSTOMER', 'Khách hàng'
        WORKER = 'WORKER', 'Nhân viên'
        ANY = 'ANY', 'Cả hai'

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='')
    stage = models.CharField(max_length=30, choices=Stage.choices, default=Stage.ANY)

    # Ai được chọn lý do này khi tạo khiếu nại — tránh khách thấy lý do
    # "Khách không trả tiền" (chỉ nhân viên mới hợp lý) và ngược lại.
    applies_to = models.CharField(max_length=20, choices=AppliesTo.choices, default=AppliesTo.ANY)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'complaint_issue_types'
        ordering = ['stage', 'name']

    def __str__(self):
        return self.name


class Complaint(models.Model):
    class ReporterRole(models.TextChoices):
        CUSTOMER = 'CUSTOMER', 'Khách hàng'
        WORKER = 'WORKER', 'Nhân viên'

    class Stage(models.TextChoices):
        BEFORE_SERVICE = 'BEFORE_SERVICE', 'Trước khi thực hiện'
        IN_SERVICE = 'IN_SERVICE', 'Đang thực hiện'
        AFTER_SERVICE = 'AFTER_SERVICE', 'Sau khi hoàn thành'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Chờ xử lý'
        IN_REVIEW = 'IN_REVIEW', 'Đang xem xét'
        RESOLVED = 'RESOLVED', 'Đã xử lý'
        REJECTED = 'REJECTED', 'Bị từ chối'
        CANCELLED = 'CANCELLED', 'Đã hủy'

    class Outcome(models.TextChoices):
        NONE = '', 'Không xử lý tiền'
        REFUND_CUSTOMER = 'REFUND_CUSTOMER', 'Hoàn tiền khách'
        PAY_WORKER = 'PAY_WORKER', 'Trả tiền nhân viên'

    code = models.CharField(max_length=30, unique=True, null=True, blank=True, editable=False)
    schedule = models.ForeignKey(
        'bookings.BookingSchedule', on_delete=models.DO_NOTHING,
        related_name='complaints', null=True, blank=True,
    )
    booking = models.ForeignKey(
        'bookings.Booking', on_delete=models.DO_NOTHING, related_name='complaints',
    )

    # Người GỬI khiếu nại — khách hàng hoặc nhân viên.
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='complaints_filed',
    )
    reporter_role = models.CharField(max_length=20, choices=ReporterRole.choices)

    # Nhân viên LIÊN QUAN, tách riêng khỏi `reporter` để luôn truy vấn được
    # "mọi khiếu nại dính tới nhân viên X" bất kể ai là người gửi.
    worker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING,
        related_name='complaints_about', null=True, blank=True,
    )

    issue_type = models.ForeignKey(
        ComplaintIssueType, on_delete=models.PROTECT, related_name='complaints',
    )

    # Backend tự xác định từ Booking.status.
    stage = models.CharField(max_length=30, choices=Stage.choices)
    content = models.TextField(blank=True, default='')
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)

    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING,
        related_name='resolved_complaints', blank=True, null=True,
    )
    resolution_note = models.TextField(blank=True, null=True)
    resolved_at = models.DateTimeField(blank=True, null=True)

    # Kết quả tiền do hệ thống tính (xem resolution_service), admin không nhập.
    outcome = models.CharField(max_length=30, choices=Outcome.choices, blank=True, default='')
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)   # khách được hoàn
    customer_delta = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # + cộng / - trừ ví khách
    worker_delta = models.DecimalField(max_digits=12, decimal_places=2, default=0)    # + cộng / - trừ ví nhân viên
    shortfall = models.DecimalField(max_digits=12, decimal_places=2, default=0)       # không thu hồi được, nền tảng chịu

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'complaints'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['worker', 'created_at'], name='complaints_worker_created_idx'),
        ]

    def save(self, *args, **kwargs):
        # Chỉ sinh mã khi tạo mới. Khiếu nại cũ được điền mã bằng migration bên dưới.
        if self._state.adding and not self.code:
            self.code = generate_code(
                'KN', lambda c: Complaint.objects.filter(code=c).exists(),
            )
        super().save(*args, **kwargs)
        
    def __str__(self):
        return f'{self.booking} - {self.issue_type.name}'


def complaint_attachment_path(instance, filename):
    return f'complaints/{instance.complaint_id}/{filename}'


class ComplaintAttachment(models.Model):
    complaint = models.ForeignKey(Complaint, on_delete=models.CASCADE, related_name='attachments')
    file = models.ImageField(upload_to=complaint_attachment_path)
    file_type = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'complaint_attachments'

    def __str__(self):
        return str(self.file)