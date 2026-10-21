from django.conf import settings
from django.db import models


class CustomerAddress(models.Model):
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='addresses')
    label = models.CharField(max_length=100, default='Dia chi')
    receiver_name = models.CharField(max_length=150)
    receiver_phone = models.CharField(max_length=15)
    address_line = models.TextField()
    ward = models.CharField(max_length=100, blank=True, null=True)
    city = models.CharField(max_length=100)
    province_code = models.CharField(max_length=10, blank=True, default='', db_index=True)
    ward_code = models.CharField(max_length=10, blank=True, default='', db_index=True)
    latitude = models.DecimalField(max_digits=14, decimal_places=7, blank=True, null=True)
    longitude = models.DecimalField(max_digits=14, decimal_places=7, blank=True, null=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'customer_addresses'
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['customer'],
                condition=models.Q(is_default=True, is_active=True),
                name='customer_addresses_one_active_default',
            ),
        ]

    def __str__(self):
        return f"{self.label} - {self.receiver_name}"


