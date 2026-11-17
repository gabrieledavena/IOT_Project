"""Riepiloghi per i clienti: il report di un giorno della community e l'ultima lettura di un impianto.

Li usa il bot Telegram (SP/telegram.py), ma non dipendono da Telegram: restituiscono dati, non testo.
Come in SP/energy.py le somme le fa il database e l'energia è la somma delle potenze diviso 60, senza
interpolare i minuti senza misure; i giorni vanno da mezzanotte a mezzanotte UTC, come nel resto del sito.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.db.models import Count, Max, Min, Sum
from django.utils import timezone

from .energy import environmental_impact, kwh, utc_midnight
from .models import Device, PanelData, PhotovoltaicSystem

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class SystemDay:
    system: PhotovoltaicSystem
    energy: float  # kWh
    readings: int
    peak: float | None = None  # kW, la potenza media del minuto migliore
    peak_at: datetime | None = None

    @property
    def specific_yield(self):
        """kWh per kW installato: confronta impianti di dimensioni diverse."""
        return self.energy / self.system.max_power if self.system.max_power else None


@dataclass(frozen=True)
class DayReport:
    community: object
    day: date
    end: datetime  # fine del periodo: la mezzanotte dopo il giorno o, per il giorno in corso, adesso
    systems: list
    energy: float  # kWh di tutta la community
    previous_energy: float | None  # kWh dello stesso intervallo del giorno prima, None se allora non c'erano misure
    peak: float | None  # kW di tutta la community nello stesso minuto
    peak_at: datetime | None
    temperature_min: float | None
    temperature_max: float | None
    coverage: float  # misure ricevute / minuti del periodo per gli impianti

    @property
    def complete(self):
        return self.end == utc_midnight(self.day + timedelta(days=1))

    @property
    def installed_power(self):
        return sum(item.system.max_power for item in self.systems)

    @property
    def specific_yield(self):
        return self.energy / self.installed_power if self.installed_power else None

    @property
    def change(self):
        """Variazione rispetto allo stesso intervallo del giorno prima (0.12 = +12%), o None se non confrontabile."""
        return self.energy / self.previous_energy - 1 if self.previous_energy else None

    @property
    def co2_kg(self):
        return environmental_impact(self.energy)["co2_kg"]


def day_report(community, day, now=None):
    """Report del giorno `day` della community; per il giorno in corso le misure fino a `now`.

    Il confronto con il giorno prima usa lo stesso intervallo: per oggi alle 15, ieri fino alle 15.
    """
    now = now or timezone.now()
    start = utc_midnight(day)
    end = min(start + timedelta(days=1), max(now, start))
    systems = list(community.photovoltaic_systems.order_by("name"))
    readings = PanelData.objects.filter(system__community=community)
    period = readings.filter(time_stamp__gte=start, time_stamp__lt=end)

    # Nomi diversi dai campi: un'annotazione che si chiama "power" nasconderebbe il campo alle successive
    rows = {row["system_id"]: row for row in period.values("system_id").annotate(
        power_sum=Sum("power"), count=Count("id"), peak_power=Max("power"),
    )}
    items = []
    for system in systems:
        row = rows.get(system.id)
        if row is None:
            items.append(SystemDay(system, 0.0, 0))
            continue
        # Il primo minuto con la potenza massima (pochi impianti per community: una query ciascuno)
        peak_at = (period.filter(system=system, power=row["peak_power"])
                   .order_by("time_stamp").values_list("time_stamp", flat=True).first())
        items.append(SystemDay(system, kwh(row["power_sum"]), row["count"], row["peak_power"], peak_at))

    totals = period.aggregate(temperature_min=Min("temperature"), temperature_max=Max("temperature"))
    community_peak = (period.values("time_stamp").annotate(total=Sum("power"))
                      .order_by("-total", "time_stamp").first())
    previous = readings.filter(time_stamp__gte=start - timedelta(days=1), time_stamp__lt=end - timedelta(days=1))
    previous_power = previous.aggregate(power=Sum("power"))["power"]
    minutes = (end - start).total_seconds() / 60 * len(systems)

    return DayReport(
        community=community,
        day=day,
        end=end,
        systems=items,
        energy=sum(item.energy for item in items),
        previous_energy=kwh(previous_power) if previous_power is not None else None,
        peak=community_peak["total"] if community_peak else None,
        peak_at=community_peak["time_stamp"] if community_peak else None,
        temperature_min=totals["temperature_min"],
        temperature_max=totals["temperature_max"],
        coverage=min(sum(item.readings for item in items) / minutes, 1.0) if minutes else 0.0,
    )


@dataclass(frozen=True)
class LatestReading:
    system: PhotovoltaicSystem
    reading: PanelData | None  # None se l'impianto non ha ancora inviato misure
    today_energy: float  # kWh dalla mezzanotte
    device: Device | None  # None se sull'impianto non è installato un dispositivo

    @property
    def power_share(self):
        """Potenza dell'ultima lettura rispetto a quella installata (0.8 = 80%)."""
        if self.reading is None or not self.system.max_power:
            return None
        return self.reading.power / self.system.max_power


def latest_reading(system, now=None):
    """Ultima misura dell'impianto, con l'energia prodotta oggi e il dispositivo che la invia."""
    now = now or timezone.now()
    readings = PanelData.objects.filter(system=system)
    # L'indice unico (impianto, istante) rende veloce l'ultima misura anche con milioni di righe
    reading = readings.order_by("-time_stamp").first()
    today = readings.filter(time_stamp__gte=utc_midnight(now.date()), time_stamp__lt=now).aggregate(power=Sum("power"))
    return LatestReading(system, reading, kwh(today["power"]), Device.objects.filter(system=system).first())
