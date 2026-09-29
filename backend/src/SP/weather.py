"""Meteo giornaliero da Open-Meteo, usato da dashboard, previsioni, ROI e training del modello."""
import logging

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Previsioni e giorni recenti (fino a circa 3 mesi fa)
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
# Archivio del meteo reale dei giorni passati, senza limiti di data
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
REQUEST_TIMEOUT_SECONDS = 10
ARCHIVE_TIMEOUT_SECONDS = 30

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


def fetch_daily_weather(latitude, longitude, start_day, end_day=None, historical=False):
    """Meteo del punto indicato, un giorno per riga, da start_day a end_day inclusi.

    Restituisce un DataFrame con la colonna "Date" e le WEATHER_COLUMNS. Con historical=True usa
    l'archivio del meteo reale: serve per i giorni passati oltre i 3 mesi, e i giorni non ancora
    presenti nell'archivio vengono scartati.
    Solleva WeatherUnavailable (con un messaggio da mostrare all'utente) se il meteo non è disponibile.
    """
    url, timeout = (OPEN_METEO_ARCHIVE_URL, ARCHIVE_TIMEOUT_SECONDS) if historical else (OPEN_METEO_URL, REQUEST_TIMEOUT_SECONDS)
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": str(start_day),
        "end_date": str(end_day or start_day),
        "daily": ",".join(DAILY_VARIABLES),
        "timezone": "auto",
    }
    try:
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        daily = response.json()["daily"]
    except (requests.RequestException, ValueError, KeyError) as error:
        logger.warning("Meteo Open-Meteo non disponibile per (%s, %s): %s", latitude, longitude, error)
        raise WeatherUnavailable("Impossibile recuperare i dati meteo da Open-Meteo.") from error

    weather = pd.DataFrame({
        column: pd.to_numeric(pd.Series(daily[variable]), errors="coerce")
        for variable, column in DAILY_VARIABLES.items()
    })
    weather.insert(0, "Date", pd.to_datetime(daily["time"]).date)
    if historical:
        return weather.dropna().reset_index(drop=True)
    # Le previsioni possono avere valori nulli isolati: diventano 0
    return weather.fillna(0)


def get_daily_weather(city, start_day, end_day=None, historical=False):
    """Meteo della città (vedi fetch_daily_weather); richiede che la città abbia le coordinate."""
    if not city.has_coordinates:
        raise WeatherUnavailable(f"La città {city} non ha coordinate: impossibile recuperare il meteo.")
    return fetch_daily_weather(city.latitude, city.longitude, start_day, end_day, historical)


def get_day_weather(city, day):
    """Meteo di un solo giorno come dizionario, o None se non è disponibile."""
    try:
        return get_daily_weather(city, day).iloc[0].to_dict()
    except WeatherUnavailable:
        return None
