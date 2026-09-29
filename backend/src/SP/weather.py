"""Meteo giornaliero da Open-Meteo, usato da dashboard, previsioni e training del modello."""
import logging

import pandas as pd
import requests

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 10

# Variabile giornaliera di Open-Meteo -> colonna usata nel progetto
DAILY_VARIABLES = {
    "shortwave_radiation_sum": "solar_radiation",  # MJ/m²
    "temperature_2m_max": "temp_max",  # °C
    "temperature_2m_min": "temp_min",  # °C
    "precipitation_sum": "precipitation",  # mm
    "wind_speed_10m_max": "wind_speed",  # km/h
    "cloud_cover_mean": "cloud_cover",  # %
    "daylight_duration": "daylight_duration",  # secondi
    "snowfall_sum": "snowfall",  # cm
}

# Colonne meteo, nell'ordine in cui il modello di previsione le usa come feature
WEATHER_COLUMNS = list(DAILY_VARIABLES.values())


class WeatherUnavailable(Exception):
    """Meteo non disponibile: città senza coordinate o errore di Open-Meteo."""


def get_daily_weather(city, start_day, end_day=None):
    """Meteo della città, un giorno per riga, da start_day a end_day inclusi.

    Restituisce un DataFrame con la colonna "Date" e le WEATHER_COLUMNS.
    Solleva WeatherUnavailable (con un messaggio da mostrare all'utente) se il meteo non è disponibile.
    """
    if not city.has_coordinates:
        raise WeatherUnavailable(f"La città {city} non ha coordinate: impossibile recuperare il meteo.")

    params = {
        "latitude": city.latitude,
        "longitude": city.longitude,
        "start_date": str(start_day),
        "end_date": str(end_day or start_day),
        "daily": ",".join(DAILY_VARIABLES),
        "timezone": "auto",
    }
    try:
        response = requests.get(OPEN_METEO_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        daily = response.json()["daily"]
    except (requests.RequestException, ValueError, KeyError) as error:
        logger.warning("Meteo Open-Meteo non disponibile per %s: %s", city, error)
        raise WeatherUnavailable("Impossibile recuperare i dati meteo da Open-Meteo.") from error

    # Open-Meteo può restituire valori nulli: diventano 0
    weather = pd.DataFrame({
        column: pd.to_numeric(pd.Series(daily[variable]), errors="coerce")
        for variable, column in DAILY_VARIABLES.items()
    }).fillna(0)
    weather.insert(0, "Date", pd.to_datetime(daily["time"]).date)
    return weather


def get_day_weather(city, day):
    """Meteo di un solo giorno come dizionario, o None se non è disponibile."""
    try:
        return get_daily_weather(city, day).iloc[0].to_dict()
    except WeatherUnavailable:
        return None
