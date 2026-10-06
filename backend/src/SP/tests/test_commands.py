import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

import pandas as pd
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from SP.management.commands.import_cities import NAME_COLUMN, PROVINCE_COLUMN, REGION_COLUMN
from SP.management.commands.populate_db import FALLBACK_CITIES, PERFORMANCE_RATIO
from SP.models import City, Community, Customer, Device, Intervention, PanelData, PhotovoltaicSystem
from SP.production import daily_energy_kwh, get_system_series
from SP.tests.helpers import DEFAULT_WEATHER, FakeModel, create_community, create_system, fake_weather
from SP.weather import WeatherUnavailable


def run(command, *args):
    out = StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


class DeviceCredentialsCommandTests(TestCase):
    def credentials(self, system):
        lines = run("device_credentials", str(system.id)).splitlines()
        return lines[1].split()[-1], lines[2].split()[-1]

    def test_installs_the_device_and_every_run_gives_a_new_token(self):
        system = create_system(create_community(), "Magazzino")

        username, token = self.credentials(system)
        self.assertEqual(username, f"pv-{system.id}")
        self.assertTrue(system.device.check_token(token))

        _, new_token = self.credentials(system)
        system.device.refresh_from_db()
        self.assertFalse(system.device.check_token(token))
        self.assertTrue(system.device.check_token(new_token))
        self.assertEqual(Device.objects.count(), 1)

    def test_prints_the_commands_to_start_the_bridge(self):
        system = create_system(create_community())

        output = run("device_credentials", str(system.id))

        self.assertIn(f"bridge.py --device pv-{system.id} --token", output)
        self.assertIn("--simulate", output)

    def test_unknown_system_is_a_command_error(self):
        with self.assertRaisesMessage(CommandError, "not found"):
            run("device_credentials", "999")


class PopulateDbCommandTests(TestCase):
    def setUp(self):
        for region in ("Piemonte", "Lazio", "Sicilia"):
            for i in range(3):
                City.objects.create(name=f"{region} {i}", region=region, latitude=42.0, longitude=12.0)

    def populate(self, model, *args, weather=None, performance=1.0):
        weather = weather or (lambda city, first, last: fake_weather(first, last))
        # Rendimento fisso degli impianti e niente polvere, per confrontare la produzione con quella prevista;
        # il controllo finale degli impianti usa lo stesso modello e lo stesso meteo
        with mock.patch("SP.management.commands.populate_db.load_model", return_value=model), \
                mock.patch("forecast.predictor.load_model", return_value=model), \
                mock.patch("SP.management.commands.populate_db.get_daily_weather", side_effect=weather), \
                mock.patch("SP.monitoring.get_daily_weather", side_effect=weather), \
                mock.patch("SP.management.commands.populate_db.system_performance", return_value=performance), \
                mock.patch("SP.management.commands.populate_db.area_soiling", return_value=1.0):
            return run(
                "populate_db", "--days", "2", "--cities_per_region", "1", "--users_per_community", "2",
                "--systems_per_community", "2", "--seed", "1", *args,
            )

    def yesterday_yields(self):
        """kWh prodotti ieri da ogni impianto, per kW installato."""
        yesterday = timezone.now().date() - timedelta(days=1)
        return [
            daily_energy_kwh(get_system_series(system, yesterday))[yesterday] / system.max_power
            for system in PhotovoltaicSystem.objects.all()
        ]

    def test_creates_demo_data(self):
        self.populate(FakeModel(3.0))

        self.assertEqual(Community.objects.count(), 3)
        self.assertEqual({c.customers.count() for c in Community.objects.all()}, {2})
        self.assertEqual(PhotovoltaicSystem.objects.count(), 6)
        self.assertTrue(User.objects.get(username="user5").check_password("password123"))
        self.assertTrue(User.objects.get(username="consulente").is_staff)
        # Tecnici dello staff con lo storico degli interventi già eseguiti (uno per impianto, qui sono 6)
        self.assertTrue(User.objects.get(username="tecnico1").is_staff)
        done = Intervention.objects.filter(status=Intervention.Status.DONE)
        self.assertEqual(done.count(), 6)
        self.assertFalse(done.filter(staff__is_staff=False).exists())

    def test_cities_per_region(self):
        self.populate(FakeModel(3.0), "--cities_per_region", "2")

        regions = list(Community.objects.values_list("city__region", flat=True))
        self.assertEqual(sorted(regions), ["Lazio", "Lazio", "Piemonte", "Piemonte", "Sicilia", "Sicilia"])

    def test_each_company_has_its_owner_other_users_and_several_systems(self):
        self.populate(FakeModel(3.0), "--users_per_community", "3", "--systems_per_community", "4")

        for community in Community.objects.all():
            users = list(community.customers.all())
            self.assertEqual(len(users), 3)
            # Il titolare è uno degli utenti e dà il nome all'azienda
            self.assertIn(community.owner, users)
            self.assertTrue(community.name.startswith(community.owner.surname))
            names = list(community.photovoltaic_systems.values_list("name", flat=True))
            self.assertEqual(len(names), 4)
            self.assertIn("Sede", names)
            self.assertEqual(len(set(names)), 4)

    def test_without_cities_uses_regional_capitals(self):
        City.objects.all().delete()

        output = self.populate(FakeModel(3.0), "--days", "1")

        self.assertIn("No cities with coordinates", output)
        self.assertEqual(Community.objects.count(), len(FALLBACK_CITIES))

    def test_one_reading_per_minute_until_now_following_the_sun(self):
        self.populate(FakeModel(3.0))

        system = PhotovoltaicSystem.objects.first()
        readings = PanelData.objects.filter(system=system).order_by("time_stamp")
        first, last = readings.first().time_stamp, readings.last().time_stamp
        self.assertEqual(readings.count(), (last - first).total_seconds() / 60 + 1)
        self.assertEqual(first.date(), timezone.now().date() - timedelta(days=1))
        self.assertLess(timezone.now() - last, timedelta(minutes=2))
        # Di notte (mezzanotte UTC in Italia) nessuna produzione, a mezzogiorno sì
        midnight, noon = first, first + timedelta(hours=11)
        self.assertEqual(readings.get(time_stamp=midnight).power, 0)
        self.assertGreater(readings.get(time_stamp=noon).power, 0)
        self.assertLessEqual(max(readings.values_list("power", flat=True)), system.max_power)

    def test_systems_are_checked_at_the_end(self):
        output = self.populate(FakeModel(3.0), "--days", "3")

        self.assertIn("Checked 6 systems (6 OK", output)
        self.assertFalse(PhotovoltaicSystem.objects.filter(last_check__isnull=True).exists())

    def test_daily_production_follows_the_model_with_the_real_weather(self):
        self.populate(FakeModel(3.0))

        for specific_yield in self.yesterday_yields():
            self.assertAlmostEqual(specific_yield, 3.0, delta=0.05)

    def test_system_performance_lowers_the_production(self):
        self.populate(FakeModel(3.0), performance=0.8)

        for specific_yield in self.yesterday_yields():
            self.assertAlmostEqual(specific_yield, 2.4, delta=0.05)

    def test_without_model_production_follows_the_solar_radiation(self):
        output = self.populate(None)

        self.assertIn("No forecast model", output)
        self.assertIn("Systems not checked", output)
        expected = DEFAULT_WEATHER["solar_radiation"] / 3.6 * PERFORMANCE_RATIO
        for specific_yield in self.yesterday_yields():
            self.assertAlmostEqual(specific_yield, expected, delta=0.05)

    def test_without_weather_uses_an_average_one(self):
        def unavailable(city, first, last):
            raise WeatherUnavailable("offline")

        output = self.populate(FakeModel(3.0), weather=unavailable)

        self.assertIn("Using an average weather", output)
        # Radiazione a cielo sereno ridotta dalle nuvole medie: plausibile in ogni stagione
        for specific_yield in self.yesterday_yields():
            self.assertTrue(0.5 < specific_yield < 7)

    def test_invalid_options_are_a_command_error(self):
        for option, value in [("--days", "0"), ("--systems_per_community", "3-1"), ("--users_per_community", "0"),
                              ("--cities_per_region", "due")]:
            with self.subTest(option=option, value=value), self.assertRaises(CommandError):
                call_command("populate_db", option, value, stdout=StringIO())


def geonames_place(name, admin1code, latitude, population=10000):
    return {"name": name, "countrycode": "IT", "admin1code": admin1code, "latitude": latitude, "longitude": 10.0,
            "population": population}


class ImportCitiesCommandTests(TestCase):
    MUNICIPALITIES = [
        ("Modena", "Modena", "Emilia-Romagna"),
        ("Bari", "Bari", "Puglia"),
        ("Bergamo", "Bergamo", "Lombardia"),
        ("Castro", "Lecce", "Puglia"),
        ("Castro", "Bergamo", "Lombardia"),
        ("Inesistente", "Roma", "Lazio"),
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "comuni.xlsx"
        pd.DataFrame(self.MUNICIPALITIES, columns=[NAME_COLUMN, PROVINCE_COLUMN, REGION_COLUMN]).to_excel(self.file, index=False)
        self.places = [
            geonames_place("Modena", "05", 44.6),
            geonames_place("Bari", "13", 41.1),
            geonames_place("Bergamo", "09", 45.7),
            # Due comuni omonimi in regioni diverse
            geonames_place("Castro", "13", 40.0),
            geonames_place("Castro", "09", 45.8),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def import_cities(self):
        geonames = mock.Mock()
        geonames.get_cities.return_value = {i: place for i, place in enumerate(self.places)}
        with mock.patch("SP.management.commands.import_cities.geonamescache.GeonamesCache", return_value=geonames):
            return run("import_cities", "--file", str(self.file))

    def test_homonyms_are_told_apart_by_region(self):
        self.import_cities()

        self.assertEqual(City.objects.get(name="Castro", province="Lecce").latitude, 40.0)
        self.assertEqual(City.objects.get(name="Castro", province="Bergamo").latitude, 45.8)
        self.assertFalse(City.objects.get(name="Inesistente").has_coordinates)

    def test_running_again_updates_cities_without_deleting_communities(self):
        self.import_cities()
        cities = City.objects.count()
        community = create_community(city=City.objects.get(name="Modena"))
        self.places[0] = geonames_place("Modena", "05", 44.7)

        output = self.import_cities()

        self.assertIn("0 created, 1 updated", output)
        self.assertEqual(City.objects.count(), cities)
        community.refresh_from_db()
        self.assertEqual(community.city.latitude, 44.7)

    def test_missing_file_is_a_command_error(self):
        with self.assertRaisesMessage(CommandError, "File not found"):
            call_command("import_cities", "--file", "/nonexistent.xlsx", stdout=StringIO())
