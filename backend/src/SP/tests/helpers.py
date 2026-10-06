"""Dati di prova condivisi dai test delle app SP, forecast e roi."""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from django.contrib.auth.models import User

from forecast.predictor import FEATURE_COLUMNS
from SP.models import City, Community, Customer, Device, PanelData, PhotovoltaicSystem
from SP.weather import WEATHER_COLUMNS

DEFAULT_WEATHER = {
    "solar_radiation": 18.3,
    "temp_max": 26.1,
    "temp_min": 9.6,
    "precipitation": 0.0,
    "wind_speed": 12.5,
    "cloud_cover": 3.0,
    "daylight_duration": 42480.0,
    "snowfall": 0.0,
}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def create_city(name="Modena", latitude=44.6471, longitude=10.9252, **fields):
    return City.objects.create(name=name, latitude=latitude, longitude=longitude, **fields)


def create_community(name="Community A", city=None):
    return Community.objects.create(name=name, city=city or create_city())


def create_customer(community, username="mario"):
    user = User.objects.create_user(username=username, password="test-password")
    Customer.objects.create(user=user, name="Mario", surname="Rossi", community=community)
    return user


def create_system(community, name="System A", max_power=4.0):
    return PhotovoltaicSystem.objects.create(name=name, max_power=max_power, community=community)


def install_device(system):
    """Installa il dispositivo sull'impianto; restituisce il dispositivo e il suo token."""
    device = Device(system=system)
    token = device.new_token()
    device.save()
    return device, token


def add_readings(system, start, powers, step_minutes=1, temperature=20.0, lightness=500.0):
    """Una misura ogni step_minutes minuti a partire da start, con le potenze (kW) indicate."""
    PanelData.objects.bulk_create([
        PanelData(
            system=system,
            time_stamp=start + timedelta(minutes=i * step_minutes),
            temperature=temperature,
            lightness=lightness,
            power=power,
        )
        for i, power in enumerate(powers)
    ])


def fake_weather(start_day, end_day=None, **values):
    """DataFrame con la stessa forma di SP.weather.get_daily_weather, senza chiamare Open-Meteo."""
    days = pd.date_range(start_day, end_day or start_day).date
    weather = {**DEFAULT_WEATHER, **values}
    df = pd.DataFrame({column: [weather[column]] * len(days) for column in WEATHER_COLUMNS})
    df.insert(0, "Date", days)
    return df


class FakeModel:
    """Modello di previsione finto con resa costante (kWh per kW installato al giorno)."""

    def __init__(self, specific_yield=5.0):
        self.specific_yield = specific_yield

    def predict(self, features):
        if list(features.columns) != FEATURE_COLUMNS:
            raise ValueError("The feature names should match those that were passed during fit")
        return np.full(len(features), self.specific_yield)


def reference_dataset(locations=8, days=60):
    """Piccolo dataset di riferimento: la resa cresce con la radiazione, come nei dati PVGIS."""
    random = np.random.RandomState(0)
    rows = []
    for location in range(locations):
        for day in pd.date_range(date(2023, 3, 1), periods=days).date:
            weather = {**DEFAULT_WEATHER, "solar_radiation": random.uniform(3, 28)}
            rows.append({
                "location": f"Località {location}", "latitude": 38 + location, "longitude": 12.0, "Date": day,
                **weather, "specific_yield": weather["solar_radiation"] / 3.6 * 0.8,
            })
    return pd.DataFrame(rows)


def past_year_yield(specific_yield=4.0, days=365):
    """Risultato finto di forecast.predictor.estimate_past_year_yield."""
    last_day = date.today() - timedelta(days=1)
    return pd.DataFrame({
        "Date": pd.date_range(end=last_day, periods=days).date,
        "specific_yield": [specific_yield] * days,
    })
