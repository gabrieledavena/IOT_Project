import requests
import pandas as pd
from datetime import date, timedelta, datetime
from django.shortcuts import render
from django.apps import apps  # Per recuperare il modello caricato all'avvio
from django.contrib.auth.decorators import login_required
from SP.models import Customer, PanelData, PhotovoltaicSystem
import sklearn
import joblib
import os
from darts import TimeSeries
from darts.utils.timeseries_generation import datetime_attribute_timeseries

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

@login_required
def view_test_previsione_rf(request):
    context = {}

    if request.method == "POST":
        try:
            # 1. Carica il modello RandomForest
            model_path = os.path.join('forecast', 'ml_models', 'modello_previsione_tide.joblib')
            model = joblib.load(model_path)

            # 2. Ottieni la community dell'utente
            customer = Customer.objects.get(user=request.user)
            community = customer.community
            systems = PhotovoltaicSystem.objects.filter(community=community)

            if not systems.exists():
                context["error"] = "Nessun impianto fotovoltaico per questa community."
                return render(request, "test_previsione_rf.html", context)

            # 3. Recupera gli ultimi 2 giorni di dati per creare la serie storica di input
            end_date = datetime.now()
            start_date = end_date - timedelta(days=2)
            
            all_systems_data = []
            for system in systems:
                panel_data = PanelData.objects.filter(system=system, time_stamp__range=(start_date, end_date)).order_by('time_stamp')
                
                records = [{'Timestamp': pd.to_datetime(p.time_stamp), 'Power': p.power} for p in panel_data]
                
                raw_data = []
                for i in range(1, len(records)):
                    curr_row, prev_row = records[i], records[i-1]
                    delta_prod = curr_row['Power'] - prev_row['Power']
                    if delta_prod < 0: delta_prod = curr_row['Power']
                    
                    time_diff_minutes = int(round((curr_row['Timestamp'] - prev_row['Timestamp']).total_seconds() / 60))
                    if time_diff_minutes <= 0: continue
                    
                    value_per_minute = delta_prod / time_diff_minutes
                    for m in range(1, time_diff_minutes + 1):
                        minute_timestamp = prev_row['Timestamp'] + timedelta(minutes=m)
                        raw_data.append({'timestamp': minute_timestamp.replace(second=0, microsecond=0), 'production': round(value_per_minute, 4)})
                
                if raw_data:
                    df_system = pd.DataFrame(raw_data).set_index('timestamp')
                    all_systems_data.append(df_system)

            if not all_systems_data:
                context["error"] = "Dati di produzione insufficienti negli ultimi 2 giorni."
                return render(request, "test_previsione_rf.html", context)

            # Somma la produzione di tutti gli impianti per ora
            df_total = pd.concat(all_systems_data).groupby('timestamp').sum()
            df_resampled = df_total.resample('1h').sum().fillna(0).reset_index()
            
            # Assicurati che ci siano abbastanza dati per la previsione
            if len(df_resampled) < 24:
                context["error"] = "Non ci sono abbastanza dati storici (richieste almeno 24 ore)."
                return render(request, "test_previsione_rf.html", context)

            # 4. Crea la TimeSeries e le covariate per Darts
            ts = TimeSeries.from_dataframe(df_resampled, time_col='timestamp', value_cols='production')
            
            # Crea covariate per il passato e il futuro per coprire l'orizzonte di previsione
            # Il modello RandomForest con past_covariates richiede che le covariate coprano
            # l'intera serie temporale target + i passi futuri da prevedere (n=24)
            covariates_time_index = pd.date_range(start=ts.start_time(), periods=len(ts) + 24, freq=ts.freq)
            
            past_covariates = datetime_attribute_timeseries(covariates_time_index, attribute='hour', one_hot=False)
            past_covariates = past_covariates.stack(datetime_attribute_timeseries(covariates_time_index, attribute='day', one_hot=False))
            past_covariates = past_covariates.stack(datetime_attribute_timeseries(covariates_time_index, attribute='month', one_hot=False))

            # 5. Fai la previsione per le prossime 24 ore
            # Il RandomForest model in Darts non supporta future_covariates, solo past_covariates.
            prediction = model.predict(n=24, series=ts, past_covariates=past_covariates)

            # 6. Prepara il contesto per il template
            context["success"] = True
            context["prediction"] = {item[0].strftime('%Y-%m-%d %H:%M'): round(item[1][0], 2) for item in zip(prediction.time_index, prediction.values())}
            context["community_name"] = community.name
            
        except FileNotFoundError:
            context["error"] = "Modello non trovato. Eseguire prima il training."
        except Exception as e:
            context["error"] = f"Errore durante la predizione: {str(e)}"

    return render(request, "test_previsione_rf.html", context)
