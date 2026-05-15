from django.db import models
from django.contrib.auth.models import User
# Create your models here.


class Community(models.Model):

    name = models.CharField(max_length=100, unique=True, null=False, blank=False)
    city = models.ForeignKey("City", on_delete=models.CASCADE, related_name="community", verbose_name="Citta")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Community"
        verbose_name_plural = "Communities"


class Customer(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100, null=False, blank=False)
    surname = models.CharField(max_length=100, null=False, blank=False)
    community = models.ForeignKey(
        Community, on_delete=models.CASCADE, related_name="users"
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Customer"
        verbose_name_plural = "Customers"


class PhotovoltaicSystem(models.Model):
    name = models.CharField(max_length=100, null=False, blank=False)
    max_power = models.FloatField(null=False, blank=False)
    area = models.FloatField(null=True, blank=True)
    brand = models.CharField(max_length=100, null=False, default="NA")
    inclination = models.IntegerField(null=True, blank=True)
    selling_rate_per_kwh = models.FloatField(null=True, blank=True)
    buying_rate_per_kwh = models.FloatField(null=True, blank=True)
    community = models.ForeignKey(
        Community, on_delete=models.CASCADE, related_name="photovoltaic_system"
    )


class Intervention(models.Model):
    class InterventionType(models.TextChoices):
        CLEANING = "CLN", "Pulizia Pannelli"
        PANEL_SUBSTITUTION = "SBT", "Sostituzione Pannello"
        ELECTRICAL_CHECK = "ELC", "Manutenzione Elettrica e Serraggi"
        INVERTER_MAINTENANCE = "INV", "Intervento su Inverter"
        THERMOGRAPHY_INSPECTION = "INF", "Ispezione Termografica/Visiva"
        STRUCTURE_CHECK = "STR", "Controllo Strutture e Ancoraggi"
        MINOR_REPLACEMENT = "RPL", "Sostituzione Componenti Minori (Fusibili/Connettori)"
        OTHER = "OTH", "Altro"

    # Stabilisce il legame con l'impianto
    system = models.ForeignKey(
        "PhotovoltaicSystem",
        on_delete=models.CASCADE,
        related_name="interventions",  # Related name convenzionale (plurale)
        verbose_name="Impianto Fotovoltaico",
    )

    code = models.CharField(max_length=3, choices=InterventionType.choices, verbose_name="Tipo di Intervento")

    date = models.DateField(
        null=False, blank=False
    )  # Rimuovo auto_now_add se vuoi inserire date passate
    notes = models.TextField(null=True, blank=True)
    cost = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"Intervento su {self.system.name} del {self.date}"

    class Meta:
        ordering = ["-date"]  # Ordina per data decrescente
        verbose_name = "Intervento di Manutenzione"
        verbose_name_plural = "Interventi di Manutenzione"


class PanelData(models.Model):

    # 1. Collegamento all'impianto
    system = models.ForeignKey(
        "PhotovoltaicSystem",
        on_delete=models.CASCADE,
        related_name="panel_data",
        verbose_name="Impianto Fotovoltaico",
    )

    time_stamp = models.DateTimeField(null=False, blank=False)
    temperature = models.FloatField(null=False, blank=False)
    lightness = models.FloatField(null=False, blank=False)
    power = models.FloatField(null=False, blank=False)

class City(models.Model):
    name = models.CharField(max_length=255)
    province = models.CharField(max_length=100, null=True, blank=True)
    region = models.CharField(max_length=100, null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    def __str__(self):
        if self.province:
            return f"{self.name} ({self.province})"
        return self.name

    class Meta:
        verbose_name = "City"
        verbose_name_plural = "Cities"
        ordering = ['name']
