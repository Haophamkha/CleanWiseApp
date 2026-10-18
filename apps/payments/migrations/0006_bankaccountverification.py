from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('payments', '0005_payment_account_name_payment_account_number_and_more')]

    operations = [
        migrations.CreateModel(
            name='BankAccountVerification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('link_token_xid', models.CharField(max_length=64, unique=True)),
                ('status', models.CharField(choices=[('PENDING', 'Đang xác minh'), ('VERIFIED', 'Đã xác minh'), ('FAILED', 'Xác minh thất bại'), ('EXPIRED', 'Đã hết hạn')], default='PENDING', max_length=20)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('provider_event_xid', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('payment_method', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='verification_attempts', to='payments.userpaymentmethod')),
            ],
            options={'db_table': 'bank_account_verifications', 'ordering': ['-created_at']},
        ),
        migrations.AddIndex(
            model_name='bankaccountverification',
            index=models.Index(fields=['payment_method', 'status'], name='bank_verify_method_idx'),
        ),
    ]
