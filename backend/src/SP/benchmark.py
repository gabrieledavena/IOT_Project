"""Confronto tra città: produzione media degli impianti rapportata alla loro potenza installata."""
from collections import defaultdict
from statistics import mean

from django.core.cache import cache
from django.utils import timezone

from forecast.reference_data import load_reference_dataset

from .models import PhotovoltaicSystem
from .production import system_daily_energy_kwh

CACHE_KEY = "measured_city_yields"
# Le misure arrivano ogni minuto, ma la media di intere giornate cambia lentamente
CACHE_SECONDS = 10 * 60

REFERENCE_CACHE_KEY = "reference_yields"
# Il dataset di riferimento cambia solo quando viene scaricato di nuovo
REFERENCE_CACHE_SECONDS = 24 * 60 * 60


def measured_city_yields():
    """Resa misurata per città: {city_id: statistiche} per le città con impianti che hanno misure.

    La resa è l'energia prodotta in un giorno per ogni kW installato (kWh/kWp), mediata su tutti
    gli impianti della città e su tutte le giornate complete (la giornata in corso è esclusa).
    """
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    today_utc = timezone.now().date()
    per_city = defaultdict(lambda: {"yields": [], "systems": set(), "days": set()})
    systems = (
        PhotovoltaicSystem.objects.filter(max_power__gt=0, panel_data__isnull=False)
        .distinct()
        .select_related("community")
    )
    for system in systems:
        for day, energy in system_daily_energy_kwh(system).items():
            if day < today_utc:
                city = per_city[system.community.city_id]
                city["yields"].append(energy / system.max_power)
                city["systems"].add(system.id)
                city["days"].add(day)

    result = {
        city_id: {
            "specific_yield": mean(values["yields"]),
            "systems": len(values["systems"]),
            "days": len(values["days"]),
            "first_day": min(values["days"]),
            "last_day": max(values["days"]),
        }
        for city_id, values in per_city.items()
    }
    cache.set(CACHE_KEY, result, CACHE_SECONDS)
    return result


def reference_yields():
    """Resa attesa (kWh/kWp al giorno) nelle località del dataset di riferimento, in tutta Italia.

    È la media di tutte le giornate del dataset, cioè la media annua di un impianto da 1 kWp
    calcolata da PVGIS con l'irraggiamento misurato dai satelliti: non dipende dalle community.
    """
    cached = cache.get(REFERENCE_CACHE_KEY)
    if cached is not None:
        return cached

    means = load_reference_dataset().groupby(["location", "latitude", "longitude"])["specific_yield"].mean()
    result = [
        {"name": name, "latitude": latitude, "longitude": longitude, "specific_yield": specific_yield}
        for (name, latitude, longitude), specific_yield in means.items()
    ]
    cache.set(REFERENCE_CACHE_KEY, result, REFERENCE_CACHE_SECONDS)
    return result
