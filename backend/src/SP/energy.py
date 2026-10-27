"""Energia prodotta da una community o da un impianto: totalizzatore, sommari per mese e per anno, impatto ambientale.

Ogni misura è la potenza media di un minuto (kW): l'energia di un periodo è la somma delle potenze diviso 60.
Le somme le fa il database, così bastano poche query anche con anni di misure; a differenza delle serie di
SP/production.py i minuti senza misure non vengono interpolati.
"""
from calendar import monthrange
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from statistics import mean

from django.db.models import Count, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import dateformat

MINUTES_PER_HOUR = 60

# Impatto ambientale dell'energia prodotta, valori indicativi:
# CO₂ emessa in media per produrre un kWh elettrico in Italia (ordine di grandezza dei fattori ISPRA, in calo
# con la crescita delle rinnovabili), CO₂ assorbita in un anno da un albero adulto e CO₂ di un'auto media per km
CO2_KG_PER_KWH = 0.25
CO2_KG_PER_TREE = 22.0
CO2_KG_PER_CAR_KM = 0.12

MONTH = "month"
YEAR = "year"


def environmental_impact(energy_kwh):
    """CO₂ evitata producendo `energy_kwh` (kg), alberi che la assorbono in un anno e km in auto che la emettono."""
    co2_kg = energy_kwh * CO2_KG_PER_KWH
    return {"co2_kg": co2_kg, "trees": co2_kg / CO2_KG_PER_TREE, "car_km": co2_kg / CO2_KG_PER_CAR_KM}


def utc_midnight(day):
    return datetime.combine(day, datetime.min.time(), tzinfo=dt_timezone.utc)


def kwh(power_sum):
    return (power_sum or 0) / MINUTES_PER_HOUR


def totalizer(readings, installed_power, today):
    """Energia prodotta (kWh) da sempre, nell'anno e nel mese di `today`, con media giornaliera, resa e impatto.

    La media giornaliera è sui giorni con misure: quelli in cui il dispositivo era spento non contano.

    None se non ci sono misure.
    """
    totals = readings.aggregate(
        total=Sum("power"),
        year=Sum("power", filter=Q(time_stamp__gte=utc_midnight(today.replace(month=1, day=1)))),
        month=Sum("power", filter=Q(time_stamp__gte=utc_midnight(today.replace(day=1)))),
        first=Min("time_stamp"),
        days=Count(TruncDate("time_stamp"), distinct=True),
    )
    if totals["first"] is None:
        return None
    total = kwh(totals["total"])
    return {
        "total": total,
        "year": kwh(totals["year"]),
        "month": kwh(totals["month"]),
        "since": totals["first"].date(),
        "days": totals["days"],
        "daily_mean": total / totals["days"],
        "specific_yield": total / installed_power if installed_power else None,
        **environmental_impact(total),
    }


def daily_energy(readings, first_day, end_day):
    """Energia (kWh) di ogni giorno con misure, da first_day (incluso) a end_day (escluso)."""
    rows = (readings.filter(time_stamp__gte=utc_midnight(first_day), time_stamp__lt=utc_midnight(end_day))
            .annotate(day=TruncDate("time_stamp")).values("day").annotate(power=Sum("power")).order_by("day"))
    return {row["day"]: kwh(row["power"]) for row in rows}


def parse_period(kind, value, today):
    """(tipo, primo giorno, giorno dopo l'ultimo) del mese "2026-10" o dell'anno "2026"; il periodo attuale se non è valido."""
    try:
        if kind == YEAR:
            first = date(int(value), 1, 1)
        else:
            kind = MONTH
            year, month = value.split("-")
            first = date(int(year), int(month), 1)
    except (AttributeError, TypeError, ValueError):
        first = None
    if first is None or first > today:
        first = date(today.year, 1, 1) if kind == YEAR else today.replace(day=1)
    if kind == YEAR:
        return kind, first, date(first.year + 1, 1, 1)
    return kind, first, first + timedelta(days=monthrange(first.year, first.month)[1])


def period_value(kind, day):
    return f"{day.year}" if kind == YEAR else f"{day:%Y-%m}"


def period_summary(readings, installed_power, kind, value, today):
    """Sommario dell'energia di un mese (barre giornaliere) o di un anno (barre mensili).

    Minimo, massimo e media sono calcolati sui giorni con misure tranne quello in corso, che è incompleto;
    il totale comprende tutti i giorni. Restituisce un dizionario pronto per JSON.
    """
    kind, first, end = parse_period(kind, value, today)
    daily = daily_energy(readings, first, end)

    if kind == MONTH:
        days = [first + timedelta(days=offset) for offset in range((end - first).days)]
        bars = [{"label": dateformat.format(day, "j M"), "energy": rounded(daily.get(day))} for day in days]
        label = dateformat.format(first, "F Y").capitalize()
    else:
        bars = []
        for month in range(1, 13):
            energies = [energy for day, energy in daily.items() if day.month == month]
            bars.append({"label": dateformat.format(date(first.year, month, 1), "M"),
                         "energy": rounded(sum(energies)) if energies else None})
        label = str(first.year)

    complete = {day: energy for day, energy in daily.items() if day != today}
    total = sum(daily.values())
    lowest = min(complete, key=complete.get) if complete else None
    highest = max(complete, key=complete.get) if complete else None
    daily_mean = mean(complete.values()) if complete else None
    first_reading = readings.aggregate(first=Min("time_stamp"))["first"]
    impact = environmental_impact(total)
    return {
        "period": kind,
        "value": period_value(kind, first),
        "label": label,
        "previous": period_value(kind, first - timedelta(days=1))
                    if first_reading and first_reading.date() < first else None,
        "next": period_value(kind, end) if end <= today else None,
        "bars": bars,
        "stats": {
            "min": rounded(complete[lowest]) if lowest else None,
            "min_day": lowest.strftime("%d/%m") if lowest else None,
            "max": rounded(complete[highest]) if highest else None,
            "max_day": highest.strftime("%d/%m") if highest else None,
            "mean": rounded(daily_mean),
            "total": rounded(total),
            "days": len(daily),
            "specific_yield": rounded(total / installed_power) if installed_power else None,
            "daily_specific_yield": rounded(daily_mean / installed_power) if installed_power and daily_mean else None,
            "co2_kg": rounded(impact["co2_kg"]),
            "trees": rounded(impact["trees"]),
            "car_km": rounded(impact["car_km"]),
        },
    }


def rounded(value):
    return None if value is None else round(value, 2)
