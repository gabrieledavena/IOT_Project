"""Modello di previsione della produzione: salvataggio, caricamento e previsione per una community."""
import os

import joblib
from django.conf import settings

from SP.weather import WEATHER_COLUMNS, WeatherUnavailable, get_daily_weather

MODEL_PATH = settings.BASE_DIR / "forecast" / "ml_models" / "modello_previsione_produzione_generale.joblib"

_loaded = {"mtime": None, "model": None}


class ForecastError(Exception):
    """La previsione non si può calcolare; il messaggio è pensato per l'utente."""


def save_model(model):
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)


def load_model():
    """Modello salvato da train_model_db, o None se il file non esiste.

    Il file viene riletto quando cambia, così un nuovo training è usato senza riavviare il server.
    """
    try:
        mtime = os.path.getmtime(MODEL_PATH)
    except OSError:
        return None
    if mtime != _loaded["mtime"]:
        _loaded.update(model=joblib.load(MODEL_PATH), mtime=mtime)
    return _loaded["model"]


def predict_specific_yield(model, weather):
    """Resa prevista (kWh per kW installato) per ogni giorno del DataFrame meteo."""
    return model.predict(weather[WEATHER_COLUMNS])


def forecast_community_production(community, day):
    """Produzione prevista (kWh) della community nel giorno `day` e meteo usato per calcolarla.

    Il modello stima la resa per kW installato: moltiplicata per la potenza totale della
    community dà la produzione, qualunque sia la taglia degli impianti.
    """
    model = load_model()
    if model is None:
        raise ForecastError("Il modello di previsione non è disponibile: eseguire il comando train_model_db.")

    systems = list(community.photovoltaic_systems.all())
    if not systems:
        raise ForecastError("Nessun impianto fotovoltaico registrato per questa community.")

    try:
        weather = get_daily_weather(community.city, day)
    except WeatherUnavailable as error:
        raise ForecastError(str(error)) from error

    try:
        specific_yield = predict_specific_yield(model, weather)[0]
    except ValueError as error:
        # Tipicamente un modello salvato con feature diverse da quelle attuali
        raise ForecastError(
            "Il modello di previsione non è compatibile con il codice attuale: eseguire di nuovo train_model_db."
        ) from error

    total_power = sum(system.max_power for system in systems)
    return specific_yield * total_power, weather.iloc[0].to_dict()
