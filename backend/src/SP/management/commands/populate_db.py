import random
import math
import requests
import pandas as pd
from django.core.management.base import BaseCommand
from SP.models import Community, Customer, PhotovoltaicSystem, PanelData
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta

class Command(BaseCommand):
    help = 'Populates the database with fictitious data consistent with real weather'

    def handle(self, *args, **kwargs):
        self.stdout.write('Deleting old data...')
        PanelData.objects.all().delete()
        Customer.objects.all().delete()
        User.objects.filter(is_superuser=False).delete()
        PhotovoltaicSystem.objects.all().delete()
        Community.objects.all().delete()

        self.stdout.write('Creating new data...')

        citta_coords = [
            {'name': 'Milano', 'lat': 45.4642, 'lon': 9.1900},
            {'name': 'Modena', 'lat': 44.6471, 'lon': 10.9252},
            {'name': 'Torino', 'lat': 45.0703, 'lon': 7.6868},
            {'name': 'Napoli', 'lat': 40.8518, 'lon': 14.2681},
            {'name': 'Bari', 'lat': 41.1171, 'lon': 16.8719}
        ]

        communities = []
        for i in range(10):
            citta_scelta = random.choice(citta_coords)
            community = Community.objects.create(
                name=f"Community {i} ({citta_scelta['name']})",
                latitude=citta_scelta['lat'] + random.uniform(-0.02, 0.02), # Add small variation
                longitude=citta_scelta['lon'] + random.uniform(-0.02, 0.02)
            )
            communities.append(community)

        for i in range(15):
            # Create a standard Django user
            user = User.objects.create_user(
                username=f'user{i}',
                password='password123', # A default password
                first_name=f'User {i}',
                last_name=f'Surname {i}'
            )
            # Create the corresponding Customer profile
            Customer.objects.create(
                user=user,
                name=user.first_name,
                surname=user.last_name,
                community=random.choice(communities)
            )

        photovoltaic_systems = []
        for i in range(10):
            photovoltaic_system = PhotovoltaicSystem.objects.create(
                name=f'System {i}',
                max_power=random.uniform(3.0, 6.0),
                area=random.uniform(20.0, 40.0),
                brand=f'Brand {i}',
                inclination=random.randint(15, 45),
                selling_rate_per_kwh=random.uniform(0.10, 0.15),
                buying_rate_per_kwh=random.uniform(0.20, 0.25),
                community=communities[i]
            )
            photovoltaic_systems.append(photovoltaic_system)

        start_date = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=5)
        end_date = timezone.now()

        # Funzione per recuperare il meteo orario vero
        def get_hourly_weather(latitude, longitude, start_date, end_date):
            url = "https://archive-api.open-meteo.com/v1/archive"
            params = {
                "latitude": latitude,
                "longitude": longitude,
                "start_date": start_date.strftime('%Y-%m-%d'),
                "end_date": end_date.strftime('%Y-%m-%d'),
                "hourly": "temperature_2m,cloud_cover,shortwave_radiation",
                "timezone": "auto"
            }
            try:
                response = requests.get(url, params=params)
                if response.status_code == 200:
                    data = response.json()['hourly']
                    df = pd.DataFrame({
                        'time': pd.to_datetime(data['time']),
                        'temperature': data['temperature_2m'],
                        'cloud_cover': data['cloud_cover'],
                        'radiation': data['shortwave_radiation']
                    })
                    df['time'] = df['time'].dt.tz_localize(None)
                    df.set_index('time', inplace=True)
                    return df
            except Exception as e:
                print(f"Errore recupero meteo: {e}")
            return None

        self.stdout.write('Scaricando dati meteo storici per generare la produzione...')
        
        # Dizionario per memorizzare il meteo per ogni community per non rifare chiamate API inutili
        weather_cache = {}
        for community in communities:
            if community.name not in weather_cache:
                weather_cache[community.name] = get_hourly_weather(community.latitude, community.longitude, start_date, end_date)

        panel_data_list = []
        for system in photovoltaic_systems:
            weather_df = weather_cache.get(system.community.name)
            
            # Se il meteo fallisce, usiamo un fallback simile al tuo script originale
            use_fallback = weather_df is None or weather_df.empty
            
            current_time = start_date
            while current_time < end_date:
                # Estraiamo ora esatta per fare lookup
                current_hour_key = current_time.replace(minute=0, second=0, microsecond=0).replace(tzinfo=None)
                
                # Se abbiamo dati meteo veri per questa ora, li usiamo, altrimenti fallback
                if not use_fallback and current_hour_key in weather_df.index:
                    row = weather_df.loc[current_hour_key]
                    temp_reale = row['temperature']
                    cloud_cover = row['cloud_cover']
                    radiation = row['radiation']
                    
                    # Logica per convertire la radiazione solare in potenza prodotta
                    # P = Area * Rendimento * Radiazione_Solare * (Fattore Inclinazione)
                    # Rendimento approssimativo 15% (0.15)
                    # Fattore Nuvole: le nuvole riducono l'efficienza
                    cloud_factor = 1.0 - (cloud_cover / 100.0) * 0.7 # max 70% di riduzione
                    
                    # Radiation è in W/m^2. Convertiamo in kW
                    max_theoretical_power = (system.area * 0.15 * radiation) / 1000.0 
                    power = max_theoretical_power * cloud_factor
                    
                    # Limitiamo la potenza al max_power dell'impianto
                    if power > system.max_power:
                        power = system.max_power
                    
                    # Aggiungiamo rumore naturale minuto per minuto
                    noise = random.uniform(0.90, 1.10)
                    power = power * noise if power > 0 else 0.0
                    
                    # Lightness artificiale coerente (0-1000)
                    lightness = radiation * noise
                    if lightness < 0: lightness = 0
                    if lightness > 1000: lightness = 1000
                    
                    # Temperatura artificiale con rumore
                    temperature = temp_reale + random.uniform(-0.5, 0.5)

                else:
                    # Fallback originale se le API meteo falliscono
                    hour = current_time.hour + current_time.minute / 60.0
                    if 6 <= hour <= 20:
                        mu = 13.0
                        sigma = 2.5
                        power_factor = math.exp(-((hour - mu) ** 2) / (2 * sigma ** 2))
                        noise = random.uniform(0.85, 1.0)
                        power = system.max_power * power_factor * noise
                        lightness = 100.0 + power_factor * 900.0 * noise
                        temperature = random.uniform(20.0, 30.0)
                    else:
                        power = 0.0
                        lightness = random.uniform(0.0, 20.0)
                        temperature = random.uniform(10.0, 15.0)

                # Converti power a cumulativo come se fosse un contatore (se necessario per Darts o mantienilo istantaneo a seconda di come usi il DB)
                # Il tuo codice originale calcolava "valore al minuto" durante il read, ma il DB salva potenza istantanea o cumulativa? 
                # Assumiamo istantanea per coerenza col tuo vecchio script.
                
                panel_data_list.append(
                    PanelData(
                        system=system,
                        time_stamp=current_time,
                        temperature=temperature,
                        lightness=lightness,
                        power=power
                    )
                )

                current_time += timedelta(minutes=1)

            # Esegui un bulk create ogni impianto per non sovraccaricare la RAM
            PanelData.objects.bulk_create(panel_data_list)
            panel_data_list = []

        self.stdout.write(self.style.SUCCESS('Successfully populated the database with weather-consistent data.'))