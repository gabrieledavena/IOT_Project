import json
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from SP.models import PanelData, PhotovoltaicSystem
from SP.monitoring import SystemChecker
from SP.tests.helpers import (
    FakeModel, create_city, create_community, create_customer, create_system, fake_weather, install_device,
)
from SP.weather import WeatherUnavailable

Status = PhotovoltaicSystem.Status

# Il modello prevede 2,4 kWh per kW installato al giorno: un impianto da 1 kW che produce 0,1 kW
# per tutti i due giorni controllati (4,8 kWh) produce esattamente quanto previsto
PREDICTED_YIELD = 2.4
EXPECTED_POWER = 0.1


def add_check_days(system, share, first_day, days=2):
    """Una misura al minuto per `days` giorni da first_day, con la potenza che dà `share` della previsione."""
    start = datetime.combine(first_day, time.min, tzinfo=dt_timezone.utc)
    PanelData.objects.bulk_create([
        PanelData(system=system, time_stamp=start + timedelta(minutes=m), temperature=20.0, lightness=500.0,
                  power=EXPECTED_POWER * share)
        for m in range(days * 24 * 60)
    ])


class MonitoringTestCase(TestCase):
    TODAY = date(2026, 9, 30)

    def setUp(self):
        patchers = [
            mock.patch("forecast.predictor.load_model", return_value=FakeModel(PREDICTED_YIELD)),
            mock.patch("SP.monitoring.get_daily_weather", side_effect=lambda city, first, last: fake_weather(first, last)),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.modena = create_city("Modena", latitude=44.65, longitude=10.93)
        self.community = create_community("Rossi Trasporti", city=self.modena)

    def system(self, share, community=None, name="Impianto", first_day=None):
        system = create_system(community or self.community, name, max_power=1.0)
        add_check_days(system, share, first_day or self.TODAY - timedelta(days=2))
        return system

    def check(self, system):
        return SystemChecker(today=self.TODAY).check(system)


class FirstCheckTests(MonitoringTestCase):
    def test_production_within_10_percent_of_the_forecast_is_ok(self):
        result = self.check(self.system(0.92))

        self.assertEqual(result.status, Status.OK)
        self.assertAlmostEqual(result.ratio, 0.92, places=2)

    def test_incomplete_days_are_not_checked(self):
        system = create_system(self.community, max_power=1.0)
        add_check_days(system, 1.0, self.TODAY - timedelta(days=2), days=1)  # manca ieri

        self.assertIsNone(self.check(system))

    def test_without_weather_the_system_is_not_checked(self):
        system = self.system(0.5)

        with mock.patch("SP.monitoring.get_daily_weather", side_effect=WeatherUnavailable("offline")):
            self.assertIsNone(self.check(system))


class SecondCheckTests(MonitoringTestCase):
    def test_low_production_with_normal_neighbours_is_a_probable_fault(self):
        system = self.system(0.7)
        for i in range(3):
            self.system(0.97, name=f"Vicino {i}")

        result = self.check(system)

        self.assertEqual(result.status, Status.FAULT)
        self.assertEqual((result.neighbours, result.anomalous_neighbours), (3, 0))

    def test_low_production_like_most_neighbours_means_dirty_panels(self):
        system = self.system(0.8)
        for i, share in enumerate([0.82, 0.85, 0.97]):
            self.system(share, name=f"Vicino {i}")

        result = self.check(system)

        self.assertEqual(result.status, Status.DIRTY)
        self.assertEqual((result.neighbours, result.anomalous_neighbours), (3, 2))

    def test_at_most_10_neighbours_of_the_same_city_first_those_of_the_same_community(self):
        system = self.system(0.7)
        other_company = create_community("Bianchi Srl", city=self.modena)
        for i in range(8):
            self.system(0.8, community=other_company, name=f"Bianchi {i}")
        colleagues = [self.system(0.97, name=f"Rossi {i}") for i in range(4)]

        neighbours = SystemChecker(today=self.TODAY).neighbours(system)

        self.assertEqual(len(neighbours), 10)
        self.assertEqual(neighbours[:4], colleagues)

    def test_without_other_systems_in_the_city_the_nearest_city_is_used(self):
        system = self.system(0.7)
        # A Bologna un impianto senza misure: non conta, si passa alla città successiva
        create_system(create_community("Senza misure", city=create_city("Bologna", latitude=44.49, longitude=11.34)))
        parma = create_community("Verdi Srl", city=create_city("Parma", latitude=44.80, longitude=10.33))
        bari = create_community("Neri Srl", city=create_city("Bari", latitude=41.12, longitude=16.87))
        near = self.system(0.97, community=parma, name="Parma")
        self.system(0.8, community=bari, name="Bari")

        checker = SystemChecker(today=self.TODAY)

        self.assertEqual(checker.neighbours(system), [near])
        self.assertEqual(checker.check(system).status, Status.FAULT)

    def test_without_any_neighbour_the_panels_are_considered_dirty(self):
        result = self.check(self.system(0.7))

        self.assertEqual(result.status, Status.DIRTY)
        self.assertEqual(result.neighbours, 0)


class CheckSystemsCommandTests(MonitoringTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("check_systems", *args, stdout=out)
        return out.getvalue()

    def recent_system(self, share, **kwargs):
        # Il comando controlla i due giorni completi prima di oggi
        return self.system(share, first_day=timezone.now().date() - timedelta(days=2), **kwargs)

    def test_saves_status_and_date_and_waits_2_days_before_checking_again(self):
        faulty = self.recent_system(0.6, name="Guasto")
        self.recent_system(1.0, name="Sano")

        output = self.run_command()

        faulty.refresh_from_db()
        self.assertEqual(faulty.status, Status.FAULT)
        self.assertLess(timezone.now() - faulty.last_check, timedelta(minutes=1))
        self.assertIn("Rossi Trasporti:", output)
        self.assertIn("Checked 2 systems (1 OK, 0 Pannelli sporchi, 1 Probabile guasto)", output)

        self.assertIn("No system to check", self.run_command())
        self.assertIn("Checked 2 systems", self.run_command("--all"))
        PhotovoltaicSystem.objects.update(last_check=timezone.now() - timedelta(days=2))
        self.assertIn("Checked 2 systems", self.run_command())

    def test_status_changes_are_sent_to_the_installed_devices(self):
        # Due impianti su tre producono poco: i loro pannelli sono sporchi
        dirty = self.recent_system(0.6, name="Sporco")
        healthy = self.recent_system(1.0, name="Sano")
        not_installed = self.recent_system(0.6, name="Senza dispositivo")
        install_device(dirty)
        install_device(healthy)

        with mock.patch("SP.mqtt.publish", return_value=True) as publish:
            output = self.run_command()

        # Solo l'impianto installato il cui stato è cambiato (quello sano resta OK)
        [message] = publish.call_args.args[0]
        self.assertEqual(message["topic"], f"solarfamily/systems/{dirty.id}/status")
        self.assertTrue(message["retain"])
        payload = json.loads(message["payload"])
        dirty.refresh_from_db()
        self.assertEqual((payload["status"], payload["previous"]), ("DRT", "OK"))
        self.assertEqual(payload["changed_at"], dirty.status_changed_at.isoformat())
        self.assertEqual(dirty.status_changed_at, dirty.last_check)
        self.assertIn("Status change sent to the devices of 1 systems", output)
        not_installed.refresh_from_db()
        self.assertEqual(not_installed.status, Status.DIRTY)

        # Al controllo successivo lo stato non cambia: niente da inviare, la data del cambiamento resta quella
        changed_at = dirty.status_changed_at
        with mock.patch("SP.mqtt.publish", return_value=False) as publish:
            self.run_command("--all")
        self.assertEqual(publish.call_args.args[0], [])
        dirty.refresh_from_db()
        self.assertEqual(dirty.status_changed_at, changed_at)

    def test_community_option_and_systems_without_readings(self):
        self.recent_system(1.0)
        other = create_community("Bianchi Srl", city=self.modena)
        create_system(other, "Senza misure")

        output = self.run_command("--community", str(other.id))

        self.assertNotIn("Rossi Trasporti", output)
        self.assertIn("Senza misure: not checked", output)
        self.assertIsNone(PhotovoltaicSystem.objects.get(name="Senza misure").last_check)

    def test_without_model_it_is_a_command_error(self):
        self.recent_system(1.0)

        with mock.patch("forecast.predictor.load_model", return_value=None), \
                self.assertRaisesMessage(CommandError, "modello di previsione"):
            self.run_command()


class SystemPageStatusTests(MonitoringTestCase):
    def test_detail_page_shows_status_and_last_check(self):
        system = create_system(self.community)
        self.client.force_login(User.objects.create_user("staff", is_staff=True))
        url = f"/sp/system/{system.id}/"

        self.assertContains(self.client.get(url, follow=True), "In attesa del primo controllo")

        system.status, system.last_check = Status.FAULT, datetime(2026, 9, 29, 6, 0, tzinfo=dt_timezone.utc)
        system.save()
        response = self.client.get(url, follow=True)
        self.assertContains(response, "Probabile guasto")
        self.assertContains(response, "29/09/2026 06:00")

    def test_customer_sees_the_status_of_the_systems_of_its_community(self):
        system = create_system(self.community)
        self.client.force_login(create_customer(self.community))

        self.assertContains(self.client.get(f"/sp/system/{system.id}/", follow=True), "Stato dell'impianto")
