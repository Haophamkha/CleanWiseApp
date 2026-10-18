from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('payments', '0006_bankaccountverification')]

    operations = [
        migrations.AddField(
            model_name='bankaccountverification',
            name='identity_encrypted',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='bankaccountverification',
            name='mobile_encrypted',
            field=models.TextField(blank=True, default=''),
        ),
    ]
