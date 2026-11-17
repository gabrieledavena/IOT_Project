from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("SP", "0016_staff_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="telegram_chat_id",
            field=models.BigIntegerField(blank=True, null=True, unique=True, verbose_name="Chat Telegram"),
        ),
        migrations.AddField(
            model_name="customer",
            name="telegram_code",
            field=models.CharField(blank=True, editable=False, max_length=32, null=True, unique=True),
        ),
    ]
