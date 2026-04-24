import random
from django.core.management.base import BaseCommand
from SP.models import Community, User, PhotovoltaicSystem, PanelData
from django.utils import timezone
from datetime import timedelta

class Command(BaseCommand):
    help = 'Populates the database with fictitious data'

    def handle(self, *args, **kwargs):
        self.stdout.write('Deleting old data...')
        PanelData.objects.all().delete()
        User.objects.all().delete()
        PhotovoltaicSystem.objects.all().delete()
        Community.objects.all().delete()

        self.stdout.write('Creating new data...')

        communities = []
        for i in range(10):
            community = Community.objects.create(
                name=f'Community {i}',
                latitude=random.uniform(45.0, 46.0),
                longitude=random.uniform(7.0, 8.0)
            )
            communities.append(community)

        for i in range(15):
            User.objects.create(
                name=f'User {i}',
                surname=f'Surname {i}',
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

        start_date = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)

        panel_data_list = []
        for system in photovoltaic_systems:
            for day in range(3):
                for minute in range(24 * 60):
                    timestamp = start_date + timedelta(days=day, minutes=minute)
                    panel_data_list.append(
                        PanelData(
                            system=system,
                            time_stamp=timestamp,
                            temperature=random.uniform(15.0, 35.0),
                            lightness=random.uniform(100.0, 1000.0),
                            power=random.uniform(0.0, system.max_power)
                        )
                    )
        
        PanelData.objects.bulk_create(panel_data_list)

        self.stdout.write(self.style.SUCCESS('Successfully populated the database.'))
