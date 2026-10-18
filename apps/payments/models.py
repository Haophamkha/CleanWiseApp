from django.conf import settings
from django.db import models


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = 'CASH', 'Tiền mặt'
        BANK_TRANSFER = 'BANK_TRANSFER', 'Chuyển khoản'
        MOMO = 'MOMO', 'MoMo'
        VNPAY = 'VNPAY', 'VNPay'
        CARD = 'CARD', 'Thẻ'
        WALLET = 'WALLET', 'Ví CleanWise'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Chờ thanh toán'
        SUCCESS = 'SUCCESS', 'Thành công'
        FAILED = 'FAILED', 'Thất bại'
        CANCELLED = 'CANCELLED', 'Đã hủy'
        REFUNDED = 'REFUNDED', 'Đã hoàn tiền'

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='payments')
    booking = models.ForeignKey('bookings.Booking', on_delete=models.DO_NOTHING, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=30, choices=Method.choices)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    transaction_code = models.CharField(max_length=100, unique=True, blank=True, null=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    failure_reason = models.TextField(blank=True, null=True)
    order_code = models.BigIntegerField(unique=True, blank=True, null=True)
    payment_link_id = models.CharField(max_length=100, blank=True, default='')
    checkout_url = models.TextField(blank=True, default='')
    qr_code = models.TextField(blank=True, default='')
    link_expires_at = models.DateTimeField(blank=True, null=True)
    bank_bin = models.CharField(max_length=10, blank=True, default='')
    account_number = models.CharField(max_length=30, blank=True, default='')
    account_name = models.CharField(max_length=150, blank=True, default='')
    transfer_content = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payments'
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='payments_amount_check')]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.booking} - {self.amount}'


class UserPaymentMethod(models.Model):
    class MethodType(models.TextChoices):
        BANK_ACCOUNT = 'BANK_ACCOUNT', 'Tài khoản ngân hàng'
        MOMO = 'MOMO', 'MoMo'
        VNPAY = 'VNPAY', 'VNPAY'

    class UsageType(models.TextChoices):
        PAYMENT = 'PAYMENT', 'Thanh toán'
        PAYOUT = 'PAYOUT', 'Nhận tiền'

    class VerificationStatus(models.TextChoices):
        UNVERIFIED = 'UNVERIFIED', 'Chưa xác minh'
        PENDING = 'PENDING', 'Đang xác minh'
        VERIFIED = 'VERIFIED', 'Đã xác minh'
        SANDBOX_VERIFIED = 'SANDBOX_VERIFIED', 'Đã xác minh Sandbox'
        FAILED = 'FAILED', 'Xác minh thất bại'
        DISCONNECTED = 'DISCONNECTED', 'Đã ngắt liên kết'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='payment_methods',
    )
    method_type = models.CharField(max_length=30, choices=MethodType.choices)
    usage_type = models.CharField(max_length=20, choices=UsageType.choices)
    display_name = models.CharField(max_length=100, blank=True)
    bank_bin = models.CharField(max_length=10, blank=True)
    bank_code = models.CharField(max_length=30, blank=True)
    bank_name = models.CharField(max_length=150, blank=True)
    account_holder_name = models.CharField(max_length=150, blank=True)
    account_number_encrypted = models.TextField(blank=True)
    account_number_last4 = models.CharField(max_length=4, blank=True)
    provider_reference = models.CharField(max_length=255, blank=True)
    verification_status = models.CharField(
        max_length=20,
        choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED,
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_payment_methods'
        ordering = ['-is_default', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'usage_type'],
                condition=models.Q(is_default=True, is_active=True),
                name='payment_methods_one_active_default',
            ),
        ]
        indexes = [
            models.Index(
                fields=['user', 'usage_type', 'is_active'],
                name='payment_method_user_idx',
            ),
        ]

    @property
    def account_number_masked(self):
        if not self.account_number_last4:
            return ''
        return f'******{self.account_number_last4}'

    def __str__(self):
        return f'{self.user} - {self.get_method_type_display()}'


class BankAccountVerification(models.Model):
    """One bank-link attempt; provider confirmation inputs are encrypted at rest."""

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Đang xác minh'
        VERIFIED = 'VERIFIED', 'Đã xác minh'
        FAILED = 'FAILED', 'Xác minh thất bại'
        EXPIRED = 'EXPIRED', 'Đã hết hạn'

    payment_method = models.ForeignKey(
        UserPaymentMethod, on_delete=models.CASCADE, related_name='verification_attempts',
    )
    link_token_xid = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField(null=True, blank=True)
    provider_event_xid = models.CharField(max_length=64, blank=True, default='')
    identity_encrypted = models.TextField(blank=True, default='')
    mobile_encrypted = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bank_account_verifications'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['payment_method', 'status'], name='bank_verify_method_idx')]
