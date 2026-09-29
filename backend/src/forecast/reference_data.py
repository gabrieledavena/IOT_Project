"""Dataset di riferimento per il modello di previsione: meteo reale e produzione fotovoltaica.

Per 24 località distribuite in tutta Italia e per ogni giorno di REFERENCE_YEARS contiene:
- il meteo del giorno da Open-Meteo, le stesse variabili che il modello riceve quando è in uso;
- la produzione di un impianto da 1 kWp calcolata da PVGIS (Commissione Europea, JRC) con
  l'irraggiamento misurato dai satelliti: impianto orientato in modo ottimale, perdite del 14%.

I dati di produzione delle community non bastano per addestrare il modello: coprono pochi giorni
e poche città, mentre la stima annua del ROI richiede tutte le stagioni e tutta Italia.
"""
import time

import pandas as pd
import requests
from django.conf import settings

from SP.weather import WeatherUnavailable, fetch_daily_weather

DATASET_PATH = settings.BASE_DIR / "forecast" / "data" / "reference_dataset.csv.gz"

REFERENCE_YEARS = (2021, 2023)  # PVGIS (SARAH3) arriva fino al 2023
PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc"
PVGIS_SYSTEM_LOSS_PERCENT = 14
PVGIS_TIMEOUT_SECONDS = 120

# Capoluoghi e città dal nord alle isole, per coprire latitudini e climi diversi
REFERENCE_LOCATIONS = [
    ("Aosta", 45.737, 7.320), ("Torino", 45.070, 7.687), ("Milano", 45.464, 9.190), ("Bolzano", 46.498, 11.354),
    ("Trento", 46.067, 11.121), ("Venezia", 45.440, 12.316), ("Trieste", 45.650, 13.777), ("Genova", 44.405, 8.946),
    ("Bologna", 44.494, 11.343), ("Firenze", 43.770, 11.256), ("Ancona", 43.616, 13.519), ("Perugia", 43.112, 12.389),
    ("Roma", 41.903, 12.496), ("L'Aquila", 42.350, 13.399), ("Campobasso", 41.561, 14.668), ("Napoli", 40.852, 14.268),
    ("Bari", 41.117, 16.872), ("Potenza", 40.640, 15.806), ("Catanzaro", 38.905, 16.594), ("Palermo", 38.116, 13.361),
    ("Catania", 37.502, 15.087), ("Cagliari", 39.224, 9.122), ("Sassari", 40.727, 8.560), ("Lecce", 40.352, 18.172),
]

# Open-Meteo limita le richieste al minuto: un archivio di più anni ne vale diverse
OPEN_METEO_RETRIES = 5
OPEN_METEO_RETRY_WAIT_SECONDS = 65


def load_reference_dataset():
    """Dataset salvato: colonne location, latitude, longitude, Date, WEATHER_COLUMNS e specific_yield."""
    dataset = pd.read_csv(DATASET_PATH, parse_dates=["Date"])
    dataset["Date"] = dataset["Date"].dt.date
    return dataset


def download_reference_dataset(log=print):
    """Scarica di nuovo il dataset da PVGIS e Open-Meteo (qualche minuto) e lo salva in DATASET_PATH."""
    frames = []
    for name, latitude, longitude in REFERENCE_LOCATIONS:
        log(f"Scarico produzione PVGIS e meteo Open-Meteo per {name}...")
        production = pvgis_daily_yield(latitude, longitude)
        weather = historical_weather(latitude, longitude, log)
        frame = weather.merge(production, on="Date")
        frame.insert(0, "location", name)
        frame.insert(1, "latitude", latitude)
        frame.insert(2, "longitude", longitude)
        frames.append(frame)

    dataset = pd.concat(frames, ignore_index=True)
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_csv(DATASET_PATH, index=False)
    return dataset


def pvgis_daily_yield(latitude, longitude):
    """Produzione giornaliera (kWh) di un impianto da 1 kWp calcolata da PVGIS, colonne Date e specific_yield."""
    first_year, last_year = REFERENCE_YEARS
    response = requests.get(PVGIS_URL, params={
        "lat": latitude, "lon": longitude, "startyear": first_year, "endyear": last_year,
        "pvcalculation": 1, "peakpower": 1, "loss": PVGIS_SYSTEM_LOSS_PERCENT, "optimalangles": 1,
        "outputformat": "json",
    }, timeout=PVGIS_TIMEOUT_SECONDS)
    response.raise_for_status()
    hourly = pd.DataFrame(response.json()["outputs"]["hourly"])
    # Orari in UTC: la produzione avviene di giorno, quindi il giorno UTC coincide con quello locale
    hourly["Date"] = pd.to_datetime(hourly["time"], format="%Y%m%d:%H%M").dt.date
    # P è la potenza media dell'ora in W: la somma sulle ore del giorno, divisa per 1000, dà i kWh
    return (hourly.groupby("Date")["P"].sum() / 1000).rename("specific_yield").reset_index()


def historical_weather(latitude, longitude, log=print):
    first_year, last_year = REFERENCE_YEARS
    for attempt in range(1, OPEN_METEO_RETRIES + 1):
        try:
            return fetch_daily_weather(
                latitude, longitude, f"{first_year}-01-01", f"{last_year}-12-31", historical=True
            )
        except WeatherUnavailable:
            if attempt == OPEN_METEO_RETRIES:
                raise
            log(f"Open-Meteo non risponde (probabile limite di richieste): nuovo tentativo tra {OPEN_METEO_RETRY_WAIT_SECONDS} s")
            time.sleep(OPEN_METEO_RETRY_WAIT_SECONDS)
