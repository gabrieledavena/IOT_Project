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


def forecast_community_production(community, day):
    """Produzione prevista (kWh) della community nel giorno `day` e meteo usato per calcolarla.

    Il modello stima la resa per kW installato: moltiplicata per la potenza totale della
    community dà la produzione, qualunque sia la taglia degli impianti.
    """
    model = require_model()

    systems = list(community.photovoltaic_systems.all())
    if not systems:
        raise ForecastError("Nessun impianto fotovoltaico registrato per questa community.")

    try:
        weather = get_daily_weather(community.city, day)
    except WeatherUnavailable as error:
        raise ForecastError(str(error)) from error

    specific_yield = predict_specific_yield(model, weather, community.city.latitude)[0]
    total_power = sum(system.max_power for system in systems)
    return specific_yield * total_power, weather.iloc[0].to_dict()


def estimate_past_year_yield(city):
    """Resa (kWh per kW installato) che un impianto avrebbe avuto negli ultimi 365 giorni nella città.

    Usa il meteo reale di ogni giorno. Restituisce un DataFrame con le colonne "Date" e
    "specific_yield"; i giorni non ancora presenti nell'archivio meteo mancano.
    """
    model = require_model()
    end_day = date.today() - timedelta(days=1)
    start_day = end_day - timedelta(days=PAST_YEAR_DAYS - 1)

    # Un anno di meteo per città cambia una volta al giorno: evita di richiederlo a ogni simulazione
    cache_key = f"past_year_weather:{city.latitude}:{city.longitude}:{end_day}"
    weather = cache.get(cache_key)
    if weather is None:
        try:
            weather = get_daily_weather(city, start_day, end_day, historical=True)
        except WeatherUnavailable as error:
            raise ForecastError(str(error)) from error
        cache.set(cache_key, weather, PAST_YEAR_CACHE_SECONDS)

    if weather.empty:
        raise ForecastError(f"Meteo storico non disponibile per {city}.")
    return pd.DataFrame({
        "Date": weather["Date"],
        "specific_yield": predict_specific_yield(model, weather, city.latitude),
    })
