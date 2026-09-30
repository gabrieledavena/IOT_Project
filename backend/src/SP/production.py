"""Serie di produzione ricavate dalle misure (PanelData) di impianti e community."""
import math
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
from django.db.models import CharField
from django.db.models.functions import Cast

from .models import PanelData

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
# I giorni sono in UTC, come le misure salvate
EPOCH = date(1970, 1, 1)
SECONDS_PER_DAY = 24 * 60 * 60


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


def system_daily_energy_kwh(system, start=None, end=None):
    """Energia (kWh) prodotta dall'impianto in ciascun giorno: come daily_energy_kwh(get_system_series(system)).

    Con start e/o end usa solo le misure da start (incluso) a end (escluso). Somma i punti interpolati di
    ogni intervallo tra due letture con una formula, senza costruire la serie minuto per minuto: con
    settimane di misure è decine di volte più veloce.
    """
    readings = PanelData.objects.filter(system=system)
    if start is not None:
        readings = readings.filter(time_stamp__gte=start)
    if end is not None:
        readings = readings.filter(time_stamp__lt=end)
    # Gli orari arrivano come testo ("2026-09-28 12:00:00", in UTC) e numpy li converte tutti insieme:
    # molto più veloce che creare un datetime per ogni misura
    readings = list(readings.order_by("time_stamp").values_list(Cast("time_stamp", CharField()), "power"))
    if len(readings) < 2:
        return {}
    seconds = np.array([time_stamp[:19] for time_stamp, _ in readings], dtype="datetime64[s]").astype(np.int64)
    power = np.array([value for _, value in readings])

    # Il punto m (da 1 a n) dell'intervallo tra due letture cade m minuti dopo la prima e vale p0 + (p1 - p0) * m / n
    minutes = np.round(np.diff(seconds) / 60)
    start, p0, p1 = seconds[:-1], power[:-1], power[1:]
    first_day = (start + 60) // SECONDS_PER_DAY
    last_day = (start + 60 * minutes) // SECONDS_PER_DAY

    totals = defaultdict(float)
    # Intervalli in un solo giorno (quasi tutti): la somma dei punti da 1 a n è n * p0 + (p1 - p0) * (n + 1) / 2
    same_day = (minutes > 0) & (first_day == last_day)
    days, day_of_interval = np.unique(first_day[same_day], return_inverse=True)
    interval_sums = minutes * p0 + (p1 - p0) * (minutes + 1) / 2
    for day, total in zip(days, np.bincount(day_of_interval, weights=interval_sums[same_day])):
        totals[day] += total

    # Intervalli a cavallo della mezzanotte: i punti si dividono tra i giorni
    for i in np.flatnonzero((minutes > 0) & (first_day != last_day)):
        first = 1
        for day in range(int(first_day[i]), int(last_day[i]) + 1):
            last = min(minutes[i], math.ceil(((day + 1) * SECONDS_PER_DAY - start[i]) / 60) - 1)
            count = last - first + 1
            totals[day] += count * p0[i] + (p1[i] - p0[i]) / minutes[i] * (first + last) * count / 2
            first = last + 1
    return {EPOCH + timedelta(days=int(day)): total / 60 for day, total in totals.items()}
