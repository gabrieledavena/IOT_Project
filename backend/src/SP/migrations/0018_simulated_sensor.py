import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("SP", "0017_customer_telegram"),
    ]

    operations = [
        migrations.CreateModel(
            name="SimulatedSensor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("power", models.FloatField(verbose_name="Potenza (kW)")),
                ("temperature", models.FloatField(verbose_name="Temperatura (°C)")),
                ("lightness", models.FloatField(verbose_name="Luce (lux)")),
                ("started_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="Simulato dal")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Valori cambiati il")),
                ("system", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE, related_name="simulated_sensor",
                    to="SP.photovoltaicsystem", verbose_name="Impianto Fotovoltaico",
                )),
            ],
            options={
                "verbose_name": "Sensore simulato",
                "verbose_name_plural": "Sensori simulati",
            },
        ),
    ]
