import math
import random
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from forecast.predictor import ForecastError, load_model, predict_specific_yield
from SP.models import City, Community, Customer, PanelData, PhotovoltaicSystem
from SP.weather import WeatherUnavailable, get_daily_weather

NUMBER_OF_COMMUNITIES = 10
NUMBER_OF_CUSTOMERS = 15
CUSTOMER_PASSWORD = "password123"
# Staff account (same password) to try the ROI calculator, reserved to staff
STAFF_USERNAME = "consulente"

# Used when the database has no city with coordinates (run import_cities to get the real ones)
FALLBACK_CITIES = [
    {"name": "Milano", "lat": 45.4642, "lon": 9.1900},
    {"name": "Modena", "lat": 44.6471, "lon": 10.9252},
    {"name": "Torino", "lat": 45.0703, "lon": 7.6868},
    {"name": "Napoli", "lat": 40.8518, "lon": 14.2681},
    {"name": "Bari", "lat": 41.1171, "lon": 16.8719},
]


NOISE_RANGE = (0.85, 1.0)


def solar_power_factor(hour):
    """Fraction of the peak power produced at a given hour (0-24): bell curve centered at 13:00."""
    if not 6 <= hour <= 20:
        return 0.0
    mu, sigma = 13.0, 2.5
    return math.exp(-((hour - mu) ** 2) / (2 * sigma ** 2))


# Hours at full power the bell curve is worth in a day, including the average noise
CURVE_HOURS = sum(solar_power_factor(minute / 60) for minute in range(24 * 60)) / 60 * sum(NOISE_RANGE) / 2


class Command(BaseCommand):
    help = 'Populates the database with fictitious data'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=4, help='Number of days of data to generate')
        parser.add_argument(
            '--systems_per_community', type=int, default=2, help='Number of photovoltaic systems per community'
        )

    def handle(self, *args, **options):
        self.stdout.write('Deleting old data...')
        self.delete_old_data()

        self.stdout.write('Creating new data...')
        communities = self.create_communities(self.pick_cities())
        self.create_customers(communities)
        User.objects.create_user(
            username=STAFF_USERNAME, password=CUSTOMER_PASSWORD, first_name='Consulente', last_name='Demo', is_staff=True
        )
        systems = self.create_systems(communities, options['systems_per_community'])
        self.create_panel_data(systems, options['days'])

        self.stdout.write(self.style.SUCCESS('Successfully populated the database.'))

    def delete_old_data(self):
        PanelData.objects.all().delete()
        Customer.objects.all().delete()
        # Keep the bridge user, otherwise its token would change at every run
        User.objects.filter(is_superuser=False).exclude(username=settings.BRIDGE_USERNAME).delete()
        PhotovoltaicSystem.objects.all().delete()
        Community.objects.all().delete()

    def pick_cities(self):
        """Up to 5 random cities with coordinates, or the fallback ones if the database has none."""
        cities = list(City.objects.filter(latitude__isnull=False, longitude__isnull=False).order_by('?')[:5])
        if cities:
            return cities

        self.stdout.write(self.style.WARNING('No cities with coordinates found in the database. Generating dummy cities.'))
        return [
            City.objects.get_or_create(name=city['name'], defaults={'latitude': city['lat'], 'longitude': city['lon']})[0]
            for city in FALLBACK_CITIES
        ]

    def create_communities(self, cities):
        communities = []
        for i in range(NUMBER_OF_COMMUNITIES):
            city = random.choice(cities)
            communities.append(Community.objects.create(name=f"Community {i} ({city.name})", city=city))
        return communities

    def create_customers(self, communities):
        for i in range(NUMBER_OF_CUSTOMERS):
            user = User.objects.create_user(
                username=f'user{i}',
                password=CUSTOMER_PASSWORD,
                first_name=f'User {i}',
                last_name=f'Surname {i}'
            )
            Customer.objects.create(
                user=user, name=user.first_name, surname=user.last_name, community=random.choice(communities)
            )

    def create_systems(self, communities, systems_per_community):
        systems = []
        for community in communities:
            for _ in range(systems_per_community):
                index = len(systems)
                systems.append(PhotovoltaicSystem.objects.create(
                    name=f'System {index}',
                    max_power=random.uniform(3.0, 6.0),
                    area=random.uniform(20.0, 40.0),
                    brand=f'Brand {index}',
                    inclination=random.randint(15, 45),
                    selling_rate_per_kwh=random.uniform(0.10, 0.15),
                    buying_rate_per_kwh=random.uniform(0.20, 0.25),
                    community=community
                ))
        return systems

    def create_panel_data(self, systems, number_of_days):
        """One reading per minute from midnight number_of_days - 1 days ago until now.

        Each day the bell curve is scaled so that the energy produced is the one the forecast model
        predicts with the real weather of that day in the system's city.
        """
        now = timezone.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(0, number_of_days - 1))
        end = min(now, start + timedelta(days=number_of_days))
        yields = self.daily_yields({system.community.city for system in systems}, start.date(), end.date())

        panel_data = []
        for system in systems:
            timestamp = start
            while timestamp < end:
                power_factor = solar_power_factor(timestamp.hour + timestamp.minute / 60.0)
                if power_factor:
                    # A little noise to make it realistic
                    noise = random.uniform(*NOISE_RANGE)
                    daily_yield = yields.get((system.community.city_id, timestamp.date()))
                    peak = system.max_power if daily_yield is None else daily_yield * system.max_power / CURVE_HOURS
                    power = min(peak * power_factor * noise, system.max_power)
                    lightness = 100.0 + power_factor * 900.0 * noise
                else:
                    power = 0.0
                    lightness = random.uniform(0.0, 20.0)
                panel_data.append(PanelData(
                    system=system,
                    time_stamp=timestamp,
                    temperature=random.uniform(15.0, 35.0),
                    lightness=lightness,
                    power=power
                ))
                timestamp += timedelta(minutes=1)

        PanelData.objects.bulk_create(panel_data, batch_size=5000)

    def daily_yields(self, cities, first_day, last_day):
        """{(city_id, day): kWh per installed kW} predicted with the real weather; missing when not available."""
        model = load_model()
        if model is None:
            self.stdout.write(self.style.WARNING('No forecast model: production generated without weather.'))
            return {}

        yields = {}
        for city in cities:
            try:
                weather = get_daily_weather(city, first_day, last_day)
                predicted = predict_specific_yield(model, weather, city.latitude)
            except (WeatherUnavailable, ForecastError) as error:
                self.stdout.write(self.style.WARNING(f'{city}: {error} Production generated without weather.'))
                continue
            yields.update({(city.id, day): value for day, value in zip(weather["Date"], predicted)})
        return yields
