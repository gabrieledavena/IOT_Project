"""Confronto tra città: produzione media degli impianti rapportata alla loro potenza installata."""
from collections import defaultdict
from statistics import mean

from django.core.cache import cache
from django.utils import timezone

from .models import PhotovoltaicSystem
from .production import daily_energy_kwh, get_system_series

CACHE_KEY = "measured_city_yields"
# Le misure arrivano ogni minuto, ma la media di intere giornate cambia lentamente
CACHE_SECONDS = 10 * 60


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
        for day, energy in daily_energy_kwh(get_system_series(system)).items():
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
