import pandas as pd
from datetime import timedelta
import os
import joblib
import warnings
from django.core.management.base import BaseCommand
from SP.models import PhotovoltaicSystem, PanelData

# Suppress warnings for cleaner output
warnings.filterwarnings("ignore")

try:
    from darts import TimeSeries
    from darts.models import RandomForest
    from darts.utils.timeseries_generation import datetime_attribute_timeseries
except ImportError:
    print("Errore: la libreria 'darts' non è installata. Assicurati di averla installata ('pip install darts').")

class Command(BaseCommand):
    help = 'Train a RandomForest model using data from the database (PanelData) to predict real time production considering the time'

    def handle(self, *args, **kwargs):
        self.stdout.write("--- 1. ADDESTRAMENTO DEL MODELLO RandomForest DA DATABASE ---")
        self.stdout.write("Recupero dati di produzione degli impianti dal database...")

        systems = PhotovoltaicSystem.objects.all()
        if not systems.exists():
            self.stdout.write(self.style.ERROR("Nessun Impianto trovato nel database."))
            return

        all_time_series = []
        all_covariates = []

        for system in systems:
            self.stdout.write(f"Elaborazione dati per Impianto: {system.name}")
            
            panel_data = PanelData.objects.filter(system=system).order_by('time_stamp')
            if not panel_data.exists():
                self.stdout.write(self.style.WARNING(f"Nessun dato per Impianto: {system.name}"))
                continue
            
            records = [
                {'Timestamp': pd.to_datetime(p.time_stamp), 'Power': p.power}
                for p in panel_data
            ]
            
            raw_data = []
            for i in range(1, len(records)):
                curr_row = records[i]
                prev_row = records[i - 1]

                curr_time = curr_row['Timestamp']
                prev_time = prev_row['Timestamp']
                curr_val = curr_row['Power']
                prev_val = prev_row['Power']

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
                            'production': round(value_per_minute, 4)
                        })

            if not raw_data:
                continue

            df_system = pd.DataFrame(raw_data)
            
            # Rimozione della timezone per facilitare il lavoro di resample e Darts
            df_system['timestamp'] = df_system['timestamp'].dt.tz_localize(None)

            # Resample ad intervalli regolari di 1 ora per formare una serie temporale continua
            df_system.set_index('timestamp', inplace=True)
            df_resampled = df_system.resample('1h').sum().fillna(0).reset_index()

            # Ci assicuriamo di avere almeno 48 ore di dati (24 input + 24 output per l'allenamento)
            if len(df_resampled) > 48: 
                ts = TimeSeries.from_dataframe(df_resampled, time_col='timestamp', value_cols='production')
                all_time_series.append(ts)
                
                # Creazione delle covariate basate sul tempo per fornire informazioni contestuali al modello (ora, giorno, mese)
                hour_cov = datetime_attribute_timeseries(ts, attribute='hour')
                day_cov = datetime_attribute_timeseries(ts, attribute='day')
                month_cov = datetime_attribute_timeseries(ts, attribute='month')
                covariates = hour_cov.stack(day_cov).stack(month_cov)
                all_covariates.append(covariates)

        if not all_time_series:
            self.stdout.write(self.style.ERROR("Non ci sono serie temporali abbastanza lunghe (minimo 48h) per l'addestramento."))
            return

        self.stdout.write(f"Trovate {len(all_time_series)} serie storiche valide. Inizializzazione modello RandomForest...")

        # Configuriamo il modello RandomForest: 
        # input_chunk_length = ore passate osservate (es. le ultime 24 ore)
        # output_chunk_length = ore future da predire (es. prossime 24 ore)
        model = RandomForest(
            lags=24,
            lags_past_covariates=24,
            n_estimators=100,
            random_state=42
        )

        self.stdout.write("Addestramento del modello in corso (potrebbe richiedere qualche minuto)...")
        try:
            # RandomForest supporta l'allenamento su serie temporali multiple e relative covariate
            model.fit(series=all_time_series, past_covariates=all_covariates)
            self.stdout.write("Addestramento completato con successo!")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Errore durante l'addestramento: {str(e)}"))
            return

        # Salvataggio del modello nella directory indicata, in formato .joblib
        model_dir = os.path.join('forecast', 'ml_models')
        os.makedirs(model_dir, exist_ok=True)
        model_path = os.path.join(model_dir, 'modello_previsione_tide.joblib')
        
        joblib.dump(model, model_path)
        self.stdout.write(self.style.SUCCESS(f"Modello salvato con successo in {model_path}"))
