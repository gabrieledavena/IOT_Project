"""Modello di previsione della produzione: feature, salvataggio, caricamento e previsioni."""
import os
from datetime import date, timedelta

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from django.core.cache import cache

from SP.weather import WEATHER_COLUMNS, WeatherUnavailable, get_daily_weather

MODEL_PATH = settings.BASE_DIR / "forecast" / "ml_models" / "modello_previsione_produzione_generale.joblib"
TRAIN_COMMAND = "train_forecast_model"

# Oltre al meteo, la posizione del sole: la stessa radiazione rende in modo diverso a seconda
# dell'altezza del sole rispetto ai pannelli inclinati (latitudine e periodo dell'anno)
FEATURE_COLUMNS = WEATHER_COLUMNS + ["latitude", "solar_declination"]

PAST_YEAR_DAYS = 365
PAST_YEAR_CACHE_SECONDS = 24 * 60 * 60

# Open-Meteo prevede il meteo fino a 15 giorni dopo oggi e aggiorna le previsioni ogni ora
FORECAST_DAYS = 16
FORECAST_CACHE_SECONDS = 60 * 60
# Oltre le previsioni meteo, la resa che il modello stima con il meteo reale degli stessi giorni negli anni passati
TYPICAL_YEARS = 5
TYPICAL_WINDOW_DAYS = 3
MONTH_DAYS = 30
WEEK_DAYS = 7

# Da dove viene la previsione di ciascun giorno
SOURCE_WEATHER = "meteo"
SOURCE_HISTORY = "storico"

_loaded = {"mtime": None, "model": None}


class ForecastError(Exception):
    """La previsione non si può calcolare; il messaggio è pensato per l'utente."""


def solar_declination(days):
    """Declinazione solare (gradi) di ogni giorno, con la formula di Cooper."""
    day_of_year = pd.to_datetime(pd.Series(list(days))).dt.dayofyear.to_numpy()
    return 23.44 * np.sin(np.radians(360 / 365 * (day_of_year - 81)))


def build_features(weather, latitude):
    """Feature del modello per ogni giorno di un DataFrame meteo (colonne "Date" e WEATHER_COLUMNS)."""
    features = weather[WEATHER_COLUMNS].copy()
    features["latitude"] = latitude
    features["solar_declination"] = solar_declination(weather["Date"])
    return features[FEATURE_COLUMNS]


def save_model(model):
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH, compress=3)


def load_model():
    """Modello salvato dal comando di training, o None se il file non esiste.

    Il file viene riletto quando cambia, così un nuovo training è usato senza riavviare il server.
    """
    try:
        mtime = os.path.getmtime(MODEL_PATH)
    except OSError:
        return None
    if mtime != _loaded["mtime"]:
        _loaded.update(model=joblib.load(MODEL_PATH), mtime=mtime)
    return _loaded["model"]


def require_model():
    model = load_model()
    if model is None:
        raise ForecastError(f"Il modello di previsione non è disponibile: eseguire il comando {TRAIN_COMMAND}.")
    return model


def predict_specific_yield(model, weather, latitude):
    """Resa prevista (kWh per kW installato) per ogni giorno del DataFrame meteo."""
    try:
        return model.predict(build_features(weather, latitude))
    except ValueError as error:
        # Tipicamente un modello salvato con feature diverse da quelle attuali
        raise ForecastError(
            f"Il modello di previsione non è compatibile con il codice attuale: eseguire di nuovo {TRAIN_COMMAND}."
        ) from error


def cached_weather(cache_key, seconds, city, start_day, end_day, historical=False):
    """Meteo della città (vedi SP.weather.get_daily_weather), tenuto in cache per `seconds` secondi."""
    weather = cache.get(cache_key)
    if weather is None:
        try:
            weather = get_daily_weather(city, start_day, end_day, historical=historical)
        except WeatherUnavailable as error:
            raise ForecastError(str(error)) from error
        cache.set(cache_key, weather, seconds)
    return weather


def day_of_year(day):
    """Giorno dell'anno da 1 a 365, uguale in tutti gli anni: il 29 febbraio conta come 28."""
    return date(2001, day.month, 28 if (day.month, day.day) == (2, 29) else day.day).timetuple().tm_yday


def typical_yield(model, city, days, today):
    """Resa tipica (kWh per kW installato) di ciascuno dei giorni `days`, per i giorni senza previsioni meteo.

    È la resa che il modello stima con il meteo reale degli ultimi TYPICAL_YEARS anni, nei giorni che
    distano al più TYPICAL_WINDOW_DAYS dallo stesso giorno dell'anno (35 giorni con 5 anni): un solo
    giorno per anno darebbe una media che cambia a caso da un giorno all'altro. Restituisce un DataFrame
    con "Date", "specific_yield" (la media) e la resa minima e massima di quei giorni ("yield_low", "yield_high").
    """
    end_day = today - timedelta(days=1)
    start_day = date(today.year - TYPICAL_YEARS, today.month, 1)
    weather = cached_weather(f"typical_weather:{city.latitude}:{city.longitude}:{end_day}", PAST_YEAR_CACHE_SECONDS,
                             city, start_day, end_day, historical=True)
    if weather.empty:
        raise ForecastError(f"Meteo storico non disponibile per {city}.")

    past_days = np.array([day_of_year(day) for day in weather["Date"]])
    past_yield = predict_specific_yield(model, weather, city.latitude)
    rows = []
    for day in days:
        distance = np.abs(past_days - day_of_year(day))
        # A cavallo di capodanno: il 30 dicembre e il 2 gennaio distano 3 giorni
        nearby = past_yield[np.minimum(distance, 365 - distance) <= TYPICAL_WINDOW_DAYS]
        if not len(nearby):
            raise ForecastError(f"Meteo storico non disponibile per {city}.")
        rows.append({"Date": day, "specific_yield": nearby.mean(), "yield_low": nearby.min(), "yield_high": nearby.max()})
    return pd.DataFrame(rows)


def forecast_community_days(community, days=MONTH_DAYS, today=None):
    """Produzione prevista (kWh) della community in ciascuno dei prossimi `days` giorni, oggi compreso.

    Il modello stima la resa per kW installato: moltiplicata per la potenza totale della community dà
    la produzione, qualunque sia la taglia degli impianti. Restituisce un DataFrame con un giorno per
    riga: "Date", "source", "specific_yield" e "production". I primi FORECAST_DAYS giorni usano le
    previsioni meteo (source SOURCE_WEATHER, con le colonne meteo); gli altri la resa tipica degli anni
    passati (SOURCE_HISTORY, con la produzione minima e massima di quegli anni in "production_low" e
    "production_high").
    """
    model = require_model()

    systems = list(community.photovoltaic_systems.all())
    if not systems:
        raise ForecastError("Nessun impianto fotovoltaico registrato per questa community.")

    city, today = community.city, today or date.today()
    # Una sola richiesta per tutti i giorni previsti, da oggi in poi: serve ogni orizzonte della pagina
    weather = cached_weather(f"forecast_weather:{city.latitude}:{city.longitude}:{today}", FORECAST_CACHE_SECONDS,
                             city, today, today + timedelta(days=FORECAST_DAYS - 1))
    forecast = weather.head(days).assign(source=SOURCE_WEATHER, yield_low=np.nan, yield_high=np.nan)
    forecast["specific_yield"] = predict_specific_yield(model, forecast, city.latitude)

    later_days = [today + timedelta(days=offset) for offset in range(len(forecast), days)]
    if later_days:
        typical = typical_yield(model, city, later_days, today).assign(source=SOURCE_HISTORY)
        forecast = pd.concat([forecast, typical], ignore_index=True)

    total_power = sum(system.max_power for system in systems)
    return forecast.assign(
        production=forecast["specific_yield"] * total_power,
        production_low=forecast["yield_low"] * total_power,
        production_high=forecast["yield_high"] * total_power,
    )


def estimate_past_year_yield(city):
    """Resa (kWh per kW installato) che un impianto avrebbe avuto negli ultimi 365 giorni nella città.

    Usa il meteo reale di ogni giorno. Restituisce un DataFrame con le colonne "Date" e
    "specific_yield"; i giorni non ancora presenti nell'archivio meteo mancano.
    """
    model = require_model()
    end_day = date.today() - timedelta(days=1)
    start_day = end_day - timedelta(days=PAST_YEAR_DAYS - 1)

    # Un anno di meteo per città cambia una volta al giorno: evita di richiederlo a ogni simulazione
    weather = cached_weather(f"past_year_weather:{city.latitude}:{city.longitude}:{end_day}", PAST_YEAR_CACHE_SECONDS,
                             city, start_day, end_day, historical=True)
    if weather.empty:
        raise ForecastError(f"Meteo storico non disponibile per {city}.")
    return pd.DataFrame({
        "Date": weather["Date"],
        "specific_yield": predict_specific_yield(model, weather, city.latitude),
    })
