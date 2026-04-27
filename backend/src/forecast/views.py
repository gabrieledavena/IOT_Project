import requests
import pandas as pd
from datetime import date, timedelta
from django.shortcuts import render
from django.apps import apps  # Per recuperare il modello caricato all'avvio
from django.contrib.auth.decorators import login_required
from SP.models import Customer
import sklearn

# Configurazione costanti (le stesse del training)
METEO_PARAMS = (
    "temperature_2m_max,temperature_2m_min,"
    "precipitation_sum,shortwave_radiation_sum,"
    "wind_speed_10m_max,cloud_cover_mean,daylight_duration,snowfall_sum"
)


def get_weather_data(latitude, longitude, start_date, end_date, is_forecast=False):
    """Scarica dati meteo estesi da Open-Meteo"""
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "daily": METEO_PARAMS,
        "timezone": "auto"
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

@login_required
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
            try:
                # Get the current user's community to fetch the right weather data
                customer = Customer.objects.get(user=request.user)
                community = customer.community
                
                # We also need some max_power and area to make a prediction
                # Let's average the power and area of the photovoltaic systems in this community
                # Or sum them if we want to predict the total production of the community
                photovoltaic_systems = community.photovoltaic_system.all()
                
                if not photovoltaic_systems.exists():
                     context["error"] = "Nessun impianto fotovoltaico registrato per questa community."
                     return render(request, "test_previsione.html", context)

                total_max_power = sum([system.max_power for system in photovoltaic_systems])
                total_area = sum([system.area for system in photovoltaic_systems if system.area is not None])
                
                # If some systems have no area, we might have an issue, let's provide a fallback
                if total_area == 0:
                     total_area = total_max_power * 5 # Rough estimate if area is missing

                # 2. Ottieni i dati meteo per domani basati sulla latitudine e longitudine della community
                today = date.today()
                tomorrow = today + timedelta(days=1)
                tomorrow_str = tomorrow.strftime("%Y-%m-%d")
                X_input = get_weather_data(community.latitude, community.longitude, tomorrow_str, tomorrow_str, is_forecast=True)

                if X_input is not None:
                    # Add max_power and area to X_input to match what the general model expects
                    X_input['max_power'] = total_max_power
                    X_input['area'] = total_area

                    # 3. Fai la previsione
                    # Assicuriamoci che le colonne siano nell'ordine corretto
                    feature_cols = [
                        "max_power",
                        "area",
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
                    context["prediction"] = round(kwh_predicted, 3)
                    context["date"] = (date.today() + timedelta(days=1)).strftime(
                        "%d/%m/%Y"
                    )
                    context["community_name"] = community.name
                    context["total_power"] = total_max_power
                    context["total_area"] = total_area
                    
                    # Passiamo anche i dati meteo per visualizzarli se vuoi
                    context["meteo_data"] = X_input.iloc[0].to_dict()
                else:
                    context["error"] = "Impossibile recuperare i dati meteo da Open-Meteo."
                    
            except Customer.DoesNotExist:
                context["error"] = "Utente non associato a nessuna community."
            except Exception as e:
                context["error"] = f"Errore durante la predizione: {str(e)}"

    return render(request, "test_previsione.html", context)


@login_required
def view_test_previsione_oggi(request):
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
            try:
                # Get the current user's community to fetch the right weather data
                customer = Customer.objects.get(user=request.user)
                community = customer.community

                # We also need some max_power and area to make a prediction
                # Let's average the power and area of the photovoltaic systems in this community
                # Or sum them if we want to predict the total production of the community
                photovoltaic_systems = community.photovoltaic_system.all()

                if not photovoltaic_systems.exists():
                    context["error"] = "Nessun impianto fotovoltaico registrato per questa community."
                    return render(request, "test_previsione.html", context)

                total_max_power = sum([system.max_power for system in photovoltaic_systems])
                total_area = sum([system.area for system in photovoltaic_systems if system.area is not None])

                # If some systems have no area, we might have an issue, let's provide a fallback
                if total_area == 0:
                    total_area = total_max_power * 5  # Rough estimate if area is missing

                # 2. Ottieni i dati meteo per domani basati sulla latitudine e longitudine della community
                today = date.today()
                today_str = today.strftime("%Y-%m-%d")
                X_input = get_weather_data(community.latitude, community.longitude, today_str, today_str,
                                           is_forecast=True)

                if X_input is not None:
                    # Add max_power and area to X_input to match what the general model expects
                    X_input['max_power'] = total_max_power
                    X_input['area'] = total_area

                    # 3. Fai la previsione
                    # Assicuriamoci che le colonne siano nell'ordine corretto
                    feature_cols = [
                        "max_power",
                        "area",
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
                    context["prediction"] = round(kwh_predicted, 3)
                    context["date"] = (date.today() + timedelta(days=1)).strftime(
                        "%d/%m/%Y"
                    )
                    context["community_name"] = community.name
                    context["total_power"] = total_max_power
                    context["total_area"] = total_area

                    # Passiamo anche i dati meteo per visualizzarli se vuoi
                    context["meteo_data"] = X_input.iloc[0].to_dict()
                else:
                    context["error"] = "Impossibile recuperare i dati meteo da Open-Meteo."

            except Customer.DoesNotExist:
                context["error"] = "Utente non associato a nessuna community."
            except Exception as e:
                context["error"] = f"Errore durante la predizione: {str(e)}"

    return render(request, "test_previsione.html", context)
