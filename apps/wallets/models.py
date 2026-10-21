from django.conf import settings
from django.db import models

from apps.common.codes import generate_code

class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='wallet',
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wallets'
        constraints = [
            models.CheckConstraint(condition=models.Q(balance__gte=0), name='wallets_balance_check'),
        ]

    def __str__(self):
        return f'Wallet - {self.user}'


class WalletTransaction(models.Model):
    class Type(models.TextChoices):
        PAYMENT = 'PAYMENT', 'Thanh toán dịch vụ'
        REFUND = 'REFUND', 'Hoàn tiền'
        WITHDRAW = 'WITHDRAW', 'Rút tiền'
        ADJUSTMENT = 'ADJUSTMENT', 'Điều chỉnh'
        EARNING = 'EARNING', 'Thu nhập từ đơn'  # MỚI: tiền nhân viên nhận khi hoàn thành buổi (đơn online)
        TOPUP = 'TOPUP', 'Nạp tiền'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Đang xử lý'
        SUCCESS = 'SUCCESS', 'Thành công'
        FAILED = 'FAILED', 'Thất bại'

    # Mã hiển thị cho người dùng, dạng GD-YYMMDD-XXXXX. Tự sinh trong save().
    code = models.CharField(max_length=30, unique=True, null=True, blank=True, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.DO_NOTHING, related_name='transactions')
    type = models.CharField(max_length=20, choices=Type.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUCCESS)

    booking = models.ForeignKey(
        'bookings.Booking',
        on_delete=models.DO_NOTHING,
        related_name='wallet_transactions',
        blank=True,
        null=True,
    )
    note = models.TextField(blank=True, null=True)
    idempotency_key = models.CharField(max_length=100, unique=True, blank=True, null=True)

    class Direction(models.TextChoices):
        CREDIT = 'CREDIT', 'Cộng'
        DEBIT = 'DEBIT', 'Trừ'

    direction = models.CharField(max_length=10, choices=Direction.choices, blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wallet_transactions'
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='wallet_tx_amount_check')]
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        # Chỉ sinh mã khi tạo mới. Các lệnh tx.save(update_fields=[...]) khi cập nhật không bị ảnh hưởng.
        if self._state.adding and not self.code:
            self.code = generate_code(
                'GD', lambda c: WalletTransaction.objects.filter(code=c).exists(),
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.wallet} - {self.type} - {self.amount}'
    
    
class WorkerEarning(models.Model):
    """
    Sổ cái thu nhập của nhân viên: mỗi buổi làm hoàn thành = 1 dòng.

    - ONLINE: khách đã trả trước cho app -> app giữ tiền, cộng worker_amount
      (đã trừ hoa hồng) vào ví nhân viên.
    - CASH: nhân viên đã thu tiền mặt của khách -> không đụng ví, chỉ ghi
      commission_amount là khoản nhân viên phải nộp lại app (settled_at
      null = chưa nộp).

    Lưu commission_rate theo từng dòng để sau này đổi tỷ lệ không làm sai
    số liệu cũ.
    """

    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', 'Tiền mặt'
        ONLINE = 'ONLINE', 'Thanh toán online'

    worker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='worker_earnings')
    # OneToOne = mỗi buổi chỉ được ghi thu nhập 1 lần (check-out trùng không cộng 2 lần)
    schedule = models.OneToOneField(
        'bookings.BookingSchedule', on_delete=models.DO_NOTHING, related_name='worker_earning',
    )
    booking = models.ForeignKey('bookings.Booking', on_delete=models.DO_NOTHING, related_name='worker_earnings')
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)

    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2)
    worker_amount = models.DecimalField(max_digits=12, decimal_places=2)
    voided_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField()
    settled_at = models.DateTimeField(blank=True, null=True)
    wallet_credited_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'worker_earnings'
        constraints = [
            models.CheckConstraint(condition=models.Q(gross_amount__gte=0), name='worker_earnings_gross_check'),
            models.CheckConstraint(
                condition=models.Q(gross_amount=models.F('worker_amount') + models.F('commission_amount')),
                name='worker_earnings_split_check',
            ),
        ]
        indexes = [
            models.Index(fields=['worker', 'completed_at'], name='we_worker_completed_idx'),
            models.Index(fields=['worker', 'payment_method', 'settled_at'], name='we_worker_settle_idx'),
        ]
        ordering = ['-completed_at']

    def __str__(self):
        return f'{self.worker} - {self.booking} - {self.payment_method} - {self.worker_amount}'


class WalletTopup(models.Model):
    """Lệnh nạp tiền vào ví qua payOS (khách và nhân viên dùng chung)."""

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Chờ thanh toán'
        SUCCESS = 'SUCCESS', 'Thành công'
        EXPIRED = 'EXPIRED', 'Hết hạn'
        CANCELLED = 'CANCELLED', 'Đã hủy'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='wallet_topups')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    order_code = models.BigIntegerField(unique=True)
    payment_link_id = models.CharField(max_length=100, blank=True, default='')
    checkout_url = models.TextField(blank=True, default='')
    qr_code = models.TextField(blank=True, default='')
    link_expires_at = models.DateTimeField(blank=True, null=True)
    transaction_code = models.CharField(max_length=100, blank=True, default='')
    paid_at = models.DateTimeField(blank=True, null=True)
    wallet_transaction = models.OneToOneField(
        WalletTransaction, on_delete=models.SET_NULL, blank=True, null=True, related_name='topup',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wallet_topups'
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='wallet_topups_amount_check')]
        indexes = [models.Index(fields=['status', 'link_expires_at'], name='wallet_topup_status_idx')]
        ordering = ['-created_at']

    def __str__(self):
        return f'Topup {self.order_code} - {self.user} - {self.amount}'


class WithdrawRequest(models.Model):
    """
    Lệnh rút tiền tự động qua payOS payout. Tiền đã bị trừ khỏi ví ngay lúc tạo
    (qua WalletTransaction WITHDRAW đang PENDING); chi thành công -> tx SUCCESS,
    chi thất bại -> hoàn ví, tx FAILED.

    needs_review=True: không biết chắc tiền đã đi hay chưa -> task KHÔNG tự hoàn/chốt nữa,
    chỉ admin chốt tay sau khi tra dashboard payOS.
    """

    class Status(models.TextChoices):
        PROCESSING = 'PROCESSING', 'Đang xử lý'
        SUCCESS = 'SUCCESS', 'Thành công'
        FAILED = 'FAILED', 'Thất bại'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='withdraw_requests')
    wallet_transaction = models.OneToOneField(
        WalletTransaction, on_delete=models.DO_NOTHING, related_name='withdraw_request',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROCESSING)

    # Vừa là reference_id vừa là idempotency key gửi payOS: gọi lại không bao giờ chi 2 lần.
    reference_id = models.CharField(max_length=40, unique=True)
    payout_id = models.CharField(max_length=100, blank=True, default='')
    payout_mode = models.CharField(max_length=10, default='mock')

    # Ngân hàng nhận được chụp lại lúc rút, đổi/xóa phương thức sau đó không ảnh hưởng lệnh đang chạy.
    bank_bin = models.CharField(max_length=10)
    bank_name = models.CharField(max_length=150, blank=True, default='')
    account_holder_name = models.CharField(max_length=150, blank=True, default='')
    account_number_encrypted = models.TextField()
    account_number_last4 = models.CharField(max_length=4, blank=True, default='')

    failure_reason = models.TextField(blank=True, default='')

    # --- an toàn tiền ---
    had_unknown_result = models.BooleanField(default=False)  # từng có lần gọi payOS không rõ kết quả
    needs_review = models.BooleanField(default=False)        # cần người đối soát, task không tự xử lý
    review_note = models.TextField(blank=True, default='')
    submit_attempts = models.PositiveSmallIntegerField(default=0)

    completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'withdraw_requests'
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='withdraw_requests_amount_check')]
        indexes = [
            models.Index(fields=['status', 'created_at'], name='withdraw_status_idx'),
            models.Index(fields=['user', 'created_at'], name='withdraw_user_idx'),
            models.Index(fields=['status', 'needs_review'], name='withdraw_review_idx'),
        ]
        ordering = ['-created_at']

    @property
    def account_number_masked(self):
        return f'******{self.account_number_last4}' if self.account_number_last4 else ''

    def __str__(self):
        return f'Withdraw {self.reference_id} - {self.user} - {self.amount} - {self.status}'