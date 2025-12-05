import requests
import pandas as pd
from datetime import date, timedelta
from django.shortcuts import render
from django.apps import apps  # Per recuperare il modello caricato all'avvio
import sklearn

# Configurazione costanti (le stesse del training)
LATITUDE = 44.77
LONGITUDE = 10.78
METEO_PARAMS = (
    "temperature_2m_max,temperature_2m_min,"
    "precipitation_sum,shortwave_radiation_sum,"
    "wind_speed_10m_max,cloud_cover_mean,daylight_duration,snowfall_sum"
)


def get_meteo_domani():
    """Scarica le previsioni meteo per domani da Open-Meteo"""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "start_date": tomorrow_str,
        "end_date": tomorrow_str,
        "daily": METEO_PARAMS,
        "timezone": "auto",
    }

    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()["daily"]

        # Creiamo un DataFrame con una sola riga
        df = pd.DataFrame(
            {
                "solar_radiation": data["shortwave_radiation_sum"],
                "temp_max": data["temperature_2m_max"],
                "temp_min": data["temperature_2m_min"],
                "precipitation": data["precipitation_sum"],
                "wind_speed": data["wind_speed_10m_max"],
                "cloud_cover": data["cloud_cover_mean"],
                "daylight_duration": data["daylight_duration"],
                "snowfall": data["snowfall_sum"],
            }
        )
        return df.fillna(0)  # Gestione eventuali null
    except Exception as e:
        print(f"Errore meteo: {e}")
        return None


def view_test_previsione(request):
    context = {}

    if request.method == "POST":
        # 1. Recupera il modello dalla RAM (caricato in apps.py)
        # Sostituisci 'mia_app' con il nome reale della tua app in settings.py
        app_config = apps.get_app_config("forecast")
        model = app_config.model

        if not model:
            context["error"] = (
                "Il modello non è stato caricato correttamente all'avvio."
            )
        else:
            # 2. Ottieni i dati meteo per domani
            X_input = get_meteo_domani()

            if X_input is not None:
                try:
                    # 3. Fai la previsione
                    # Assicuriamoci che le colonne siano nell'ordine corretto
                    feature_cols = [
                        "solar_radiation",
                        "temp_max",
                        "temp_min",
                        "precipitation",
                        "wind_speed",
                        "cloud_cover",
                        "daylight_duration",
                        "snowfall",
                    ]
                    X_input = X_input[feature_cols]

                    kwh_predicted = model.predict(X_input)[0]

                    context["success"] = True
                    context["prediction"] = round(kwh_predicted, 2)
                    context["date"] = (date.today() + timedelta(days=1)).strftime(
                        "%d/%m/%Y"
                    )
                    # Passiamo anche i dati meteo per visualizzarli se vuoi
                    context["meteo_data"] = X_input.iloc[0].to_dict()
                except Exception as e:
                    context["error"] = f"Errore durante la predizione: {str(e)}"
            else:
                context["error"] = "Impossibile recuperare i dati meteo da Open-Meteo."

    return render(request, "test_previsione.html", context)
