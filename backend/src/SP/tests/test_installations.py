import json
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from SP.models import Device, PhotovoltaicSystem
from SP.tests.helpers import create_community, create_customer, create_system, install_device

LIST_URL = "/sp/installations/"
NEW_URL = "/sp/installations/new/"


def shown_token(response):
    match = re.search(r'<code class="user-select-all">([0-9a-f]{40})</code>', response.content.decode())
    return match.group(1) if match else None


class InstallationTestCase(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.customer = create_customer(self.community)
        self.system = create_system(self.community, "Magazzino")
        self.staff = User.objects.create_user("tecnico", is_staff=True)
        self.client.force_login(self.staff)

    def detail_url(self, system=None):
        return f"/sp/installations/{(system or self.system).id}/"

    def post_action(self, action, **data):
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.detail_url(), {"action": action, **data}, follow=True)
        return response, publish


class AccessTests(InstallationTestCase):
    def test_staff_only(self):
        self.client.force_login(self.customer)
        for url in (LIST_URL, NEW_URL, self.detail_url()):
            self.assertEqual(self.client.get(url).status_code, 403)

        self.client.logout()
        self.assertRedirects(self.client.get(LIST_URL), f"/sp/login/?next={LIST_URL}")

    def test_menu_link_for_staff(self):
        self.assertContains(self.client.get("/"), LIST_URL)
        self.client.force_login(self.customer)
        self.assertNotContains(self.client.get("/"), LIST_URL)


class InstallationListTests(InstallationTestCase):
    def test_lists_the_systems_with_the_state_of_their_device(self):
        online = create_system(self.community, "Sede")
        device, _ = install_device(online)
        device.online = True
        device.save()

        response = self.client.get(LIST_URL)
        self.assertContains(response, "Magazzino")
        self.assertContains(response, "Non installato")
        self.assertContains(response, f"pv-{online.id}")

        response = self.client.get(LIST_URL, {"device": "online"})
        self.assertContains(response, "Sede")
        self.assertNotContains(response, "Magazzino")
        self.assertContains(self.client.get(LIST_URL, {"device": "none"}), "Magazzino")
        self.assertNotContains(self.client.get(LIST_URL, {"q": "sede"}), "Magazzino")


class NewInstallationTests(InstallationTestCase):
    def form(self, **fields):
        return {"community": self.community.id, "name": "Capannone 2", "max_power": "12.5", "brand": "SunPower",
                **fields}

    def test_creates_the_system_with_its_device_and_shows_the_token_once(self):
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(NEW_URL, self.form(), follow=True)

        system = PhotovoltaicSystem.objects.get(name="Capannone 2")
        self.assertEqual((system.community, system.max_power), (self.community, 12.5))
        self.assertEqual(system.device.installed_by, self.staff)
        token = shown_token(response)
        self.assertTrue(system.device.check_token(token))
        self.assertContains(response, f"--device pv-{system.id} --token {token} --simulate")
        # Configurazione e stato pubblicati per il nuovo dispositivo
        topics = [message["topic"] for message in publish.call_args.args[0]]
        self.assertEqual(topics, [f"solarfamily/systems/{system.id}/config", f"solarfamily/systems/{system.id}/status"])

        # Ricaricando la pagina il token non si vede più
        self.assertIsNone(shown_token(self.client.get(f"/sp/installations/{system.id}/")))

    def test_invalid_power_is_rejected(self):
        response = self.client.post(NEW_URL, self.form(max_power="0"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "maggiore di zero")
        self.assertFalse(PhotovoltaicSystem.objects.filter(name="Capannone 2").exists())


class DeviceActionTests(InstallationTestCase):
    def test_install_on_an_existing_system(self):
        response, _ = self.post_action("install")

        device = Device.objects.get(system=self.system)
        self.assertTrue(device.check_token(shown_token(response)))
        self.assertContains(response, "Dispositivo pv-")

    def test_new_credentials_invalidate_the_old_ones(self):
        device, old_token = install_device(self.system)

        response, _ = self.post_action("regenerate")

        device.refresh_from_db()
        self.assertFalse(device.check_token(old_token))
        self.assertTrue(device.check_token(shown_token(response)))

    def test_uninstall_removes_the_device_and_its_retained_messages(self):
        install_device(self.system)

        _, publish = self.post_action("uninstall")

        self.assertFalse(Device.objects.exists())
        messages = publish.call_args.args[0]
        self.assertTrue(all(message["payload"] is None and message["retain"] for message in messages))


class ForceStatusTests(InstallationTestCase):
    def test_saves_the_status_as_a_check_and_sends_it_to_the_device(self):
        install_device(self.system)

        response, publish = self.post_action("force_status", status="DRT")

        self.system.refresh_from_db()
        self.assertEqual((self.system.status, self.system.previous_status), ("DRT", "OK"))
        self.assertLess(timezone.now() - self.system.status_changed_at, timedelta(seconds=5))
        self.assertEqual(self.system.last_check, self.system.status_changed_at)
        [message] = publish.call_args.args[0]
        self.assertEqual(message["topic"], f"solarfamily/systems/{self.system.id}/status")
        self.assertContains(response, "Stato impostato a «Pannelli sporchi» e inviato al dispositivo.")

    def test_same_status_again_is_a_new_change(self):
        # Per riprovare il lavaggio: ogni stato impostato è un nuovo cambiamento
        install_device(self.system)
        self.post_action("force_status", status="DRT")
        self.system.refresh_from_db()
        first_change = self.system.status_changed_at

        self.post_action("force_status", status="DRT")

        self.system.refresh_from_db()
        self.assertGreater(self.system.status_changed_at, first_change)
        self.assertEqual(self.system.previous_status, "DRT")

    def test_without_device_the_status_is_only_saved(self):
        response, publish = self.post_action("force_status", status="FLT")

        self.system.refresh_from_db()
        self.assertEqual(self.system.status, "FLT")
        self.assertEqual(publish.call_args.args[0], [])
        self.assertContains(response, "Stato impostato a «Probabile guasto».")

    def test_invalid_status(self):
        response = self.client.post(self.detail_url(), {"action": "force_status", "status": "XYZ"})

        self.assertEqual(response.status_code, 400)
        self.system.refresh_from_db()
        self.assertEqual(self.system.status, "OK")


class AdminStatusChangeTests(InstallationTestCase):
    """Cambiare lo stato dall'admin è un nuovo cambiamento: il dispositivo reagisce (con DRT parte la pompa)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("admin", password=None))

    def change(self, status):
        data = {"name": self.system.name, "max_power": self.system.max_power, "brand": self.system.brand,
                "community": self.community.id, "status": status, "last_check_0": "", "last_check_1": ""}
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/admin/SP/photovoltaicsystem/{self.system.id}/change/", data)
        self.assertEqual(response.status_code, 302)
        self.system.refresh_from_db()
        return publish

    def test_each_status_change_has_a_new_date_sent_to_the_device(self):
        install_device(self.system)

        publish = self.change("DRT")
        first_change = self.system.status_changed_at
        self.assertEqual((self.system.status, self.system.previous_status), ("DRT", "OK"))
        self.assertLess(timezone.now() - first_change, timedelta(seconds=5))
        status = json.loads(publish.call_args.args[0][1]["payload"])
        self.assertEqual(status["changed_at"], first_change.isoformat())

        self.change("OK")
        self.change("DRT")
        self.assertGreater(self.system.status_changed_at, first_change)
        self.assertEqual(self.system.previous_status, "OK")

    def test_other_changes_keep_the_date_of_the_last_status_change(self):
        self.change("DRT")
        changed_at = self.system.status_changed_at

        self.system.max_power = 9.5
        self.change("DRT")

        self.assertEqual(self.system.status_changed_at, changed_at)
