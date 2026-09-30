from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models


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

    @property
    def has_coordinates(self):
        # Le coordinate sono opzionali (es. comuni non trovati in geonames da import_cities)
        return self.latitude is not None and self.longitude is not None

    class Meta:
        verbose_name = "City"
        verbose_name_plural = "Cities"
        ordering = ["name"]


class Community(models.Model):
    name = models.CharField(max_length=100, unique=True)
    # PROTECT: eliminare una città non deve eliminare a cascata community, clienti e misure
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="communities", verbose_name="Città")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Community"
        verbose_name_plural = "Communities"


class Customer(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    surname = models.CharField(max_length=100)
    community = models.ForeignKey(Community, on_delete=models.CASCADE, related_name="customers")

    def __str__(self):
        return self.name

    @staticmethod
    def community_of(user):
        """Community del cliente associato all'utente, o None se l'utente non è un cliente."""
        customer = Customer.objects.select_related("community__city").filter(user=user).first()
        return customer.community if customer else None

    class Meta:
        verbose_name = "Customer"
        verbose_name_plural = "Customers"


class PhotovoltaicSystem(models.Model):
    name = models.CharField(max_length=100)
    max_power = models.FloatField()  # Potenza di picco installata, in kW
    area = models.FloatField(null=True, blank=True)
    brand = models.CharField(max_length=100, default="NA")
    inclination = models.IntegerField(null=True, blank=True)
    selling_rate_per_kwh = models.FloatField(null=True, blank=True)
    buying_rate_per_kwh = models.FloatField(null=True, blank=True)
    community = models.ForeignKey(Community, on_delete=models.CASCADE, related_name="photovoltaic_systems")
    # Un cliente può avere più impianti (casa, capannone, ...), tutti nella sua community
    owner = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="photovoltaic_systems",
        verbose_name="Proprietario",
    )

    def __str__(self):
        return self.name

    def clean(self):
        if self.owner and self.community_id and self.owner.community_id != self.community_id:
            raise ValidationError({"owner": "Il proprietario deve far parte della community dell'impianto."})


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

    system = models.ForeignKey(
        PhotovoltaicSystem,
        on_delete=models.CASCADE,
        related_name="interventions",
        verbose_name="Impianto Fotovoltaico",
    )
    code = models.CharField(max_length=3, choices=InterventionType.choices, verbose_name="Tipo di Intervento")
    date = models.DateField()
    notes = models.TextField(null=True, blank=True)
    cost = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"Intervento su {self.system.name} del {self.date}"

    class Meta:
        ordering = ["-date"]
        verbose_name = "Intervento di Manutenzione"
        verbose_name_plural = "Interventi di Manutenzione"


class PanelData(models.Model):
    """Misura inviata dal bridge: media su un minuto dei sensori di un impianto."""

    system = models.ForeignKey(
        PhotovoltaicSystem,
        on_delete=models.CASCADE,
        related_name="panel_data",
        verbose_name="Impianto Fotovoltaico",
    )
    time_stamp = models.DateTimeField()
    temperature = models.FloatField()  # °C
    lightness = models.FloatField()  # lux
    power = models.FloatField()  # Potenza istantanea, in kW

    def __str__(self):
        return f"{self.system} - {self.time_stamp:%Y-%m-%d %H:%M}"
