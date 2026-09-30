"""Controllo degli impianti: la produzione degli ultimi due giorni confrontata con la previsione e con i vicini.

1. Se un impianto ha prodotto meno del 90% di quanto prevede il modello con il meteo reale di quei giorni,
   ha un'anomalia (per esempio pannelli sporchi).
2. Solo in quel caso lo si confronta con fino a 10 impianti vicini: se anche la maggior parte di loro ha
   un'anomalia la causa è comune alla zona e i pannelli sono solo sporchi; altrimenti è un probabile guasto.

Vicini sono gli impianti delle community della stessa città (prima quelli della stessa community) o, se lì
non ce ne sono con misure utilizzabili, quelli della città più vicina che ne ha.
"""
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.db.models import Count, Q
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


@dataclass(frozen=True)
class CheckResult:
    status: Status
    ratio: float  # energia prodotta / energia prevista
    neighbours: int = 0  # vicini confrontati, solo in caso di anomalia
    anomalous_neighbours: int = 0


def distance_km(city, other):
    """Distanza in linea d'aria tra due città con coordinate (formula dell'emisenoverso)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (city.latitude, city.longitude, other.latitude, other.longitude))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


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
        """Esito dei due controlli, o None se mancano le misure o il meteo per farli."""
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
        """Fino a MAX_NEIGHBOURS altri impianti con un rapporto calcolabile, della città più vicina che ne ha.

        La prima città è quella dell'impianto, dove vengono prima gli impianti della stessa community.
        """
        city = system.community.city
        by_city = defaultdict(list)
        for other in self.systems:
            if other.id != system.id:
                by_city[other.community.city_id].append(other)
        cities = [city]
        if city.has_coordinates:
            others = {other.community.city for other in self.systems} - {city}
            cities += sorted((c for c in others if c.has_coordinates), key=lambda c: distance_km(city, c))

        for candidate_city in cities:
            candidates = sorted(by_city[candidate_city.id], key=lambda s: (s.community_id != system.community_id, s.id))
            comparable = []
            for candidate in candidates:
                if self.ratio(candidate) is not None:
                    comparable.append(candidate)
                    if len(comparable) == MAX_NEIGHBOURS:
                        break
            if comparable:
                return comparable
        return []


def systems_to_check(now=None):
    """Impianti mai controllati o controllati da almeno CHECK_INTERVAL."""
    now = now or timezone.now()
    return PhotovoltaicSystem.objects.filter(Q(last_check__isnull=True) | Q(last_check__lte=now - CHECK_INTERVAL))
