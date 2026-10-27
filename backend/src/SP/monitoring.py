"""Controllo degli impianti: la produzione degli ultimi due giorni confrontata con la previsione e con i vicini.

1. Se un impianto ha prodotto meno del 90% di quanto prevede il modello con il meteo reale di quei giorni,
   ha un'anomalia (per esempio pannelli sporchi).
2. Solo in quel caso lo si confronta con fino a 10 impianti vicini: se anche la maggior parte di loro ha
   un'anomalia la causa è comune alla zona e i pannelli sono solo sporchi; altrimenti è un probabile guasto.
3. Con un probabile guasto il dispositivo lava comunque i pannelli. Dopo un'ora di produzione si confronta
   l'impianto con i vicini in quella stessa ora: se produce come loro (almeno il 90%) torna OK, altrimenti
   è un guasto. Un guasto resta tale ai controlli successivi finché la produzione non torna nella norma.

Vicini sono gli impianti delle community della stessa città (prima quelli della stessa community) o, se lì
non ce ne sono con misure utilizzabili, quelli della città più vicina che ne ha.
"""
import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from statistics import median

from django.db.models import Avg, Count, F, Q
from django.utils import timezone

from forecast.predictor import predict_specific_yield, require_model

from .models import PanelData, PhotovoltaicSystem
from .production import system_daily_energy_kwh
from .weather import WeatherUnavailable, get_daily_weather

Status = PhotovoltaicSystem.Status

# Si controllano i 2 giorni completi precedenti, e ogni impianto va ricontrollato dopo 2 giorni
CHECK_DAYS = 2
CHECK_INTERVAL = timedelta(days=CHECK_DAYS)
# Anomalia: produzione inferiore di almeno il 10% alla previsione
ANOMALY_THRESHOLD = 0.9
MAX_NEIGHBOURS = 10
# Il bridge invia una misura al minuto: un giorno con meno del 90% delle misure non è confrontabile
READINGS_PER_DAY = 24 * 60
MIN_COVERAGE = 0.9
EARTH_RADIUS_KM = 6371

# Verifica del lavaggio di un probabile guasto: un'ora di produzione dopo il lavaggio, con almeno il 75% delle
# misure. Un'ora conta solo se i vicini producono almeno il 5% della loro potenza: di notte non si confronta
# nulla e si aspetta il mattino. Dopo CHECK_INTERVAL senza un'ora confrontabile si smette di cercarla.
CLEANING_TEST = timedelta(hours=1)
MIN_TEST_READINGS = 45
MIN_REFERENCE_YIELD = 0.05


@dataclass(frozen=True)
class CheckResult:
    status: Status
    ratio: float  # energia prodotta / energia prevista
    neighbours: int = 0  # vicini confrontati, solo in caso di anomalia
    anomalous_neighbours: int = 0


@dataclass(frozen=True)
class CleaningResult:
    status: Status  # OK se dopo il lavaggio produce come i vicini, BROKEN se no
    ratio: float  # resa dell'impianto / resa mediana dei vicini nell'ora confrontata
    start: datetime  # inizio dell'ora confrontata
    neighbours: int


def distance_km(city, other):
    """Distanza in linea d'aria tra due città con coordinate (formula dell'emisenoverso)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (city.latitude, city.longitude, other.latitude, other.longitude))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def nearest_comparable(system, systems, comparable):
    """Fino a MAX_NEIGHBOURS altri impianti tra `systems` per cui comparable(impianto) è vero, della città più vicina che ne ha.

    La prima città è quella dell'impianto, dove vengono prima gli impianti della stessa community.
    """
    city = system.community.city
    by_city = defaultdict(list)
    for other in systems:
        if other.id != system.id:
            by_city[other.community.city_id].append(other)
    cities = [city]
    if city.has_coordinates:
        others = {other.community.city for other in systems} - {city}
        cities += sorted((c for c in others if c.has_coordinates), key=lambda c: distance_km(city, c))

    for candidate_city in cities:
        candidates = sorted(by_city[candidate_city.id], key=lambda s: (s.community_id != system.community_id, s.id))
        found = []
        for candidate in candidates:
            if comparable(candidate):
                found.append(candidate)
                if len(found) == MAX_NEIGHBOURS:
                    break
        if found:
            return found
    return []


class SystemChecker:
    """Controlla gli impianti sui CHECK_DAYS giorni completi prima di `today`.

    Meteo e rapporti produzione/previsione sono calcolati una volta sola e riusati: lo stesso impianto
    può servire da vicino a molti altri. Solleva ForecastError se il modello di previsione non c'è.
    """

    def __init__(self, today=None):
        self.model = require_model()
        today = today or timezone.now().date()
        self.first_day = today - timedelta(days=CHECK_DAYS)
        self.last_day = today - timedelta(days=1)
        self.start = datetime.combine(self.first_day, time.min, tzinfo=dt_timezone.utc)
        self.systems = list(PhotovoltaicSystem.objects.select_related("community__city").order_by("id"))
        self._weather = {}
        self._ratios = {}

    def check(self, system):
        """Esito dei due controlli, o None se mancano le misure o il meteo per farli.

        Un guasto confermato dopo il lavaggio resta tale finché la produzione non torna nella norma (per
        esempio dopo la riparazione): non diventa di nuovo un probabile guasto, che farebbe ripetere il lavaggio.
        """
        result = self._check(system)
        if result and system.status == Status.BROKEN and result.status != Status.OK:
            return replace(result, status=Status.BROKEN)
        return result

    def _check(self, system):
        ratio = self.ratio(system)
        if ratio is None:
            return None
        if ratio >= ANOMALY_THRESHOLD:
            return CheckResult(Status.OK, ratio)

        neighbours = self.neighbours(system)
        anomalous = sum(self.ratio(neighbour) < ANOMALY_THRESHOLD for neighbour in neighbours)
        # Anomalia comune alla zona (o nessun vicino con cui confrontarsi): solo pannelli sporchi
        dirty = anomalous * 2 >= len(neighbours)
        return CheckResult(Status.DIRTY if dirty else Status.FAULT, ratio, len(neighbours), anomalous)

    def ratio(self, system):
        """Energia prodotta nei giorni controllati rispetto a quella prevista dal modello, o None se non calcolabile."""
        if system.id not in self._ratios:
            self._ratios[system.id] = self._compute_ratio(system)
        return self._ratios[system.id]

    def _compute_ratio(self, system):
        if not self.has_complete_days(system):
            return None
        city = system.community.city
        weather = self.weather(city)
        if weather is None:
            return None
        predicted = predict_specific_yield(self.model, weather, city.latitude).sum() * system.max_power
        if predicted <= 0:
            return None
        end = self.start + timedelta(days=CHECK_DAYS)
        return sum(system_daily_energy_kwh(system, self.start, end).values()) / predicted

    def has_complete_days(self, system):
        """True se in ognuno dei giorni controllati l'impianto ha inviato almeno il MIN_COVERAGE delle misure."""
        days = [self.start + timedelta(days=i) for i in range(CHECK_DAYS + 1)]
        counts = PanelData.objects.filter(system=system).aggregate(**{
            f"day{i}": Count("id", filter=Q(time_stamp__gte=days[i], time_stamp__lt=days[i + 1]))
            for i in range(CHECK_DAYS)
        })
        return min(counts.values()) >= READINGS_PER_DAY * MIN_COVERAGE

    def weather(self, city):
        """Meteo reale della città nei giorni controllati, o None se non disponibile. Una richiesta per città."""
        if city.id not in self._weather:
            try:
                weather = get_daily_weather(city, self.first_day, self.last_day)
            except WeatherUnavailable:
                weather = None
            self._weather[city.id] = weather if weather is not None and len(weather) == CHECK_DAYS else None
        return self._weather[city.id]

    def neighbours(self, system):
        """Fino a MAX_NEIGHBOURS altri impianti con un rapporto calcolabile, della città più vicina che ne ha."""
        return nearest_comparable(system, self.systems, lambda candidate: self.ratio(candidate) is not None)


class CleaningVerifier:
    """Verifica se il lavaggio ha rimesso in funzione gli impianti con un probabile guasto.

    Confronta la potenza per kW installato dell'impianto con quella mediana dei vicini senza guasti nella
    prima ora dopo il lavaggio in cui entrambi hanno le misure e i vicini producono (di giorno).
    """

    def __init__(self, now=None):
        self.now = now or timezone.now()
        systems = PhotovoltaicSystem.objects.select_related("community__city").order_by("id")
        # Riferimento solo gli impianti senza guasti
        self.healthy = list(systems.exclude(status__in=[Status.FAULT, Status.BROKEN]))

    def verify(self, system):
        """Esito del lavaggio, o None se non c'è ancora un'ora di produzione da confrontare."""
        cleaned_at = system.device.last_cleaning
        start = cleaned_at
        while start + CLEANING_TEST <= self.now and start < cleaned_at + CHECK_INTERVAL:
            result = self.compare(system, start)
            if result:
                return result
            start += CLEANING_TEST
        return None

    def compare(self, system, start):
        """Confronto nell'ora che inizia a `start`, o None se non c'è un riferimento (notte, misure mancanti)."""
        rows = (PanelData.objects
                .filter(system__in=[system, *self.healthy], time_stamp__gte=start, time_stamp__lt=start + CLEANING_TEST)
                .values("system_id").annotate(readings=Count("id"), power=Avg("power")))
        power = {row["system_id"]: row["power"] for row in rows if row["readings"] >= MIN_TEST_READINGS}
        if system.id not in power:
            return None
        neighbours = nearest_comparable(system, self.healthy, lambda candidate: candidate.id in power)
        if not neighbours:
            return None
        reference = median(power[neighbour.id] / neighbour.max_power for neighbour in neighbours)
        if reference < MIN_REFERENCE_YIELD:
            return None
        ratio = power[system.id] / system.max_power / reference
        status = Status.OK if ratio >= ANOMALY_THRESHOLD else Status.BROKEN
        return CleaningResult(status, ratio, start, len(neighbours))


def systems_to_verify():
    """Impianti con un probabile guasto i cui pannelli il dispositivo ha lavato dopo che è stato rilevato."""
    return PhotovoltaicSystem.objects.filter(
        status=Status.FAULT, device__last_cleaning__gte=F("status_changed_at"),
    ).select_related("community__city", "device")


def systems_to_check(now=None):
    """Impianti mai controllati o controllati da almeno CHECK_INTERVAL."""
    now = now or timezone.now()
    return PhotovoltaicSystem.objects.filter(Q(last_check__isnull=True) | Q(last_check__lte=now - CHECK_INTERVAL))
