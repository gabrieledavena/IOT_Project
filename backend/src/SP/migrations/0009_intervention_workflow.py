import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def mark_existing_as_done(apps, schema_editor):
    """Gli interventi registrati prima di questa migrazione avevano solo la data: erano già stati eseguiti."""
    Intervention = apps.get_model("SP", "Intervention")
    for intervention in Intervention.objects.all():
        intervention.status = "DON"
        intervention.executed_on = intervention.preferred_date
        intervention.save(update_fields=["status", "executed_on"])


class Migration(migrations.Migration):

    dependencies = [
        ('SP', '0008_photovoltaicsystem_status'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name='intervention',
            old_name='date',
            new_name='preferred_date',
        ),
        migrations.AlterField(
            model_name='intervention',
            name='preferred_date',
            field=models.DateField(verbose_name='Giorno richiesto dal cliente'),
        ),
        migrations.AddField(
            model_name='intervention',
            name='status',
            field=models.CharField(choices=[('REQ', 'Richiesta inoltrata'), ('ACC', 'Richiesta accettata'), ('DON', 'Intervento eseguito')], default='REQ', max_length=3, verbose_name='Stato'),
        ),
        migrations.AddField(
            model_name='intervention',
            name='requested_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='requested_interventions', to=settings.AUTH_USER_MODEL, verbose_name='Richiesto da'),
        ),
        migrations.AddField(
            model_name='intervention',
            name='requested_at',
            field=models.DateTimeField(default=django.utils.timezone.now, verbose_name='Data della richiesta'),
        ),
        migrations.AddField(
            model_name='intervention',
            name='customer_notes',
            field=models.TextField(blank=True, default='', verbose_name='Note del cliente'),
        ),
        migrations.AddField(
            model_name='intervention',
            name='staff',
            field=models.ForeignKey(blank=True, limit_choices_to={'is_staff': True}, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='managed_interventions', to=settings.AUTH_USER_MODEL, verbose_name='Gestito da'),
        ),
        migrations.AddField(
            model_name='intervention',
            name='executed_on',
            field=models.DateField(blank=True, null=True, verbose_name='Data di esecuzione'),
        ),
        migrations.AlterField(
            model_name='intervention',
            name='code',
            field=models.CharField(blank=True, choices=[('CLN', 'Pulizia Pannelli'), ('SBT', 'Sostituzione Pannello'), ('ELC', 'Manutenzione Elettrica e Serraggi'), ('INV', 'Intervento su Inverter'), ('INF', 'Ispezione Termografica/Visiva'), ('STR', 'Controllo Strutture e Ancoraggi'), ('RPL', 'Sostituzione Componenti Minori (Fusibili/Connettori)'), ('OTH', 'Altro')], max_length=3, verbose_name='Tipo di Intervento'),
        ),
        migrations.AlterField(
            model_name='intervention',
            name='notes',
            field=models.TextField(blank=True, null=True, verbose_name='Lavori eseguiti'),
        ),
        migrations.AlterField(
            model_name='intervention',
            name='cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name='Costo (€)'),
        ),
        migrations.AlterModelOptions(
            name='intervention',
            options={'ordering': ['-requested_at'], 'verbose_name': 'Intervento di Manutenzione', 'verbose_name_plural': 'Interventi di Manutenzione'},
        ),
        migrations.RunPython(mark_existing_as_done, migrations.RunPython.noop),
    ]
