from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('payments', '0007_tingee_verification_inputs')]

    operations = [
        migrations.AlterField(
            model_name='userpaymentmethod',
            name='verification_status',
            field=models.CharField(
                choices=[
                    ('UNVERIFIED', 'Chưa xác minh'),
                    ('PENDING', 'Đang xác minh'),
                    ('VERIFIED', 'Đã xác minh'),
                    ('SANDBOX_VERIFIED', 'Đã xác minh Sandbox'),
                    ('FAILED', 'Xác minh thất bại'),
                    ('DISCONNECTED', 'Đã ngắt liên kết'),
                ],
                default='UNVERIFIED',
                max_length=20,
            ),
        ),
    ]
