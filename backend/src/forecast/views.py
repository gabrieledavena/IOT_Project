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
import torch
import numpy as np
from darts import TimeSeries
from darts.utils.timeseries_generation import datetime_attribute_timeseries
from darts.models import TiDEModel

# Configurazione costanti (le stesse del training)
METEO_PARAMS = (
    "temperature_2m_max,temperature_2m_min,"
    "precipitation_sum,shortwave_radiation_sum,"
    "wind_speed_10m_max,cloud_cover_mean,daylight_duration,snowfall_sum"
)


def get_weather_data(latitude, longitude, start_date, end_date, is_forecast=False):
    """Scarica dati meteo estesi da Open-Meteo"""
    url = "https://api.open-meteo.com/v1/forecast" if is_forecast else "https://archive-api.open-meteo.com/v1/archive"

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
        data = response.json().get("daily")
        
        if not data:
            return None

        # Creiamo un DataFrame con una sola riga se necessario
        df = pd.DataFrame(
            {
                "Date": pd.to_datetime(data['time']).dt.date if hasattr(pd.to_datetime(data['time']),
                                                                        'dt') else pd.to_datetime(data['time']).date,
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
                    # Rimuoviamo il "Date" prima di passarlo al modello generico
                    if 'Date' in X_input.columns:
                        X_input = X_input.drop(columns=['Date'])
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
                    # Rimuoviamo il "Date" prima di passarlo al modello generico
                    if 'Date' in X_input.columns:
                        X_input = X_input.drop(columns=['Date'])
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
            # 1. Carica il modello TiDE e gli scalers
            model_path = os.path.join('forecast', 'ml_models', 'modello_previsione_tide.pt')
            scalers_path = os.path.join('forecast', 'ml_models', 'scalers_tide.joblib')
            
            # Allowlist torch.optim.adam.Adam for loading PyTorch weights
            try:
                import torch.optim.adam
                torch.serialization.add_safe_globals([torch.optim.adam.Adam])
            except Exception:
                pass
            
            # Allow loading weights only for older models where it causes issues in PyTorch 2.6+
            # Let's override torch.load to always bypass weights_only internally for darts if needed
            original_torch_load = torch.load
            def custom_torch_load(*args, **kwargs):
                if 'weights_only' in kwargs:
                    kwargs['weights_only'] = False
                return original_torch_load(*args, **kwargs)
                
            try:
                torch.load = custom_torch_load
                # Use TiDEModel.load for a PyTorch darts model
                model = TiDEModel.load(model_path)
            finally:
                torch.load = original_torch_load

            scalers = joblib.load(scalers_path)

            scaler_target = scalers['target']
            scaler_past_cov = scalers['past_cov']
            scaler_future_cov = scalers['future_cov']

            # 2. Ottieni la community dell'utente
            customer = Customer.objects.get(user=request.user)
            community = customer.community
            systems = PhotovoltaicSystem.objects.filter(community=community)

            if not systems.exists():
                context["error"] = "Nessun impianto fotovoltaico per questa community."
                return render(request, "test_previsione_rf.html", context)

            # 3. Recupera i dati passati e futuri necessari 
            # Vogliamo le ULTIME 48 ORE per l'input (che ti sei configurato in training)
            # e poi prevedere le 24 ore successive (il giorno corrente).
            today = datetime.now().date()
            start_of_today = datetime.combine(today, datetime.min.time())
            start_date_past = start_of_today - timedelta(days=2) # Ultime 48 ore (2 giorni prima di oggi)
            
            all_systems_data = []
            for system in systems:
                # Recuperiamo dati fino all'inizio di oggi (cioè dalle 00:00 di 2 giorni fa, alle 00:00 di oggi)
                panel_data = PanelData.objects.filter(system=system, time_stamp__range=(start_date_past, start_of_today)).order_by('time_stamp')
                
                records = [{'Timestamp': pd.to_datetime(p.time_stamp), 'Power': p.power, 'Lightness': p.lightness, 'Temperature': p.temperature} for p in panel_data]
                
                raw_data = []
                for i in range(1, len(records)):
                    curr_row, prev_row = records[i], records[i-1]
                    delta_prod = curr_row['Power'] - prev_row['Power']
                    if delta_prod < 0: delta_prod = curr_row['Power']
                    
                    time_diff_minutes = int(round((curr_row['Timestamp'] - prev_row['Timestamp']).total_seconds() / 60))
                    if time_diff_minutes <= 0: continue
                    
                    value_per_minute = delta_prod / time_diff_minutes
                    lightness = curr_row['Lightness'] + prev_row['Lightness']
                    temperature = curr_row['Temperature'] + prev_row['Temperature']

                    for m in range(1, time_diff_minutes + 1):
                        minute_timestamp = prev_row['Timestamp'] + timedelta(minutes=m)
                        raw_data.append({'timestamp': minute_timestamp.replace(second=0, microsecond=0), 'production': round(value_per_minute, 4), 'lightness': lightness, 'temperature': temperature})
                
                if raw_data:
                    df_system = pd.DataFrame(raw_data).set_index('timestamp')
                    all_systems_data.append(df_system)

            if not all_systems_data:
                context["error"] = f"Dati di produzione insufficienti per gli ultimi 2 giorni a partire dal {start_date_past.date()}."
                return render(request, "test_previsione_rf.html", context)

            # Somma la produzione di tutti gli impianti per ora (facciamo media per le condizioni ambientali)
            df_concat = pd.concat(all_systems_data)
            df_total_prod = df_concat[['production']].groupby('timestamp').sum()
            df_total_env = df_concat[['lightness', 'temperature']].groupby('timestamp').mean()
            
            df_total = pd.concat([df_total_prod, df_total_env], axis=1)
            # Remove timezone if any exists to match training data
            if df_total.index.tz is not None:
                df_total.index = df_total.index.tz_localize(None)
            df_resampled = df_total.resample('1h').mean().fillna(0).reset_index()
            
            # Filtra per prendere solo le ultime 48 ore esatte di ieri per l'input
            df_resampled = df_resampled[(df_resampled['timestamp'] >= start_date_past) & (df_resampled['timestamp'] < start_of_today)]

            if len(df_resampled) < 48:
                 context["error"] = "Non ci sono 48 ore di dati storici complete per i 2 giorni precedenti."
                 return render(request, "test_previsione_rf.html", context)

            # Prendi solo gli ultimi 48 per sicurezza (input_chunk_length = 48)
            df_resampled = df_resampled.tail(48)

            # --- Dati Meteo (Passato e Futuro) ---
            # Il passato sono i 2 giorni precedenti, il futuro è oggi
            start_date_meteo = df_resampled['timestamp'].min().date() 
            end_date_meteo = start_of_today.date() # Oggi
            
            # Attenzione, stiamo usando un mix di dati meteo storici (per past) e forecast (per future)
            weather_df_past = get_weather_data(community.latitude, community.longitude, start_date_meteo, end_date_meteo - timedelta(days=1), is_forecast=False)
            weather_df_future = get_weather_data(community.latitude, community.longitude, end_date_meteo, end_date_meteo, is_forecast=True)
            
            if weather_df_past is None or weather_df_future is None:
                context["error"] = "Impossibile recuperare i dati meteo necessari per la predizione."
                return render(request, "test_previsione_rf.html", context)

            weather_df = pd.concat([weather_df_past, weather_df_future]).drop_duplicates(subset=['Date'])
            weather_df['Date'] = pd.to_datetime(weather_df['Date'])
            
            # Assicurati che date sia compatibile, senza fusi orari
            df_resampled['Date'] = pd.to_datetime(df_resampled['timestamp'].dt.date)
            df_resampled = pd.merge(df_resampled, weather_df, on='Date', how='left').fillna(0)
            df_resampled.drop(columns=['Date'], inplace=True)

            # 4. Crea la TimeSeries e le covariate per Darts
            ts_target = TimeSeries.from_dataframe(df_resampled, time_col='timestamp', value_cols=['production'])
            
            weather_cols = ['solar_radiation', 'temp_max', 'temp_min', 'precipitation', 'wind_speed', 'cloud_cover', 'daylight_duration', 'snowfall']
            
            ts_past_cov = TimeSeries.from_dataframe(df_resampled, time_col='timestamp', value_cols=['lightness', 'temperature'] + weather_cols)
            
            # Per le covariate future, creiamo il dataframe per OGGI (dalle 00:00 alle 23:00)
            future_timestamps = pd.date_range(start=start_of_today, periods=24, freq='1h')
            future_df = pd.DataFrame({'timestamp': future_timestamps})
            future_df['Date'] = pd.to_datetime(future_df['timestamp'].dt.date)
            future_df = pd.merge(future_df, weather_df, on='Date', how='left').fillna(0)
            future_df.drop(columns=['Date'], inplace=True)

            # L'input model di TiDE si aspetta le future_covariates fornite su tutto lo scope (past_chunk_length + output_chunk_length)
            # Quindi concateniamo df_resampled (ultime 48h) e future_df (oggi 24h) per ottenere un timeframe da 72h totali
            full_time_df = pd.concat([df_resampled[['timestamp'] + weather_cols], future_df[['timestamp'] + weather_cols]])
            ts_future_cov = TimeSeries.from_dataframe(full_time_df, time_col='timestamp', value_cols=weather_cols)

            # Covariate temporali - Cyclical Encoding (Stessa cosa fatta in training)
            hour_ts = datetime_attribute_timeseries(ts_target, attribute='hour')
            hour_sin = hour_ts.map(lambda x: np.sin(2 * np.pi * x / 24.0))
            hour_cos = hour_ts.map(lambda x: np.cos(2 * np.pi * x / 24.0))
            
            day_ts = datetime_attribute_timeseries(ts_target, attribute='day')
            day_sin = day_ts.map(lambda x: np.sin(2 * np.pi * x / 31.0))
            day_cos = day_ts.map(lambda x: np.cos(2 * np.pi * x / 31.0))
            
            month_ts = datetime_attribute_timeseries(ts_target, attribute='month')
            month_sin = month_ts.map(lambda x: np.sin(2 * np.pi * x / 12.0))
            month_cos = month_ts.map(lambda x: np.cos(2 * np.pi * x / 12.0))

            time_covariates_past = hour_sin.stack(hour_cos).stack(day_sin).stack(day_cos).stack(month_sin).stack(month_cos)
            past_covariates = ts_past_cov.stack(time_covariates_past)

            # Esegui lo stesso mapping ciclico per il futuro
            hour_future_ts = datetime_attribute_timeseries(ts_future_cov, attribute='hour')
            hour_future_sin = hour_future_ts.map(lambda x: np.sin(2 * np.pi * x / 24.0))
            hour_future_cos = hour_future_ts.map(lambda x: np.cos(2 * np.pi * x / 24.0))

            day_future_ts = datetime_attribute_timeseries(ts_future_cov, attribute='day')
            day_future_sin = day_future_ts.map(lambda x: np.sin(2 * np.pi * x / 31.0))
            day_future_cos = day_future_ts.map(lambda x: np.cos(2 * np.pi * x / 31.0))

            month_future_ts = datetime_attribute_timeseries(ts_future_cov, attribute='month')
            month_future_sin = month_future_ts.map(lambda x: np.sin(2 * np.pi * x / 12.0))
            month_future_cos = month_future_ts.map(lambda x: np.cos(2 * np.pi * x / 12.0))

            time_covariates_future = hour_future_sin.stack(hour_future_cos).stack(day_future_sin).stack(day_future_cos).stack(month_future_sin).stack(month_future_cos)
            
            future_covariates = ts_future_cov.stack(time_covariates_future)

            # Scale i dati prima di inviarli
            ts_target_scaled = scaler_target.transform(ts_target)
            past_covariates_scaled = scaler_past_cov.transform(past_covariates)
            future_covariates_scaled = scaler_future_cov.transform(future_covariates)

            # 5. Fai la previsione per le 24 ore di OGGI
            prediction_scaled = model.predict(n=24, series=ts_target_scaled, past_covariates=past_covariates_scaled, future_covariates=future_covariates_scaled)
            
            # Inverse scale la predizione
            prediction = scaler_target.inverse_transform(prediction_scaled)

            # Post-processing: evita valori negativi (Clipping)
            pred_values = prediction.values()
            pred_values[pred_values < 0] = 0
            
            # 6. Prepara il contesto per il template
            context["success"] = True
            context["prediction"] = {item[0].strftime('%Y-%m-%d %H:%M'): round(item[1][0], 2) for item in zip(prediction.time_index, pred_values)}
            context["community_name"] = community.name
            
        except FileNotFoundError:
            context["error"] = "Modello non trovato. Eseguire prima il training."
        except Exception as e:
            context["error"] = f"Errore durante la predizione: {str(e)}"

    return render(request, "test_previsione_rf.html", context)