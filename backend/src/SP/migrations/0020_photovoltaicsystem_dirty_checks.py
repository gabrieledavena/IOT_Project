from django.db import migrations, models


def count_the_current_dirty_panels(apps, schema_editor):
    """Gli impianti già con i pannelli sporchi lo sono almeno dall'ultimo controllo."""
    apps.get_model("SP", "PhotovoltaicSystem").objects.filter(status="DRT").update(dirty_checks=1)


class Migration(migrations.Migration):

    dependencies = [
        ("SP", "0019_intervention_date_proposal"),
    ]

    operations = [
        migrations.AddField(
            model_name="photovoltaicsystem",
            name="dirty_checks",
            field=models.PositiveSmallIntegerField(default=0, verbose_name="Controlli di fila con pannelli sporchi"),
        ),
        migrations.RunPython(count_the_current_dirty_panels, migrations.RunPython.noop),
    ]
