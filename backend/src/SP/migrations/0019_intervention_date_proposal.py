from django.db import migrations, models
from django.db.models import F


def schedule_on_the_requested_day(apps, schema_editor):
    """Fino a questa migrazione il tecnico interveniva nel giorno richiesto: è il giorno dell'intervento."""
    apps.get_model("SP", "Intervention").objects.update(scheduled_date=F("preferred_date"))


class Migration(migrations.Migration):

    dependencies = [
        ("SP", "0018_simulated_sensor"),
    ]

    operations = [
        migrations.AlterField(
            model_name="intervention",
            name="status",
            field=models.CharField(choices=[("REQ", "Richiesta inoltrata"), ("ACC", "Richiesta accettata"), ("PRO", "Nuova data proposta"), ("DON", "Intervento eseguito"), ("CAN", "Richiesta annullata")], default="REQ", max_length=3, verbose_name="Stato"),
        ),
        # Prima vuoto, poi copiato dal giorno richiesto e reso obbligatorio
        migrations.AddField(
            model_name="intervention",
            name="scheduled_date",
            field=models.DateField(blank=True, null=True, verbose_name="Giorno dell'intervento"),
        ),
        migrations.RunPython(schedule_on_the_requested_day, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="intervention",
            name="scheduled_date",
            field=models.DateField(blank=True, verbose_name="Giorno dell'intervento"),
        ),
        migrations.AddField(
            model_name="intervention",
            name="proposed_date",
            field=models.DateField(blank=True, null=True, verbose_name="Nuova data proposta"),
        ),
        migrations.AddField(
            model_name="intervention",
            name="proposed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Data della proposta"),
        ),
        migrations.AddField(
            model_name="intervention",
            name="answered_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Risposta del titolare"),
        ),
    ]
