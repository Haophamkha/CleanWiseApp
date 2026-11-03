from django.conf import settings
from django.db import models


class MonthlyVipReward(models.Model):
    """One thank-you reward per customer/report month, across all voucher codes."""
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    month = models.DateField()
    user_voucher = models.OneToOneField('vouchers.UserVoucher', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['customer', 'month'], name='vip_reward_customer_month_unique')]
