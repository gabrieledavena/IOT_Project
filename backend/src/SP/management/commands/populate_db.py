import random
import math
from django.core.management.base import BaseCommand
from SP.models import Community, Customer, PhotovoltaicSystem, PanelData
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta

class Command(BaseCommand):
    help = 'Populates the database with fictitious data'

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

        # start_date = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
        start_date = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)

        panel_data_list = []
        for system in photovoltaic_systems:
            for day in range(6):
                for minute in range(24 * 60):
                    timestamp = start_date + timedelta(days=day, minutes=minute)

                    hour = timestamp.hour + timestamp.minute / 60.0
                    # Simulate solar production: active between 6:00 and 20:00
                    if 6 <= hour <= 20:
                        # Bell curve (Gaussian) centered at 13:00 (1:00 PM)
                        mu = 13.0
                        sigma = 2.5
                        power_factor = math.exp(-((hour - mu) ** 2) / (2 * sigma ** 2))
                        # Add a little noise to make it realistic
                        noise = random.uniform(0.85, 1.0)
                        power = system.max_power * power_factor * noise
                        lightness = 100.0 + power_factor * 900.0 * noise
                    else:
                        power = 0.0
                        lightness = random.uniform(0.0, 20.0)

                    if timezone.now() <= timestamp:
                        break

                    panel_data_list.append(
                        PanelData(
                            system=system,
                            time_stamp=timestamp,
                            temperature=random.uniform(15.0, 35.0),
                            lightness=lightness,
                            power=power
                        )
                    )

        PanelData.objects.bulk_create(panel_data_list)

        self.stdout.write(self.style.SUCCESS('Successfully populated the database.'))
