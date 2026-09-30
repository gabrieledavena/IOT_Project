import argparse
import math
import random
import time
from collections import defaultdict
from datetime import timedelta
from itertools import repeat

import numpy as np
import pandas as pd
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone
from scipy.signal import lfilter
from scipy.stats import norm

from forecast.predictor import ForecastError, load_model, predict_specific_yield
from SP.models import City, Community, Customer, PanelData, PhotovoltaicSystem
from SP.weather import WeatherUnavailable, get_daily_weather

CUSTOMER_PASSWORD = "password123"
# Staff account (same password) to try the ROI calculator, reserved to staff
STAFF_USERNAME = "consulente"

# The Open-Meteo forecast API, used for the weather of the recent days, goes back about 3 months
MAX_DAYS = 90

# Used when the database has no city with coordinates (run import_cities to get the real ones)
FALLBACK_CITIES = [
    {"name": "Torino", "province": "Torino", "region": "Piemonte", "lat": 45.0703, "lon": 7.6868},
    {"name": "Milano", "province": "Milano", "region": "Lombardia", "lat": 45.4642, "lon": 9.1900},
    {"name": "Padova", "province": "Padova", "region": "Veneto", "lat": 45.4064, "lon": 11.8768},
    {"name": "Modena", "province": "Modena", "region": "Emilia-Romagna", "lat": 44.6471, "lon": 10.9252},
    {"name": "Firenze", "province": "Firenze", "region": "Toscana", "lat": 43.7696, "lon": 11.2558},
    {"name": "Roma", "province": "Roma", "region": "Lazio", "lat": 41.9028, "lon": 12.4964},
    {"name": "Pescara", "province": "Pescara", "region": "Abruzzo", "lat": 42.4618, "lon": 14.2161},
    {"name": "Napoli", "province": "Napoli", "region": "Campania", "lat": 40.8518, "lon": 14.2681},
    {"name": "Bari", "province": "Bari", "region": "Puglia", "lat": 41.1171, "lon": 16.8719},
    {"name": "Cosenza", "province": "Cosenza", "region": "Calabria", "lat": 39.2983, "lon": 16.2537},
    {"name": "Palermo", "province": "Palermo", "region": "Sicilia", "lat": 38.1157, "lon": 13.3615},
    {"name": "Cagliari", "province": "Cagliari", "region": "Sardegna", "lat": 39.2238, "lon": 9.1217},
]

FIRST_NAMES = [
    "Marco", "Giulia", "Luca", "Francesca", "Alessandro", "Chiara", "Andrea", "Sara", "Matteo", "Elena",
    "Lorenzo", "Martina", "Davide", "Valentina", "Simone", "Alessia", "Federico", "Silvia", "Giuseppe", "Anna",
]
SURNAMES = [
    "Rossi", "Russo", "Ferrari", "Esposito", "Bianchi", "Romano", "Colombo", "Ricci", "Marino", "Greco",
    "Bruno", "Gallo", "Conti", "De Luca", "Mancini", "Costa", "Giordano", "Rizzo", "Lombardi", "Moretti",
]
PANEL_BRANDS = ["SunPower", "REC", "Jinko Solar", "Trina Solar", "LONGi", "Canadian Solar", "Q CELLS", "Futura Sun"]

# The first system of each customer is the home; the others are further buildings of the same customer,
# each with its typical number of modules (400-430 Wp, about 1.95 m² each)
HOME = ("Casa", (8, 16))
OTHER_BUILDINGS = [("Seconda casa", (6, 12)), ("Garage", (4, 8)), ("Negozio", (10, 20)), ("Capannone", (24, 48))]
MODULE_POWERS_KW = (0.400, 0.410, 0.425, 0.430)
MODULE_AREA_M2 = 1.95

# Production of each system compared to the forecast model, which assumes an optimally oriented system:
# orientation and shading lower it a little, and a few systems have dirty panels
ORIENTATION_RANGE = (0.85, 1.0)
DIRTY_SHARE = 0.1
DIRTY_RANGE = (0.75, 0.85)
# Without the forecast model: share of the solar radiation turned into energy by a system
PERFORMANCE_RATIO = 0.8

# Clouds pass in about half an hour; a thick cloud lets through 30% of the light
CLOUD_AUTOCORRELATION = 0.97
CLOUD_EDGE = 0.15
CLOUD_TRANSMITTANCE = 0.3
# Weather used when Open-Meteo is not available: clear-sky radiation reduced by the average clouds
FALLBACK_CLEARNESS = 0.75
FALLBACK_WEATHER = {"cloud_cover": 30.0, "temp_min": 12.0, "temp_max": 22.0}

# Power loss per °C of the cells above 25 °C (same coefficient as the sensor node); in full sun the cells
# are about 25 °C warmer than the air
TEMPERATURE_COEFFICIENT = -0.004
CELL_HEATING_PER_WM2 = 25 / 800
# The air is coldest at sunrise and warmest 2.5 hours after solar noon
WARMEST_HOUR = 2.5
LUX_PER_WM2 = 110


def solar_geometry(times, latitude, longitude):
    """Position of the sun at the given UTC times (NOAA formulas).

    Returns the cosine of the zenith angle, the hours from solar noon (-12..12) and the day length in hours.
    """
    hours = (times.hour + times.minute / 60).to_numpy()
    gamma = 2 * np.pi / 365 * (times.dayofyear.to_numpy() - 1 + (hours - 12) / 24)
    equation_of_time = 229.18 * (
        0.000075 + 0.001868 * np.cos(gamma) - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma) - 0.040849 * np.sin(2 * gamma)
    )
    declination = (
        0.006918 - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma) - 0.006758 * np.cos(2 * gamma)
        + 0.000907 * np.sin(2 * gamma) - 0.002697 * np.cos(3 * gamma) + 0.00148 * np.sin(3 * gamma)
    )
    solar_time = hours + (equation_of_time + 4 * longitude) / 60
    hour_angle = np.radians(15 * (solar_time - 12))
    lat = np.radians(latitude)
    cos_zenith = np.sin(lat) * np.sin(declination) + np.cos(lat) * np.cos(declination) * np.cos(hour_angle)
    day_length = 2 / 15 * np.degrees(np.arccos(np.clip(-np.tan(lat) * np.tan(declination), -1, 1)))
    return cos_zenith, (solar_time % 24) - 12, day_length


def clear_sky_irradiance(cos_zenith):
    """Solar irradiance on the ground (W/m²) with a clear sky, Haurwitz model."""
    sun_up = cos_zenith > 0
    safe = np.where(sun_up, cos_zenith, 1)
    return np.where(sun_up, 1098 * safe * np.exp(-0.057 / safe), 0.0)


def cloud_transmittance(cloud_cover, rng):
    """Share of sunlight passing through the clouds, minute by minute.

    The clouds are a smooth random process that covers the sun for the share of time given by
    cloud_cover (%, one value per minute): clear and overcast days are regular, the others alternate.
    """
    rho = CLOUD_AUTOCORRELATION
    process = lfilter([math.sqrt(1 - rho ** 2)], [1, -rho], rng.standard_normal(len(cloud_cover)))
    threshold = norm.ppf(np.clip(cloud_cover / 100, 0.001, 0.999))
    covered = 1 / (1 + np.exp((process - threshold) / CLOUD_EDGE))
    return 1 - (1 - CLOUD_TRANSMITTANCE) * covered


def air_temperature(hours_from_noon, day_length, temp_min, temp_max):
    """Air temperature: minimum at sunrise, maximum WARMEST_HOUR hours after solar noon."""
    sunrise = -day_length / 2
    rising = (hours_from_noon >= sunrise) & (hours_from_noon <= WARMEST_HOUR)
    warming = 0.5 - 0.5 * np.cos(np.pi * (hours_from_noon - sunrise) / (WARMEST_HOUR - sunrise))
    cooling = 0.5 + 0.5 * np.cos(np.pi * ((hours_from_noon - WARMEST_HOUR) % 24) / (24 - WARMEST_HOUR + sunrise))
    return temp_min + (temp_max - temp_min) * np.where(rising, warming, cooling)


def system_performance(rng):
    """Production of a system compared to an optimally oriented one with clean panels."""
    performance = rng.uniform(*ORIENTATION_RANGE)
    if rng.random() < DIRTY_SHARE:
        performance *= rng.uniform(*DIRTY_RANGE)
    return performance


def number_range(text):
    """Command line range: "2-3" -> (2, 3), "4" -> (4, 4)."""
    low, _, high = text.partition('-')
    try:
        low, high = int(low), int(high or low)
    except ValueError:
        raise argparse.ArgumentTypeError(f'"{text}" is not a number or a range like 2-3')
    if not 1 <= low <= high:
        raise argparse.ArgumentTypeError(f'"{text}": the numbers must be at least 1, the first not above the second')
    return low, high


class Command(BaseCommand):
    help = (
        "Replaces communities, customers, systems and readings with realistic demo data: communities in "
        "several cities of every Italian region, customers with one or more systems and one reading per "
        "minute that follows the real weather of each city."
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7, help=f'Days of readings to generate, up to today (max {MAX_DAYS})')
        parser.add_argument(
            '--cities_per_region', type=number_range, default=(2, 3),
            help='Cities with a community in each region, a number or a range like 2-3 (default)',
        )
        parser.add_argument(
            '--customers_per_community', type=number_range, default=(2, 4), help='Customers in each community (default 2-4)'
        )
        parser.add_argument(
            '--systems_per_customer', type=number_range, default=(1, 3),
            help=f'Systems owned by each customer (default 1-3, at most {1 + len(OTHER_BUILDINGS)})',
        )
        parser.add_argument('--seed', type=int, help='Seed of the random choices, to generate the same data again')

    def handle(self, *args, **options):
        if not 1 <= options['days'] <= MAX_DAYS:
            raise CommandError(f'--days must be between 1 and {MAX_DAYS}.')
        if options['systems_per_customer'][1] > 1 + len(OTHER_BUILDINGS):
            raise CommandError(f'--systems_per_customer can be at most {1 + len(OTHER_BUILDINGS)}.')
        started = time.monotonic()
        self.random = random.Random(options['seed'])
        self.rng = np.random.default_rng(options['seed'])

        with transaction.atomic():
            self.stdout.write('Deleting old data...')
            self.delete_old_data()

            communities = [
                Community.objects.create(name=f'CER {city}', city=city)
                for city in self.pick_cities(options['cities_per_region'])
            ]
            customers = self.create_customers(communities, options['customers_per_community'])
            systems = self.create_systems(customers, options['systems_per_customer'])
            User.objects.create_user(
                username=STAFF_USERNAME, password=CUSTOMER_PASSWORD, first_name='Consulente', last_name='Demo',
                is_staff=True,
            )
            self.stdout.write(f'Generating the readings of {len(systems)} systems...')
            readings = self.create_panel_data(systems, options['days'])

        self.stdout.write(self.style.SUCCESS(
            f'Created {len(communities)} communities, {len(customers)} customers (user0 ... user{len(customers) - 1}), '
            f'{len(systems)} systems and {readings} readings in {time.monotonic() - started:.0f} s.'
        ))

    def delete_old_data(self):
        PanelData.objects.all().delete()
        Customer.objects.all().delete()
        # Keep the bridge user, otherwise its token would change at every run
        User.objects.filter(is_superuser=False).exclude(username=settings.BRIDGE_USERNAME).delete()
        PhotovoltaicSystem.objects.all().delete()
        Community.objects.all().delete()

    def pick_cities(self, cities_per_region):
        """Random cities with coordinates: in each region as many as cities_per_region (min, max) says."""
        by_region = defaultdict(list)
        cities = City.objects.filter(latitude__isnull=False, longitude__isnull=False).order_by('id')
        for city_id, region in cities.values_list('id', 'region'):
            by_region[region].append(city_id)
        if not by_region:
            self.stdout.write(self.style.WARNING('No cities with coordinates found in the database: using a few regional capitals.'))
            return [
                City.objects.update_or_create(name=city['name'], province=city['province'], defaults={
                    'region': city['region'], 'latitude': city['lat'], 'longitude': city['lon'],
                })[0]
                for city in FALLBACK_CITIES
            ]

        picked = []
        for region in sorted(by_region, key=str):
            # Regions with few cities in the database give all they have
            picked += self.random.sample(by_region[region], min(self.random.randint(*cities_per_region), len(by_region[region])))
        return list(City.objects.filter(id__in=picked))

    def create_customers(self, communities, customers_per_community):
        """Customers of each community, who log in as user0, user1, ..."""
        # All customers have the same password: hashing it once saves almost a second per customer
        password = make_password(CUSTOMER_PASSWORD)
        customers = []
        for community in communities:
            for _ in range(self.random.randint(*customers_per_community)):
                first_name, surname = self.random.choice(FIRST_NAMES), self.random.choice(SURNAMES)
                user = User.objects.create(
                    username=f'user{len(customers)}', password=password, first_name=first_name, last_name=surname
                )
                customers.append(Customer.objects.create(user=user, name=first_name, surname=surname, community=community))
        return customers

    def create_systems(self, customers, systems_per_customer):
        """Systems of each customer: the home and, for some customers, other buildings in the same city."""
        systems = []
        for customer in customers:
            count = self.random.randint(*systems_per_customer)
            for kind, modules_range in [HOME] + self.random.sample(OTHER_BUILDINGS, count - 1):
                modules = self.random.randint(*modules_range)
                systems.append(PhotovoltaicSystem.objects.create(
                    name=f'{kind} {customer.surname} ({customer.name})',
                    max_power=round(modules * self.random.choice(MODULE_POWERS_KW), 2),
                    area=round(modules * MODULE_AREA_M2, 1),
                    brand=self.random.choice(PANEL_BRANDS),
                    inclination=self.random.randint(10, 35),
                    selling_rate_per_kwh=round(self.random.uniform(0.08, 0.12), 3),
                    buying_rate_per_kwh=round(self.random.uniform(0.22, 0.32), 3),
                    community=customer.community,
                    owner=customer,
                ))
        return systems

    def create_panel_data(self, systems, number_of_days):
        """One reading per minute from midnight (UTC) number_of_days - 1 days ago until now.

        The production of every day is the one the forecast model predicts with the real weather of
        the city; during the day it follows the height of the sun, the passing clouds (the same for all
        the systems of a city) and the temperature of the cells. Returns the number of readings.
        """
        now = timezone.now().replace(second=0, microsecond=0)
        start = now.replace(hour=0, minute=0) - timedelta(days=number_of_days - 1)
        # Whole days, so that today's production is spread over the whole day; readings stop at now
        times = pd.date_range(start, start + timedelta(days=number_of_days), freq='min', inclusive='left')
        cities = {system.community.city for system in systems}
        weather = self.city_weather(cities, times)

        # The same minutes for every system, converted once to the database format
        stamps = [connection.ops.adapt_datetimefield_value(t) for t in times[times < now].to_pydatetime()]
        # Millions of rows: one INSERT per system with executemany, without creating a model instance per
        # reading as bulk_create does (several times slower)
        meta = PanelData._meta
        columns = [meta.get_field(name).column for name in ('system', 'time_stamp', 'power', 'lightness', 'temperature')]
        quote = connection.ops.quote_name
        sql = (
            f'INSERT INTO {quote(meta.db_table)} ({", ".join(map(quote, columns))}) '
            f'VALUES ({", ".join(["%s"] * len(columns))})'
        )
        with connection.cursor() as cursor:
            for system in systems:
                power, lightness, temperature = self.system_readings(system, weather[system.community.city_id], len(stamps))
                cursor.executemany(
                    sql, zip(repeat(system.id), stamps, power.tolist(), lightness.tolist(), temperature.tolist())
                )
        return len(stamps) * len(systems)

    def system_readings(self, system, profile, count):
        """First `count` minutes of power (kW), light (lux) and air temperature (°C) of a system."""
        irradiance = profile['irradiance']
        cell_temperature = profile['temperature'] + CELL_HEATING_PER_WM2 * irradiance
        shape = irradiance * (1 + TEMPERATURE_COEFFICIENT * (cell_temperature - 25))
        shape *= 1 + self.rng.normal(0, 0.01, len(shape))  # measurement noise

        # Every day the system produces the energy predicted for its city, scaled by its performance
        energy_by_day = np.bincount(profile['day'], weights=shape) / 60
        target = profile['daily_yield'] * system.max_power * system_performance(self.rng)
        with np.errstate(divide='ignore', invalid='ignore'):
            scale = np.where(energy_by_day > 0, target / energy_by_day, 0)
        power = np.clip(shape * scale[profile['day']], 0, system.max_power)

        lightness = LUX_PER_WM2 * irradiance * (1 + self.rng.normal(0, 0.02, len(irradiance)))
        lightness = np.where(irradiance > 0, lightness, self.rng.uniform(0, 3, len(irradiance)))
        temperature = profile['temperature'] + self.rng.normal(0, 0.15, len(irradiance))
        return np.round(power[:count], 4), np.round(np.clip(lightness[:count], 0, None), 1), np.round(temperature[:count], 2)

    def city_weather(self, cities, times):
        """{city_id: minute-by-minute sunlight and air temperature, daily yield} for each city."""
        days = pd.DatetimeIndex(times.date).unique()
        # One request at a time: Open-Meteo refuses parallel requests (429 Too Many Requests)
        fetched = {city: self.fetch_weather(city, days) for city in cities}

        model = load_model()
        if model is None:
            self.stdout.write(self.style.WARNING('No forecast model: daily production estimated from the solar radiation.'))

        profiles = {}
        for city, real_weather in fetched.items():
            cos_zenith, hours_from_noon, day_length = solar_geometry(times, city.latitude, city.longitude)
            clear_sky = clear_sky_irradiance(cos_zenith)
            day = np.searchsorted(days, times.normalize().tz_localize(None))
            weather = real_weather if real_weather is not None else self.fallback_weather(days, clear_sky, day)

            daily_yield = None
            if model is not None and real_weather is not None:
                try:
                    daily_yield = predict_specific_yield(model, weather, city.latitude)
                except ForecastError as error:
                    self.stdout.write(self.style.WARNING(f'{error} Daily production estimated from the solar radiation.'))
                    model = None
            if daily_yield is None:
                daily_yield = weather['solar_radiation'].to_numpy() / 3.6 * PERFORMANCE_RATIO

            # Daily values change gradually from one day to the next, centred at noon
            noons = (days + timedelta(hours=12)).asi8
            minutes = times.tz_localize(None).asi8
            smooth = {column: np.interp(minutes, noons, weather[column].to_numpy()) for column in FALLBACK_WEATHER}
            profiles[city.id] = {
                'irradiance': clear_sky * cloud_transmittance(smooth['cloud_cover'], self.rng),
                'temperature': air_temperature(hours_from_noon, day_length, smooth['temp_min'], smooth['temp_max']),
                'daily_yield': np.asarray(daily_yield),
                'day': day,
            }
        return profiles

    def fetch_weather(self, city, days):
        """Real weather of the city, one row per day, or None if Open-Meteo is not available."""
        try:
            weather = get_daily_weather(city, days[0].date(), days[-1].date())
        except WeatherUnavailable as error:
            self.stdout.write(self.style.WARNING(f'{city}: {error} Using an average weather.'))
            return None
        # One row for each day, in order, even if Open-Meteo returned fewer
        weather = weather.set_index(pd.DatetimeIndex(weather['Date'])).reindex(days).ffill().bfill()
        weather['Date'] = days.date
        return weather.reset_index(drop=True)

    @staticmethod
    def fallback_weather(days, clear_sky, day):
        """Average weather: clear-sky radiation reduced by the usual clouds, mild temperatures."""
        radiation = np.bincount(day, weights=clear_sky, minlength=len(days)) * 60 / 1e6  # MJ/m²
        return pd.DataFrame({'solar_radiation': radiation * FALLBACK_CLEARNESS, **FALLBACK_WEATHER}, index=range(len(days)))
