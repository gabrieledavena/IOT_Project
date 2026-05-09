import pandas as pd
import requests
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score
import joblib
from datetime import date, timedelta
import os
from django.core.management.base import BaseCommand
from SP.models import Community, PhotovoltaicSystem, PanelData
from django.db.models import Sum, F, FloatField
from django.db.models.functions import TruncDate, Cast

class Command(BaseCommand):
    help = 'Train the random forest model using data from the database (PanelData) for all communities'

    def handle(self, *args, **kwargs):
        self.stdout.write("--- 1. ADDESTRAMENTO DEL MODELLO DA DATABASE ---")

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

            response = requests.get(url, params=params)
            if response.status_code != 200:
                raise Exception(f"Errore API Meteo: {response.text}")

            data = response.json()['daily']

            df = pd.DataFrame({
                'Date': pd.to_datetime(data['time']).dt.date if hasattr(pd.to_datetime(data['time']), 'dt') else pd.to_datetime(data['time']).date,
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

        communities = Community.objects.all()
        if not communities.exists():
            self.stdout.write(self.style.ERROR("Nessuna Community trovata nel database."))
            return

        all_data = []

        for community in communities:
            self.stdout.write(f"Elaborazione dati per Community: {community.name}")
            
            # Recupera la produzione giornaliera per ogni impianto della community
            systems = PhotovoltaicSystem.objects.filter(community=community)
            daily_production = []
            
            for system in systems:
                panel_data = PanelData.objects.filter(system=system).order_by('time_stamp')
                if not panel_data.exists():
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
                                'timestamp': minute_timestamp,
                                'value': round(value_per_minute, 4)
                            })
                
                daily_sums = {}
                for item in raw_data:
                    day = item['timestamp'].date()
                    if day not in daily_sums:
                        daily_sums[day] = 0
                    daily_sums[day] += item['value']
                
                for day, val in daily_sums.items():
                    daily_production.append({
                        'Date': day,
                        'system__id': system.id,
                        'max_power': system.max_power,
                        'area': system.area,
                        'Daily Production (Active)': val / 60
                    })
            
            if not daily_production:
                self.stdout.write(self.style.WARNING(f"Nessun dato di produzione per la Community {community.name}"))
                continue

            df_community = pd.DataFrame(daily_production)
            
            # Gestisci valori nulli per max_power e area
            df_community['max_power'] = df_community['max_power'].fillna(0)
            df_community['area'] = df_community['area'].fillna(0)

            # Ensure 'Date' is datetime.date type
            df_community['Date'] = pd.to_datetime(df_community['Date']).dt.date if hasattr(pd.to_datetime(df_community['Date']), 'dt') else pd.to_datetime(df_community['Date']).apply(lambda x: x.date())
            
            start_hist = df_community['Date'].min().strftime('%Y-%m-%d')
            end_hist = df_community['Date'].max().strftime('%Y-%m-%d')
            
            self.stdout.write(f"Recupero dati meteo per {community.name} ({start_hist} - {end_hist})...")
            df_weather_hist = get_weather_data(community.latitude, community.longitude, start_hist, end_hist)
            df_weather_hist['Date'] = pd.to_datetime(df_weather_hist['Date']).dt.date if hasattr(pd.to_datetime(df_weather_hist['Date']), 'dt') else pd.to_datetime(df_weather_hist['Date']).apply(lambda x: x.date())

            print(f' df_community: {df_community.columns} \n df_weather_hist: {df_weather_hist.columns}')
            # Merge dati di produzione con meteo
            df_merged = pd.merge(df_community, df_weather_hist, on='Date', how='inner')
            all_data.append(df_merged)

        if not all_data:
            self.stdout.write(self.style.ERROR("Nessun dato utile trovato per l'addestramento."))
            return

        # Concatena i dati di tutte le community
        df_train = pd.concat(all_data, ignore_index=True)

        self.stdout.write(f"Dati di addestramento pronti, {len(df_train)} record totali.")

        if len(df_train) < 2:
            self.stdout.write(self.style.WARNING("Troppi pochi dati per l'addestramento. Necessari almeno 2 record."))
            return

        # Aggiungiamo max_power e area alle feature
        feature_cols = [
            'max_power', 'area',
            'solar_radiation', 'temp_max', 'temp_min', 'precipitation',
            'wind_speed', 'cloud_cover', 'daylight_duration', 'snowfall'
        ]
        X_train = df_train[feature_cols]
        y_train = df_train['Daily Production (Active)']
        print(X_train.columns)
        print(y_train.head())

        # 4. Allena Random Forest
        self.stdout.write("Addestramento del modello Random Forest...")
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(X_train, y_train)

        # Mostriamo quali variabili il modello ha trovato più utili
        self.stdout.write("\n--- IMPORTANZA VARIABILI (Top 5) ---")
        importances = pd.Series(model.feature_importances_, index=feature_cols)
        self.stdout.write(str(importances.sort_values(ascending=False).head(5)))

        # 5. Esempio di previsione (opzionale, per la prima community e un impianto medio)
        self.stdout.write("\n--- 2. ESEMPIO DI PREVISIONE PER DOMANI ---")
        first_community = communities.first()
        if first_community:
            today = date.today()
            tomorrow = today + timedelta(days=1)
            tomorrow_str = tomorrow.strftime('%Y-%m-%d')

            self.stdout.write(f"Scarico previsioni per domani ({tomorrow_str}) in {first_community.name}...")
            df_forecast = get_weather_data(first_community.latitude, first_community.longitude, tomorrow_str, tomorrow_str, is_forecast=True)

            # Usiamo valori medi di max_power e area del training set come esempio
            avg_max_power = df_train['max_power'].mean()
            avg_area = df_train['area'].mean()
            
            df_forecast['max_power'] = avg_max_power
            df_forecast['area'] = avg_area

            X_tomorrow = df_forecast[feature_cols]
            prediction = model.predict(X_tomorrow)[0]

            meteo = X_tomorrow.iloc[0]
            self.stdout.write("-" * 40)
            self.stdout.write(f"DATA: {tomorrow_str}")
            self.stdout.write(f"Impianto Esempio (Potenza Max: {avg_max_power:.2f}kW, Area: {avg_area:.2f}m²)")
            self.stdout.write(f"Scenario Meteo in {first_community.name}:")
            self.stdout.write(f"  ☀️ Radiazione: {meteo['solar_radiation']:.2f} MJ/m²")
            self.stdout.write(f"  ☁️ Nuvolosità: {meteo['cloud_cover']:.1f}%")
            self.stdout.write(f"  💨 Vento Max: {meteo['wind_speed']:.1f} km/h")
            self.stdout.write(f"  💧 Pioggia: {meteo['precipitation']:.2f} mm")
            self.stdout.write(f"  🌡️ Temp Max/Min: {meteo['temp_min']:.1f}°C / {meteo['temp_max']:.1f}°C")
            self.stdout.write(f"  ⏳ Durata Luce: {meteo['daylight_duration'] / 3600:.1f} ore")
            if meteo['snowfall'] > 0:
                self.stdout.write(f"  ❄️ Neve: {meteo['snowfall']:.2f} cm")

            self.stdout.write("-" * 40)
            self.stdout.write(f"⚡ PRODUZIONE STIMATA: {prediction:.2f} kWh")
            self.stdout.write("-" * 40)

        # Salvataggio del modello
        model_dir = os.path.join('forecast', 'ml_models')
        os.makedirs(model_dir, exist_ok=True)
        model_path = os.path.join(model_dir, 'modello_previsione_produzione_generale.joblib')
        
        joblib.dump(model, model_path)
        self.stdout.write(self.style.SUCCESS(f"Modello salvato con successo in {model_path}"))