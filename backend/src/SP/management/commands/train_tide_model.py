import pandas as pd
from datetime import timedelta
import os
import joblib
import warnings
from django.core.management.base import BaseCommand
from SP.models import PhotovoltaicSystem, PanelData
import requests
import numpy as np

# Suppress warnings for cleaner output
warnings.filterwarnings("ignore")

try:
    from darts import TimeSeries
    from darts.models import TiDEModel
    from darts.utils.timeseries_generation import datetime_attribute_timeseries
    from darts.dataprocessing.transformers import Scaler
except ImportError:
    print("Errore: la libreria 'darts' non è installata. Assicurati di averla installata ('pip install darts').")

class Command(BaseCommand):
    help = 'Train a TiDE model using data from the database (PanelData) to predict real time production considering the time'

    def handle(self, *args, **kwargs):
        self.stdout.write("--- 1. ADDESTRAMENTO DEL MODELLO TiDE DA DATABASE ---")
        self.stdout.write("Recupero dati di produzione degli impianti dal database...")
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

            response = requests.get(url, params=params)
            if response.status_code != 200:
                self.stdout.write(self.style.WARNING(f"Errore API Meteo: {response.text}"))
                return pd.DataFrame()

            data = response.json().get('daily')
            if not data:
                 return pd.DataFrame()

            df = pd.DataFrame({
                'Date': pd.to_datetime(data['time']).dt.date if hasattr(pd.to_datetime(data['time']),
                                                                        'dt') else pd.to_datetime(data['time']).date,
                'solar_radiation': data['shortwave_radiation_sum'],
                'temp_max': data['temperature_2m_max'],
                'temp_min': data['temperature_2m_min'],
                'precipitation': data['precipitation_sum'],
                'wind_speed': data['wind_speed_10m_max'],
                'cloud_cover': data['cloud_cover_mean'],
                'daylight_duration': data['daylight_duration'],
                'snowfall': data['snowfall_sum']
            })
            return df.fillna(0)

        # 1. Recupero dati dal Database (PanelData) per tutte le community
        self.stdout.write("Recupero dati di produzione e caratteristiche degli impianti dal database...")

        systems = PhotovoltaicSystem.objects.all()
        if not systems.exists():
            self.stdout.write(self.style.ERROR("Nessun Impianto trovato nel database."))
            return

        all_time_series = []
        all_past_covariates = []
        all_future_covariates = []

        for system in systems:
            self.stdout.write(f"Elaborazione dati per Impianto: {system.name}")
            
            panel_data = PanelData.objects.filter(system=system).order_by('time_stamp')
            if not panel_data.exists():
                self.stdout.write(self.style.WARNING(f"Nessun dato per Impianto: {system.name}"))
                continue
            
            records = [
                {'Timestamp': pd.to_datetime(p.time_stamp), 'Power': p.power,'Lightness': p.lightness, 'Temperature': p.temperature}
                for p in panel_data
            ]
            
            raw_data = []
            for i in range(1, len(records)):
                curr_row = records[i]
                prev_row = records[i - 1]

                # Rimosso il filtro sulla Lightness: 
                # Il modello ha bisogno di vedere i dati continui, inclusi la notte e le temperature notturne
                # per capire matematicamente quando la produzione è zero in modo organico.

                curr_time = curr_row['Timestamp']
                prev_time = prev_row['Timestamp']
                curr_val = curr_row['Power']
                prev_val = prev_row['Power']
                lightness = prev_row['Lightness'] + curr_row['Lightness']
                temperature = prev_row['Temperature'] + curr_row['Temperature']

                delta_prod = curr_val - prev_val

                # Gestione reset del contatore (es. nuovo giorno)
                if delta_prod < 0:
                    delta_prod = curr_val

                time_diff_minutes = int(round((curr_time - prev_time).total_seconds() / 60))

                if time_diff_minutes <= 0:
                    continue

                value_per_minute = delta_prod / time_diff_minutes

                for m in range(1, time_diff_minutes + 1):
                    minute_timestamp = prev_time + timedelta(minutes=m)
                    if pd.notnull(minute_timestamp):
                        raw_data.append({
                            'timestamp': minute_timestamp.replace(second=0, microsecond=0),
                            'production': round(value_per_minute, 4),
                            'lightness' : lightness,
                            'temperature' : temperature,
                        })

            if not raw_data:
                continue

            df_system = pd.DataFrame(raw_data)
            
            # Rimozione della timezone per facilitare il lavoro di resample e Darts
            df_system['timestamp'] = df_system['timestamp'].dt.tz_localize(None)

            # Resample ad intervalli regolari di 1 ora per formare una serie temporale continua
            df_system.set_index('timestamp', inplace=True)
            df_resampled = df_system.resample('1h').mean().fillna(method='ffill').fillna(0).reset_index()

            # Recupero dei dati meteo
            start_date = df_resampled['timestamp'].min().date()
            end_date = df_resampled['timestamp'].max().date()
            weather_df = get_weather_data(system.community.latitude, system.community.longitude, start_date, end_date)
            
            if not weather_df.empty:
                weather_df['Date'] = pd.to_datetime(weather_df['Date'])
                df_resampled['Date'] = df_resampled['timestamp'].dt.date
                df_resampled['Date'] = pd.to_datetime(df_resampled['Date'])
                df_resampled = pd.merge(df_resampled, weather_df, on='Date', how='left').fillna(0)
                df_resampled.drop(columns=['Date'], inplace=True)
            else:
                 self.stdout.write(self.style.WARNING(f"Nessun dato meteo per Impianto: {system.name}, non lo posso addestrare al meglio"))
                 continue

            # Ci assicuriamo di avere almeno 48 ore di dati (24 input + 24 output per l'allenamento)
            if len(df_resampled) > 48: 
                ts_target = TimeSeries.from_dataframe(df_resampled, time_col='timestamp', value_cols=['production'])
                
                weather_cols = ['solar_radiation', 'temp_max', 'temp_min', 'precipitation', 'wind_speed', 'cloud_cover', 'daylight_duration', 'snowfall']
                
                # Le covariate passate includono lightness, temperature (dai panel data) e dati meteo
                ts_past_cov = TimeSeries.from_dataframe(df_resampled, time_col='timestamp', value_cols=['lightness', 'temperature'] + weather_cols)
                
                # Le covariate future includono i dati meteo
                ts_future_cov = TimeSeries.from_dataframe(df_resampled, time_col='timestamp', 
                                                          value_cols=weather_cols)
                
                all_time_series.append(ts_target)
                
                # Creazione delle covariate basate sul tempo per fornire informazioni contestuali al modello (ora, giorno, mese)
                # Cyclical Encoding: usiamo seno e coseno invece dei numeri interi
                hour_ts = datetime_attribute_timeseries(ts_target, attribute='hour')
                hour_sin = hour_ts.map(lambda x: np.sin(2 * np.pi * x / 24.0))
                hour_cos = hour_ts.map(lambda x: np.cos(2 * np.pi * x / 24.0))
                
                day_ts = datetime_attribute_timeseries(ts_target, attribute='day')
                day_sin = day_ts.map(lambda x: np.sin(2 * np.pi * x / 31.0))
                day_cos = day_ts.map(lambda x: np.cos(2 * np.pi * x / 31.0))
                
                month_ts = datetime_attribute_timeseries(ts_target, attribute='month')
                month_sin = month_ts.map(lambda x: np.sin(2 * np.pi * x / 12.0))
                month_cos = month_ts.map(lambda x: np.cos(2 * np.pi * x / 12.0))

                time_covariates = hour_sin.stack(hour_cos).stack(day_sin).stack(day_cos).stack(month_sin).stack(month_cos)
                
                # Combiniamo le time_covariates con le past_covariates e future_covariates
                past_covariates = ts_past_cov.stack(time_covariates)
                all_past_covariates.append(past_covariates)

                future_covariates = ts_future_cov.stack(time_covariates)
                all_future_covariates.append(future_covariates)

        if not all_time_series:
            self.stdout.write(self.style.ERROR("Non ci sono serie temporali abbastanza lunghe (minimo 48h) per l'addestramento."))
            return

        self.stdout.write(f"Trovate {len(all_time_series)} serie storiche valide. Scalatura in corso...")
        
        # TiDE richiede spesso che i dati siano scalati per convergere bene
        scaler_target = Scaler()
        scaler_past_cov = Scaler()
        scaler_future_cov = Scaler()
        
        all_time_series_scaled = scaler_target.fit_transform(all_time_series)
        all_past_covariates_scaled = scaler_past_cov.fit_transform(all_past_covariates)
        all_future_covariates_scaled = scaler_future_cov.fit_transform(all_future_covariates)

        self.stdout.write("Inizializzazione modello TiDE...")

        # Configuriamo il modello TiDE: 
        # input_chunk_length = ore passate osservate (es. le ultime 48 ore)
        # output_chunk_length = ore future da predire (es. prossime 24 ore)
        model = TiDEModel(
            input_chunk_length=48,
            output_chunk_length=24,
            num_encoder_layers=2,
            num_decoder_layers=2,
            decoder_output_dim=16,
            hidden_size=128,
            temporal_width_past=4,
            temporal_width_future=4,
            temporal_decoder_hidden=32,
            use_layer_norm=False,
            dropout=0.1,
            random_state=42,
            optimizer_kwargs={"lr": 1e-3},
            n_epochs=20
        )

        self.stdout.write("Addestramento del modello in corso (potrebbe richiedere qualche minuto)...")
        try:
            # TiDE supporta past_covariates e future_covariates
            model.fit(
                series=all_time_series_scaled, 
                past_covariates=all_past_covariates_scaled,
                future_covariates=all_future_covariates_scaled
            )
            self.stdout.write("Addestramento completato con successo!")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Errore durante l'addestramento: {str(e)}"))
            return

        # Salvataggio del modello nella directory indicata, in formato .joblib (o pth per TiDE)
        model_dir = os.path.join('forecast', 'ml_models')
        os.makedirs(model_dir, exist_ok=True)
        
        # Per darts deep learning models, si salva con save() o si salva un dict con scaler e model
        model_path = os.path.join(model_dir, 'modello_previsione_tide.pt')
        model.save(model_path)
        
        # Salviamo anche gli scaler
        scalers_path = os.path.join(model_dir, 'scalers_tide.joblib')
        joblib.dump({
            'target': scaler_target,
            'past_cov': scaler_past_cov,
            'future_cov': scaler_future_cov
        }, scalers_path)

        self.stdout.write(self.style.SUCCESS(f"Modello salvato con successo in {model_path} e scalers in {scalers_path}"))