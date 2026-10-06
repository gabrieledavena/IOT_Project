import hashlib
import hmac
import secrets

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


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
    """Un'azienda: possiede gli impianti e vi accedono i suoi utenti, ognuno con il proprio account."""

    name = models.CharField(max_length=100, unique=True)
    # PROTECT: eliminare una città non deve eliminare a cascata community, clienti e misure
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="communities", verbose_name="Città")
    # Il titolare è uno degli utenti della community. Può mancare solo finché la community non ha utenti:
    # il primo che si aggiunge ne diventa titolare (vedi Customer.save). RESTRICT: il titolare non si può
    # eliminare, se non insieme alla community; prima va scelto un altro titolare.
    owner = models.OneToOneField(
        "Customer",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="owned_community",
        verbose_name="Titolare",
    )

    def __str__(self):
        return self.name

    def clean(self):
        if self.owner and self.owner.community_id != self.pk:
            raise ValidationError({"owner": "Il titolare deve essere uno degli utenti della community."})
        if self.owner is None and self.pk and self.customers.exists():
            raise ValidationError({"owner": "Scegli il titolare tra gli utenti della community."})

    class Meta:
        verbose_name = "Community"
        verbose_name_plural = "Communities"


class Customer(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    surname = models.CharField(max_length=100)
    community = models.ForeignKey(Community, on_delete=models.CASCADE, related_name="customers")

    def __str__(self):
        return f"{self.name} {self.surname}"

    def clean(self):
        if self.pk and Community.objects.filter(owner=self).exclude(pk=self.community_id).exists():
            raise ValidationError({"community": "È il titolare della sua community: prima va scelto un altro titolare."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Il primo utente di una community ne diventa il titolare
        if self.community.owner_id is None:
            self.community.owner = self
            self.community.save(update_fields=["owner"])

    @property
    def is_owner(self):
        return self.community.owner_id == self.pk

    @staticmethod
    def community_of(user):
        """Community del cliente associato all'utente, o None se l'utente non è un cliente."""
        customer = Customer.objects.select_related("community__city", "community__owner").filter(user=user).first()
        return customer.community if customer else None

    class Meta:
        verbose_name = "Customer"
        verbose_name_plural = "Customers"


class PhotovoltaicSystem(models.Model):
    class Status(models.TextChoices):
        OK = "OK", "OK"
        DIRTY = "DRT", "Pannelli sporchi"
        FAULT = "FLT", "Probabile guasto"

    name = models.CharField(max_length=100)
    max_power = models.FloatField()  # Potenza di picco installata, in kW
    area = models.FloatField(null=True, blank=True)
    brand = models.CharField(max_length=100, default="NA")
    inclination = models.IntegerField(null=True, blank=True)
    selling_rate_per_kwh = models.FloatField(null=True, blank=True)
    buying_rate_per_kwh = models.FloatField(null=True, blank=True)
    # Gli impianti appartengono alla community, non ai singoli utenti
    community = models.ForeignKey(Community, on_delete=models.CASCADE, related_name="photovoltaic_systems")
    # Esito dell'ultimo controllo automatico della produzione (vedi SP/monitoring.py)
    status = models.CharField(max_length=3, choices=Status.choices, default=Status.OK, verbose_name="Stato")
    last_check = models.DateTimeField(null=True, blank=True, verbose_name="Data ultimo controllo")
    # Ultimo cambiamento di stato: il dispositivo lo riceve via MQTT e reagisce una volta sola a ogni cambiamento
    previous_status = models.CharField(max_length=3, choices=Status.choices, blank=True, verbose_name="Stato precedente")
    status_changed_at = models.DateTimeField(null=True, blank=True, verbose_name="Data cambiamento di stato")

    def __str__(self):
        return self.name

    def set_status(self, status, checked_at):
        """Salva l'esito di un controllo; restituisce True se lo stato è cambiato."""
        changed = status != self.status
        if changed:
            self.previous_status, self.status, self.status_changed_at = self.status, status, checked_at
        self.last_check = checked_at
        self.save(update_fields=["status", "last_check", "previous_status", "status_changed_at"])
        return changed

    @property
    def can_request_intervention(self):
        """True se ha un probabile guasto non ancora affidato a un intervento.

        Il guasto è già affidato se c'è una richiesta aperta o un intervento eseguito dopo il controllo
        che lo ha rilevato: in quel caso si aspetta il prossimo controllo della produzione.
        """
        if self.status != self.Status.FAULT:
            return False
        handled = ~Q(status=Intervention.Status.DONE)
        if self.last_check:
            handled |= Q(executed_on__gte=timezone.localdate(self.last_check))
        return not self.interventions.filter(handled).exists()


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

    class Status(models.TextChoices):
        REQUESTED = "REQ", "Richiesta inoltrata"
        ACCEPTED = "ACC", "Richiesta accettata"
        DONE = "DON", "Intervento eseguito"

    system = models.ForeignKey(
        PhotovoltaicSystem,
        on_delete=models.CASCADE,
        related_name="interventions",
        verbose_name="Impianto Fotovoltaico",
    )
    status = models.CharField(max_length=3, choices=Status.choices, default=Status.REQUESTED, verbose_name="Stato")

    # Richiesta del cliente
    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="requested_interventions",
        verbose_name="Richiesto da",
    )
    requested_at = models.DateTimeField(default=timezone.now, verbose_name="Data della richiesta")
    preferred_date = models.DateField(verbose_name="Giorno richiesto dal cliente")
    customer_notes = models.TextField(blank=True, default="", verbose_name="Note del cliente")

    # Gestione: il membro dello staff che accetta la richiesta la prende in carico e ne registra l'esecuzione
    staff = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, limit_choices_to={"is_staff": True},
        related_name="managed_interventions", verbose_name="Gestito da",
    )
    code = models.CharField(max_length=3, choices=InterventionType.choices, blank=True, verbose_name="Tipo di Intervento")
    executed_on = models.DateField(null=True, blank=True, verbose_name="Data di esecuzione")
    notes = models.TextField(null=True, blank=True, verbose_name="Lavori eseguiti")
    cost = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, verbose_name="Costo (€)")

    def __str__(self):
        return f"Intervento su {self.system.name} richiesto il {timezone.localdate(self.requested_at):%d/%m/%Y}"

    def clean(self):
        errors = {}
        if self.staff and not self.staff.is_staff:
            errors["staff"] = "Gli interventi sono gestiti dai membri dello staff."
        if self.status != self.Status.REQUESTED:
            if not self.staff_id:
                errors["staff"] = "Una richiesta accettata deve essere gestita da un membro dello staff."
            if not self.code:
                errors["code"] = "Indica il tipo di intervento."
        if self.status == self.Status.DONE and not self.executed_on:
            errors["executed_on"] = "Indica il giorno in cui è stato eseguito l'intervento."
        if errors:
            raise ValidationError(errors)

    @property
    def is_done(self):
        return self.status == self.Status.DONE

    @property
    def number(self):
        """Numero dell'intervento nei report, come INT-00042."""
        return f"INT-{self.pk:05d}"

    class Meta:
        ordering = ["-requested_at"]
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

    class Meta:
        constraints = [
            # Con MQTT QoS 1 una misura può arrivare due volte: la seconda viene scartata
            models.UniqueConstraint(fields=["system", "time_stamp"], name="unique_reading_per_minute"),
        ]


class Device(models.Model):
    """Dispositivo (Arduino e bridge) installato su un impianto: invia le misure e riceve lo stato via MQTT.

    Si collega al broker con username pv-<id dell'impianto> e un token casuale; del token si salva solo
    l'hash, quindi lo si vede una volta sola, quando viene generato.
    """

    USERNAME_PREFIX = "pv-"

    system = models.OneToOneField(
        PhotovoltaicSystem, on_delete=models.CASCADE, related_name="device", verbose_name="Impianto Fotovoltaico",
    )
    token_hash = models.CharField(max_length=64)
    installed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="installed_devices",
        verbose_name="Installato da",
    )
    installed_at = models.DateTimeField(default=timezone.now, verbose_name="Data di installazione")
    # Aggiornati dal worker MQTT (SP/mqtt.py)
    online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True, verbose_name="Ultimo messaggio")
    pump_running = models.BooleanField(default=False, verbose_name="Pompa in funzione")
    last_cleaning = models.DateTimeField(null=True, blank=True, verbose_name="Ultimo lavaggio")

    def __str__(self):
        return self.username

    @property
    def username(self):
        return f"{self.USERNAME_PREFIX}{self.system_id}"

    @classmethod
    def system_id_from_username(cls, username):
        """Id dell'impianto di uno username come pv-42, o None se non è lo username di un dispositivo."""
        number = username.removeprefix(cls.USERNAME_PREFIX) if username.startswith(cls.USERNAME_PREFIX) else ""
        return int(number) if number.isdigit() else None

    @staticmethod
    def hash_token(token):
        # Il token è casuale a 160 bit: basta un hash veloce, quelli lenti servono per le password scelte dalle persone
        return hashlib.sha256(token.encode()).hexdigest()

    def new_token(self):
        """Genera un nuovo token (il precedente smette di funzionare) e lo restituisce; va poi salvato il dispositivo."""
        token = secrets.token_hex(20)
        self.token_hash = self.hash_token(token)
        return token

    def check_token(self, token):
        return hmac.compare_digest(self.token_hash, self.hash_token(token))

    class Meta:
        verbose_name = "Dispositivo"
        verbose_name_plural = "Dispositivi"
