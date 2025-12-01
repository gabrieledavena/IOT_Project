from django.db import models

# Create your models here.

class Community(models.Model):

    name = models.CharField(max_length=100, unique=True, null=False, blank=False)
    latitude = models.FloatField(
        verbose_name="Latitude",
        help_text="La coordinata di latitudine in formato decimale (float).",
        null=False,
        blank=False,
    )

    longitude = models.FloatField(
        verbose_name="Longitude",
        help_text="La coordinata di longitudine in formato decimale (float).",
        null=False,
        blank=False,
    )

    def __str__(self):
        return self.name + " (" + str(self.latitude) + ", " + str(self.longitude) + ")"

    class Meta:
        verbose_name = "Community"
        verbose_name_plural = "Communities"


class User(models.Model):
    name = models.CharField(max_length=100, null=False, blank=False)
    surname = models.CharField(max_length=100, null=False, blank=False)
    community = models.ForeignKey(Community, on_delete=models.CASCADE, related_name='users')

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"

class PhotovoltaicSystem(models.Model):
    name = models.CharField(max_length=100, null=False, blank=False)
    max_power = models.FloatField(null=False, blank=False)
    area = models.FloatField(null=True, blank=True)
    brand = models.CharField(max_length=100, null=False, default='NA')
    inclination = models.IntegerField(null=True, blank=True)
    selling_rate_per_kwh = models.FloatField(null=True, blank=True)
    buying_rate_per_kwh = models.FloatField(null=True, blank=True)
    community = models.ForeignKey(Community, on_delete=models.CASCADE, related_name='photovoltaic_system')


class Intervention(models.Model):
    # Stabilisce il legame con l'impianto
    system = models.ForeignKey(
        'PhotovoltaicSystem',
        on_delete=models.CASCADE,
        related_name='interventions',  # Related name convenzionale (plurale)
        verbose_name="Impianto Fotovoltaico"
    )

    INTERVENTION_TYPES = (
        ('CLN', 'Clean Panels'),
        ('SBT', 'Substitutions of a Panel')
    )

    code = models.CharField(max_length=3, choices=INTERVENTION_TYPES, unique=True)

    date = models.DateField(null=False, blank=False)  # Rimuovo auto_now_add se vuoi inserire date passate
    notes = models.TextField(null=True, blank=True)
    cost = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"Intervento su {self.system.name} del {self.date}"

    class Meta:
        ordering = ['-date']  # Ordina per data decrescente
        verbose_name = "Intervento di Manutenzione"
        verbose_name_plural = "Interventi di Manutenzione"


class PanelData(models.Model):

    # 1. Collegamento all'impianto
    system = models.ForeignKey(
        'PhotovoltaicSystem',
        on_delete=models.CASCADE,
        related_name='panel_data',
        verbose_name="Impianto Fotovoltaico"
    )

    time_stamp = models.DateTimeField(null=False, blank=False)
    temperature = models.FloatField(null=False, blank=False)
    humidity = models.FloatField(null=False, blank=False)
    lightness = models.FloatField(null=False, blank=False)
    power = models.FloatField(null=False, blank=False)
