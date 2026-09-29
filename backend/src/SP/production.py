"""Serie di produzione ricavate dalle misure (PanelData) di impianti e community."""
from collections import defaultdict
from datetime import date, timedelta

from .models import PanelData

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


def get_system_series(system, day=None):
    """Serie minuto per minuto di un impianto: tutte le misure, o solo quelle del giorno `day`.

    Tra una lettura e la successiva i valori sono interpolati linearmente. La potenza ("value")
    è istantanea, in kW: l'energia prodotta si ottiene con energy_kwh.
    """
    readings = PanelData.objects.filter(system=system)
    if day is not None:
        readings = readings.filter(time_stamp__date=day)
    readings = list(readings.order_by("time_stamp").values_list("time_stamp", "power", "temperature", "lightness"))

    series = []
    for (prev_time, prev_power, prev_temp, prev_light), (curr_time, curr_power, curr_temp, curr_light) in zip(
        readings, readings[1:]
    ):
        minutes = round((curr_time - prev_time).total_seconds() / 60)
        if minutes <= 0:
            continue

        power_step = (curr_power - prev_power) / minutes
        temp_step = (curr_temp - prev_temp) / minutes
        light_step = (curr_light - prev_light) / minutes
        for m in range(1, minutes + 1):
            series.append({
                "timestamp": (prev_time + timedelta(minutes=m)).strftime(TIMESTAMP_FORMAT),
                "value": round(prev_power + power_step * m, 4),
                "temperature": round(prev_temp + temp_step * m, 2),
                "lightness": round(prev_light + light_step * m, 2),
            })
    return series


def get_community_series(community, day=None):
    """Potenza complessiva della community minuto per minuto (somma delle serie dei suoi impianti)."""
    totals = defaultdict(float)
    for system in community.photovoltaic_systems.all():
        for point in get_system_series(system, day):
            totals[point["timestamp"]] += point["value"]
    return [{"timestamp": timestamp, "value": round(totals[timestamp], 4)} for timestamp in sorted(totals)]


def energy_kwh(series):
    """Energia (kWh) di una serie di potenze in kW campionate ogni minuto."""
    return sum(point["value"] for point in series) / 60


def daily_energy_kwh(series):
    """Energia (kWh) prodotta in ciascun giorno della serie."""
    totals = defaultdict(float)
    for point in series:
        totals[date.fromisoformat(point["timestamp"][:10])] += point["value"]
    return {day: total / 60 for day, total in totals.items()}
