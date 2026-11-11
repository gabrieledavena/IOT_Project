"""Installazione di un dispositivo con il QR del suo display (SP/pairing.py)."""
import re
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from SP import mqtt
from SP.installations import NEW_TOKEN_SESSION_KEY
from SP.models import Device, DevicePairing, PhotovoltaicSystem
from SP.tests.helpers import (
    create_community, create_consultant, create_customer, create_system, create_technician, install_device,
)

START_URL = "/sp/devices/pairing/"
CREDENTIALS_URL = "/sp/devices/pairing/credentials/"
# L'indirizzo del PC con il server, come lo usa il dispositivo nella rete locale
HOST = "192.168.1.10:8000"


class PairingTestCase(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.system = create_system(self.community, "Magazzino")
        self.staff = create_technician()
        self.client.force_login(self.staff)
        # Il dispositivo non ha login: le sue richieste arrivano da un altro client
        self.device = Client(HTTP_HOST=HOST)

    def start(self):
        response = self.device.post(START_URL)
        self.assertEqual(response.status_code, 201)
        return response.json()

    def poll(self, pairing, secret=None):
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            response = self.device.post(CREDENTIALS_URL, {"code": pairing["code"], "secret": secret or pairing["secret"]})
        self.published = publish
        return response

    def pair_url(self, pairing, suffix=""):
        return f"/sp/pair/{pairing['code']}/{suffix}"

    def detail_url(self, system=None):
        return f"/sp/installations/{(system or self.system).id}/"

    def choose(self, pairing, system=None):
        return self.client.post(self.pair_url(pairing), {"system": (system or self.system).id}, follow=True)

    def expire(self, pairing):
        DevicePairing.objects.filter(code=pairing["code"]).update(expires_at=timezone.now() - timedelta(seconds=1))


class DeviceRequestTests(PairingTestCase):
    def test_the_device_gets_a_code_a_secret_and_the_address_of_the_qr(self):
        pairing = self.start()
        self.assertRegex(pairing["code"], f"^[{DevicePairing.CODE_ALPHABET}]{{8}}$")
        self.assertRegex(pairing["secret"], r"^[0-9a-f]{40}$")
        self.assertEqual(pairing["url"], f"http://{HOST}/sp/pair/{pairing['code']}/")
        self.assertEqual((pairing["expires_in"], pairing["interval"]), (600, 5))
        # Del segreto si salva solo l'hash
        saved = DevicePairing.objects.get(code=pairing["code"])
        self.assertNotIn(pairing["secret"], saved.secret_hash)
        self.assertTrue(saved.check_secret(pairing["secret"]))

    def test_the_qr_stays_small(self):
        # Il QR del display ha 29 moduli per lato fino a 53 caratteri: con l'indirizzo IP di una rete locale ci sta
        self.assertLessEqual(len(self.start()["url"]), 53)

    def test_only_post(self):
        self.assertEqual(self.device.get(START_URL).status_code, 405)
        self.assertEqual(self.device.get(CREDENTIALS_URL).status_code, 405)

    def test_expired_requests_are_deleted_when_a_device_asks_for_a_code(self):
        old = self.start()
        self.expire(old)
        self.start()
        self.assertFalse(DevicePairing.objects.filter(code=old["code"]).exists())
        self.assertEqual(DevicePairing.objects.count(), 1)

    def test_without_the_secret_the_code_gives_nothing(self):
        pairing = self.start()
        self.choose(pairing)
        for secret in ("0" * 40, ""):
            response = self.device.post(CREDENTIALS_URL, {"code": pairing["code"], "secret": secret})
            self.assertEqual(response.status_code, 404)
        self.assertFalse(Device.objects.exists())
        # Il dispositivo con il segreto giusto riceve ancora le credenziali
        self.assertEqual(self.poll(pairing).status_code, 200)

    def test_expired_code(self):
        pairing = self.start()
        self.expire(pairing)
        response = self.poll(pairing)
        self.assertEqual((response.status_code, response.json()), (404, {"status": "expired"}))
        self.assertContains(self.client.get(self.pair_url(pairing)), "Codice non valido o scaduto", status_code=404)

    @override_settings(MQTT_PUBLIC_HOST="broker.example.org", MQTT_PUBLIC_PORT=8883)
    def test_configured_broker_address(self):
        pairing = self.start()
        self.choose(pairing)
        data = self.poll(pairing).json()
        self.assertEqual((data["mqtt_host"], data["mqtt_port"]), ("broker.example.org", 8883))


class InstallationWithTheQrTests(PairingTestCase):
    def test_the_device_receives_its_credentials_once_the_technician_chooses_the_system(self):
        pairing = self.start()
        response = self.poll(pairing)
        self.assertEqual((response.status_code, response.json()), (202, {"status": "pending"}))

        # Il tecnico scansiona il QR: il display mostra che è stato letto
        response = self.client.get(self.pair_url(pairing))
        self.assertContains(response, DevicePairing.objects.get().display_code)
        self.assertContains(response, "Magazzino")
        self.assertEqual(self.poll(pairing).json(), {"status": "scanned"})

        response = self.choose(pairing)
        self.assertRedirects(response, self.detail_url())
        self.assertContains(response, "riceverà le credenziali entro pochi secondi")
        self.assertContains(response, "In attesa del dispositivo")
        self.assertContains(response, 'data-pairing-step="pickup"')

        response = self.poll(pairing)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "installed")
        self.assertEqual(data["username"], f"pv-{self.system.id}")
        self.assertEqual((data["system_id"], data["system"]), (self.system.id, "Magazzino"))
        self.assertEqual((data["mqtt_host"], data["mqtt_port"]), ("192.168.1.10", 1883))
        self.assertTrue(mqtt.authenticate(data["username"], data["token"]))
        device = Device.objects.get(system=self.system)
        self.assertEqual(device.installed_by, self.staff)
        # Configurazione e stato retained: il dispositivo li riceve appena si iscrive
        topics = {message["topic"] for message in self.published.call_args.args[0]}
        self.assertEqual(topics, {mqtt.topic(self.system.id, mqtt.CONFIG), mqtt.topic(self.system.id, mqtt.STATUS)})

        # Le credenziali si ritirano una volta sola
        self.assertEqual(self.poll(pairing).status_code, 404)

        # La pagina dell'impianto aspetta che il dispositivo si colleghi al broker
        response = self.client.get(self.detail_url())
        self.assertContains(response, "Credenziali consegnate al dispositivo")
        self.assertContains(response, 'data-pairing-step="broker"')
        # Il worker MQTT riceve "online" dal dispositivo
        Device.objects.filter(pk=device.pk).update(online=True, last_seen=timezone.now())
        response = self.client.get(self.detail_url())
        self.assertNotContains(response, "Credenziali consegnate al dispositivo")
        self.assertNotContains(response, "data-pairing-step")

    def test_the_code_written_in_the_page_of_the_system(self):
        pairing = self.start()
        written = DevicePairing.objects.get().display_code.lower()
        response = self.client.post(f"{self.detail_url()}pair/", {"code": f" {written} "}, follow=True)
        self.assertRedirects(response, self.detail_url())
        self.assertContains(response, "In attesa del dispositivo")
        self.assertEqual(self.poll(pairing).json()["username"], f"pv-{self.system.id}")

    def test_wrong_code_written_in_the_page_of_the_system(self):
        response = self.client.post(f"{self.detail_url()}pair/", {"code": "ABCD-EFGH"}, follow=True)
        self.assertContains(response, "Codice non valido o scaduto")
        self.assertFalse(DevicePairing.objects.filter(system=self.system).exists())

    def test_a_new_device_replaces_the_old_one(self):
        old, old_token = install_device(self.system)
        Device.objects.filter(pk=old.pk).update(online=True, last_seen=timezone.now() - timedelta(minutes=5))
        pairing = self.start()
        self.assertContains(self.client.get(self.pair_url(pairing), {"q": "magazz"}), f"Sostituisce il dispositivo pv-{self.system.id}")
        self.choose(pairing)
        token = self.poll(pairing).json()["token"]
        self.assertFalse(mqtt.authenticate(old.username, old_token))
        self.assertTrue(mqtt.authenticate(old.username, token))
        # "online" è ancora quello del vecchio dispositivo: la pagina aspetta che si colleghi il nuovo
        self.assertContains(self.client.get(self.detail_url()), 'data-pairing-step="broker"')

    def test_only_the_last_device_chosen_for_a_system_receives_the_credentials(self):
        first, second = self.start(), self.start()
        self.choose(first)
        self.choose(second)
        self.assertEqual(self.poll(first).status_code, 404)
        self.assertEqual(self.poll(second).status_code, 200)

    def test_a_device_is_installed_on_one_system(self):
        other = create_system(self.community, "Sede")
        pairing = self.start()
        self.choose(pairing)
        response = self.client.get(self.pair_url(pairing))
        self.assertContains(response, "già abbinato")
        response = self.choose(pairing, other)
        self.assertContains(response, "Codice non valido o scaduto")
        self.assertEqual(self.poll(pairing).json()["system_id"], self.system.id)

    def test_expired_code_cannot_be_chosen(self):
        pairing = self.start()
        self.expire(pairing)
        self.assertContains(self.choose(pairing), "Codice non valido o scaduto")
        self.assertIsNone(DevicePairing.objects.get().system)

    def test_unknown_system(self):
        pairing = self.start()
        self.assertEqual(self.client.post(self.pair_url(pairing), {"system": "abc"}).status_code, 404)
        self.assertEqual(self.client.post(self.pair_url(pairing), {"system": "999999"}).status_code, 404)

    def test_systems_to_choose(self):
        installed = create_system(self.community, "Sede")
        install_device(installed)
        newest = create_system(create_community("Bianchi Srl"), "Capannone")
        pairing = self.start()

        # Senza ricerca, i più recenti senza dispositivo
        content = self.client.get(self.pair_url(pairing)).content.decode()
        self.assertNotIn("Sede", content)
        self.assertLess(content.index("Capannone"), content.index("Magazzino"))

        response = self.client.get(self.pair_url(pairing), {"q": "rossi"})
        self.assertContains(response, "Sede")
        self.assertContains(response, "Magazzino")
        self.assertNotContains(response, newest.name)


class NewSystemForTheDeviceTests(PairingTestCase):
    def form(self):
        return {"community": self.community.id, "name": "Capannone 2", "max_power": "12.5", "brand": "SunPower"}

    def test_the_device_is_installed_on_the_new_system(self):
        pairing = self.start()
        response = self.client.get(self.pair_url(pairing, "new/"))
        self.assertContains(response, DevicePairing.objects.get().display_code)

        response = self.client.post(self.pair_url(pairing, "new/"), self.form(), follow=True)
        system = PhotovoltaicSystem.objects.get(name="Capannone 2")
        self.assertRedirects(response, self.detail_url(system))
        self.assertContains(response, "In attesa del dispositivo")
        # Nessun token da copiare: lo riceve il dispositivo
        self.assertNotIn(NEW_TOKEN_SESSION_KEY, self.client.session)
        self.assertFalse(Device.objects.exists())
        self.assertEqual(self.poll(pairing).json()["system_id"], system.id)

    def test_expired_code(self):
        pairing = self.start()
        self.expire(pairing)
        self.assertRedirects(self.client.get(self.pair_url(pairing, "new/")), self.pair_url(pairing),
                             fetch_redirect_response=False)
        # L'impianto viene creato lo stesso: il tecnico scansionerà il nuovo QR
        response = self.client.post(self.pair_url(pairing, "new/"), self.form(), follow=True)
        self.assertContains(response, "il codice del dispositivo è scaduto")
        self.assertTrue(PhotovoltaicSystem.objects.filter(name="Capannone 2").exists())
        self.assertFalse(Device.objects.exists())

    def test_invalid_form_keeps_the_code(self):
        pairing = self.start()
        response = self.client.post(self.pair_url(pairing, "new/"), {**self.form(), "max_power": "0"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, DevicePairing.objects.get().display_code)


class AccessTests(PairingTestCase):
    def test_technicians_only(self):
        pairing = self.start()
        urls = (self.pair_url(pairing), self.pair_url(pairing, "new/"))
        for user in (create_customer(self.community), create_consultant()):
            self.client.force_login(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(f"{self.detail_url()}pair/", {"code": pairing["code"]}).status_code, 403)

        # Chi scansiona il QR senza essere entrato passa dal login e torna alla pagina del QR
        self.client.logout()
        url = self.pair_url(pairing)
        self.assertRedirects(self.client.get(url), f"/sp/login/?next={url}")
        self.assertIsNone(DevicePairing.objects.get().scanned_at)
        self.assertIsNone(DevicePairing.objects.get().system)

    def test_the_page_of_the_qr_shows_only_the_code(self):
        pairing = self.start()
        content = self.client.get(self.pair_url(pairing)).content.decode()
        self.assertNotIn(pairing["secret"], content)
        self.assertIsNone(re.search(r"[0-9a-f]{40}", content))
