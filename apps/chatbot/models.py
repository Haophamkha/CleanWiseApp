import uuid

from django.conf import settings
from django.db import models
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from pgvector.django import VectorField

class HelpArticle(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Bản nháp'
        PUBLISHED = 'PUBLISHED', 'Xuất bản'
        RETIRED = 'RETIRED', 'Thu hồi'

    slug = models.SlugField(max_length=120)
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=250)
    summary = models.TextField(blank=True)
    sections = models.JSONField(default=list, help_text='Danh sách {heading, text}; văn bản thuần.')
    keywords = models.TextField(blank=True)
    role = models.CharField(max_length=20, choices=[('CUSTOMER', 'Khách hàng'), ('WORKER', 'Nhân viên')], default='CUSTOMER')
    source_document = models.CharField(max_length=250)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    effective_from = models.DateField(default=timezone.localdate)
    effective_until = models.DateField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title', '-version']
        constraints = [
            models.UniqueConstraint(fields=['slug', 'version'], name='chatbot_help_slug_version'),
            models.UniqueConstraint(fields=['slug'], condition=models.Q(status='PUBLISHED'), name='chatbot_help_one_published'),
        ]

    def __str__(self):
        return f'{self.title} v{self.version}'

    def clean(self):
        super().clean()
        if not self.sections or not isinstance(self.sections, list) or len(self.sections) > 100:
            raise ValidationError({'sections': 'Cần 1–100 mục nội dung.'})
        for section in self.sections:
            if (not isinstance(section, dict) or not isinstance(section.get('heading'), str)
                    or not isinstance(section.get('text'), str) or not section['text'].strip()
                    or len(section['text']) > 12000):
                raise ValidationError({'sections': 'Mỗi mục cần heading và text; text không rỗng, tối đa 12000 ký tự.'})
        if self.effective_until and self.effective_until < self.effective_from:
            raise ValidationError({'effective_until': 'Ngày kết thúc phải từ ngày hiệu lực trở đi.'})
        previous = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if previous and previous.published_at:
            fields = ['slug', 'version', 'title', 'summary', 'sections', 'keywords', 'role',
                      'source_document', 'effective_from', 'effective_until']
            if any(getattr(previous, name) != getattr(self, name) for name in fields):
                raise ValidationError('Bản đã xuất bản không được sửa nội dung. Tạo phiên bản mới để cập nhật.')
            if previous.status == self.Status.RETIRED and self.status != self.Status.RETIRED:
                raise ValidationError('Bản đã thu hồi không được xuất bản lại. Tạo phiên bản mới.')
            if self.status == self.Status.DRAFT:
                raise ValidationError('Bản đã xuất bản chỉ có thể giữ xuất bản hoặc thu hồi.')
        if self.status == self.Status.PUBLISHED and type(self).objects.filter(
                slug=self.slug, version__gt=self.version, published_at__isnull=False).exists():
            raise ValidationError('Không được xuất bản phiên bản cũ hơn bản đã xuất bản.')

    def save(self, *args, **kwargs):
        with transaction.atomic():
            # Serialize publication against existing revisions of the same article.
            list(type(self).objects.select_for_update().filter(slug=self.slug).values_list('pk', flat=True))
            self.full_clean()
            if self.status == self.Status.PUBLISHED:
                if not self.published_at:
                    self.published_at = timezone.now()
                type(self).objects.filter(slug=self.slug, status=self.Status.PUBLISHED).exclude(pk=self.pk).update(status=self.Status.RETIRED)
            super().save(*args, **kwargs)

    def validate_constraints(self, exclude=None):
        # Publication replaces the current published revision inside save().
        # Skip the conditional constraint during form validation, while still
        # checking slug/version. The database enforces it after retirement.
        status = self.status
        try:
            if status == self.Status.PUBLISHED:
                self.status = self.Status.DRAFT
            super().validate_constraints(exclude=exclude)
        finally:
            self.status = status


class ChatbotRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = 'RUNNING', 'Đang xử lý'
        SUCCEEDED = 'SUCCEEDED', 'Thành công'
        FAILED = 'FAILED', 'Thất bại'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey('ai_engine.ChatbotSession', on_delete=models.CASCADE, related_name='runs')
    user_message = models.OneToOneField('ai_engine.ChatbotMessage', on_delete=models.CASCADE, related_name='chatbot_run')
    assistant_message = models.OneToOneField('ai_engine.ChatbotMessage', on_delete=models.SET_NULL,
                                             null=True, blank=True, related_name='reply_run')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    model_name = models.CharField(max_length=100, blank=True)
    attempt_id = models.UUIDField(default=uuid.uuid4, editable=False)
    attempts = models.PositiveIntegerField(default=1)
    lease_expires_at = models.DateTimeField()
    error_code = models.CharField(max_length=60, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'chatbot_runs'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['conversation'], condition=models.Q(status='RUNNING'),
                                    name='chatbot_one_active_run'),
        ]

    @property
    def checkpoint_thread_id(self):
        # Separate attempts fence late writes from an expired worker. Conversation
        # context is rebuilt from committed transcript pairs, never from client input.
        return f'cleanwise-chatbot:{self.conversation_id}:{self.id}:{self.attempt_id}'


class ChatbotBookingDraft(models.Model):
    """Server-owned quote awaiting an explicit customer confirmation."""
    class Status(models.TextChoices):
        AWAITING_CONFIRMATION = 'AWAITING_CONFIRMATION', 'Chờ xác nhận'
        CONFIRMED = 'CONFIRMED', 'Đã tạo đơn'
        CANCELLED = 'CANCELLED', 'Đã hủy nháp'
        EXPIRED = 'EXPIRED', 'Hết hạn'

    conversation = models.OneToOneField('ai_engine.ChatbotSession', on_delete=models.CASCADE,
                                         related_name='booking_draft')
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chatbot_booking_drafts')
    service = models.ForeignKey('services.Service', on_delete=models.PROTECT)
    address = models.ForeignKey('addresses.CustomerAddress', on_delete=models.PROTECT)
    delivery_address = models.ForeignKey('addresses.CustomerAddress', null=True, blank=True,
                                         on_delete=models.PROTECT, related_name='chatbot_delivery_drafts')
    preferred_worker_id = models.PositiveBigIntegerField(null=True, blank=True)
    service_data = models.JSONField(default=dict)
    voucher_code = models.CharField(max_length=80, blank=True)
    payment_method = models.CharField(max_length=30)
    quote = models.JSONField(default=dict)
    prepared_run_id = models.UUIDField()
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.AWAITING_CONFIRMATION)
    booking = models.ForeignKey('bookings.Booking', null=True, blank=True, on_delete=models.SET_NULL)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class HelpChunk(models.Model):
    article = models.ForeignKey(HelpArticle, on_delete=models.CASCADE, related_name='chunks')
    section_index = models.PositiveIntegerField()
    part_index = models.PositiveIntegerField(default=0)
    heading = models.CharField(max_length=250, blank=True)
    text = models.TextField()
    content_hash = models.CharField(max_length=64)
    embedding = VectorField(dimensions=768)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['article_id', 'section_index', 'part_index']
        constraints = [
            models.UniqueConstraint(fields=['article', 'section_index', 'part_index'], name='chatbot_chunk_unique'),
        ]
