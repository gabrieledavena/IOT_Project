import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import pandas as pd
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from rest_framework.authtoken.models import Token

from SP.management.commands.import_cities import NAME_COLUMN, PROVINCE_COLUMN, REGION_COLUMN
from SP.models import City, Community, Customer, PanelData, PhotovoltaicSystem
from SP.tests.helpers import create_community


def run(command, *args):
    out = StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


class BridgeUserCommandTests(TestCase):
    def token_from_output(self, *args):
        return run("create_bridge_user", *args).splitlines()[1]

    def test_is_idempotent_and_reset_generates_a_new_token(self):
        first, second, reset = self.token_from_output(), self.token_from_output(), self.token_from_output("--reset")

        self.assertEqual(first, second)
        self.assertNotEqual(second, reset)

    def test_bridge_user_can_only_add_readings(self):
        run("create_bridge_user")
        user = User.objects.get(username="bridge")

        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.get_all_permissions(), {"SP.add_paneldata"})


class PopulateDbCommandTests(TestCase):
    def test_creates_fictitious_data_and_keeps_the_bridge_user(self):
        run("create_bridge_user")
        token = Token.objects.get(user__username="bridge").key

        run("populate_db", "--days", "2", "--systems_per_community", "1")

        self.assertEqual(Community.objects.count(), 10)
        self.assertEqual(Customer.objects.count(), 15)
        self.assertEqual(PhotovoltaicSystem.objects.count(), 10)
        self.assertTrue(PanelData.objects.exists())
        self.assertEqual(Token.objects.get(user__username="bridge").key, token)


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
