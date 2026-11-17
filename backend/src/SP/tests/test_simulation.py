"""Simulazione degli impianti senza sensore di una community, eseguita dal suo master (SP/simulation.py)."""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from SP import mqtt
from SP.gateways import assign_node, install_gateway, uninstall_gateway
from SP.installations import install_device as install_with_token, uninstall_device
from SP.models import Device, Gateway, LoRaNode, PanelData, SimulatedSensor
from SP.simulation import start, stop
from SP.tests.helpers import create_community, create_customer, create_system, create_technician

KEY = "00112233445566778899aabbccddeeff"


def published(publish):
    """Messaggi pubblicati, nell'ordine: (topic, payload decodificato)."""
    result = []
    for call in publish.call_args_list:
        for message in call.args[0]:
            payload = message["payload"]
            result.append((message["topic"], json.loads(payload) if payload else payload))
    return result


class SimulationTestCase(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.real = create_system(self.community, "Sede", max_power=6.0)
        self.warehouse = create_system(self.community, "Magazzino", max_power=10.0)
        self.shop = create_system(self.community, "Negozio", max_power=4.0)
        self.admin = User.objects.create_superuser("admin", password="test-password")
        self.client.force_login(self.admin)
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            self.gateway, _ = install_gateway(self.community, self.admin)
            # Il nodo vero sul tetto della sede
            node = LoRaNode.objects.create(gateway=self.gateway, node_id="A3F2C1D0", key=KEY)
            assign_node(node, self.real, self.admin)
        Gateway.objects.filter(pk=self.gateway.pk).update(online=True)

    @property
    def url(self):
        return f"/sp/simulation/{self.community.id}/"

    def post(self, data, **headers):
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, data, **headers)
        self.published = published(publish)
        return response

    def start(self):
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            start(self.community, self.admin)
        self.published = published(publish)

    def simulation_message(self):
        """L'ultimo elenco degli impianti simulati inviato al master."""
        topic = mqtt.gateway_topic(self.community.id, mqtt.SIMULATION)
        return [payload for name, payload in self.published if name == topic][-1]


class SimulationPageTests(SimulationTestCase):
    def test_only_administrators(self):
        self.client.force_login(create_technician())
        self.assertEqual(self.client.get("/sp/simulation/").status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(create_customer(self.community))
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        self.assertRedirects(self.client.get("/sp/simulation/"), "/sp/login/?next=/sp/simulation/")

    def test_the_menu_shows_the_page_to_administrators_only(self):
        self.assertContains(self.client.get("/sp/simulation/"), 'href="/sp/simulation/"')
        self.client.force_login(create_technician())
        self.assertNotContains(self.client.get("/sp/communities/"), 'href="/sp/simulation/"')

    def test_communities_with_a_master(self):
        create_community("Bianchi Srl")   # senza master: non si può simulare
        response = self.client.get("/sp/simulation/")
        self.assertContains(response, "Rossi Trasporti")
        self.assertNotContains(response, "Bianchi Srl")
        self.assertContains(response, "2 senza sensore")

    def test_start_simulates_the_systems_without_a_sensor(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Avvia la simulazione")
        self.assertContains(response, "Magazzino, Negozio")

        response = self.post({"action": "start"})
        self.assertRedirects(response, self.url)
        self.assertEqual(set(SimulatedSensor.objects.values_list("system__name", flat=True)), {"Magazzino", "Negozio"})
        # Un dispositivo senza token, come quello di un nodo LoRa; il nodo vero della sede resta
        device = Device.objects.get(system=self.warehouse)
        self.assertEqual((device.token_hash, device.installed_by), ("", self.admin))
        self.assertFalse(mqtt.authenticate(device.username, ""))
        self.assertTrue(LoRaNode.objects.filter(system=self.real).exists())
        self.assertFalse(SimulatedSensor.objects.filter(system=self.real).exists())
        # Configurazione e stato degli impianti (retained), poi l'elenco al master con i valori iniziali
        topics = [topic for topic, _ in self.published]
        self.assertIn(mqtt.topic(self.warehouse.id, mqtt.CONFIG), topics)
        self.assertIn(mqtt.topic(self.shop.id, mqtt.STATUS), topics)
        self.assertEqual(topics[-1], mqtt.gateway_topic(self.community.id, mqtt.SIMULATION))
        systems = self.simulation_message()["systems"]
        self.assertEqual([system["system"] for system in systems], ["Magazzino", "Negozio"])
        self.assertEqual(systems[0], {"system_id": self.warehouse.id, "system": "Magazzino", "max_power_kw": 10.0,
                                      "power": 5.0, "temperature": 24.0, "lightness": 50000.0})

        response = self.client.get(self.url)
        self.assertContains(response, "Ferma la simulazione")
        self.assertContains(response, 'name="power" id="power-%d"' % self.warehouse.id)
        self.assertContains(response, 'value="5.0"')   # senza la virgola italiana, che il campo non accetta
        self.assertNotContains(response, "Avvia la simulazione")

    def test_systems_added_later(self):
        self.start()
        new = create_system(self.community, "Uffici")
        self.assertContains(self.client.get(self.url), "Simula anche 1 nuovo impianto")
        self.post({"action": "start"})
        self.assertTrue(SimulatedSensor.objects.filter(system=new).exists())
        self.assertEqual(len(self.simulation_message()["systems"]), 3)

    def test_a_community_without_master(self):
        other = create_community("Bianchi Srl")
        create_system(other, "Capannone")
        with mock.patch("SP.mqtt.publish"):
            response = self.client.post(f"/sp/simulation/{other.id}/", {"action": "start"}, follow=True)
        self.assertContains(response, "non ha un master")
        self.assertFalse(SimulatedSensor.objects.exists())

    def test_change_the_values(self):
        self.start()
        response = self.post({"action": "save", "system": self.warehouse.id, "power": "7.25", "temperature": "31.5",
                              "lightness": "92000"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"system": self.warehouse.id, "power": 7.25, "temperature": 31.5,
                                           "lightness": 92000.0})
        sensor = SimulatedSensor.objects.get(system=self.warehouse)
        self.assertEqual((sensor.power, sensor.temperature, sensor.lightness), (7.25, 31.5, 92000.0))
        # Il master riceve subito l'elenco con i valori nuovi
        values = {system["system"]: system for system in self.simulation_message()["systems"]}
        self.assertEqual(values["Magazzino"]["power"], 7.25)
        self.assertEqual(values["Negozio"]["power"], 2.0)

    def test_values_out_of_range(self):
        self.start()
        response = self.post({"action": "save", "system": self.shop.id, "power": "4.5", "temperature": "20",
                              "lightness": "1000"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("power", response.json()["errors"])   # più della potenza installata (4 kW)
        response = self.post({"action": "save", "system": self.shop.id, "power": "1", "temperature": "90",
                              "lightness": "-1"}, HTTP_ACCEPT="application/json")
        self.assertEqual(set(response.json()["errors"]), {"temperature", "lightness"})
        self.assertEqual(SimulatedSensor.objects.get(system=self.shop).power, 2.0)
        self.assertEqual(self.published, [])

    def test_values_without_javascript(self):
        self.start()
        response = self.post({"action": "save", "system": self.shop.id, "power": "3", "temperature": "20",
                              "lightness": "1000"})
        self.assertRedirects(response, self.url)
        self.assertEqual(SimulatedSensor.objects.get(system=self.shop).power, 3.0)
        response = self.post({"action": "save", "system": self.shop.id, "power": "9", "temperature": "20",
                              "lightness": "1000"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "minore o uguale a 4", status_code=400)

    def test_only_simulated_systems_of_the_community(self):
        self.start()
        response = self.post({"action": "save", "system": self.real.id, "power": "1", "temperature": "20",
                              "lightness": "1000"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 404)
        other = create_community("Bianchi Srl")
        with mock.patch("SP.mqtt.publish"):
            response = self.client.post(f"/sp/simulation/{other.id}/", {
                "action": "save", "system": self.shop.id, "power": "1", "temperature": "20", "lightness": "0",
            }, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 404)

    def test_stop(self):
        self.start()
        response = self.post({"action": "stop"})
        self.assertRedirects(response, self.url)
        self.assertFalse(SimulatedSensor.objects.exists())
        self.assertFalse(Device.objects.filter(system__in=[self.warehouse, self.shop]).exists())
        self.assertTrue(Device.objects.filter(system=self.real).exists())
        self.assertEqual(self.simulation_message()["systems"], [])
        # Cancellati i messaggi retained degli impianti
        self.assertIn((mqtt.topic(self.shop.id, mqtt.STATUS), None), self.published)

    def test_status(self):
        self.start()
        Device.objects.filter(system=self.warehouse).update(online=True)
        PanelData.objects.create(system=self.warehouse, time_stamp="2026-11-17T10:41:00Z", temperature=24.0,
                                 lightness=50000.0, power=5.0)
        data = self.client.get(f"{self.url}status/").json()
        self.assertTrue(data["gateway"]["online"])
        systems = {system["id"]: system for system in data["systems"]}
        self.assertEqual(set(systems), {self.real.id, self.warehouse.id, self.shop.id})
        self.assertTrue(systems[self.warehouse.id]["online"])
        self.assertEqual(systems[self.warehouse.id]["reading"]["power"], 5.0)
        self.assertIsNone(systems[self.shop.id]["reading"])


class SimulationMasterTests(SimulationTestCase):
    """Il master pubblica per gli impianti simulati come per quelli dei suoi nodi."""

    def test_topics_of_the_simulated_systems(self):
        username = self.gateway.username
        self.assertTrue(mqtt.may_access(username, mqtt.gateway_topic(self.community.id, mqtt.SIMULATION),
                                        mqtt.ACC_SUBSCRIBE))
        self.assertFalse(mqtt.may_access(username, mqtt.gateway_topic(self.community.id, mqtt.SIMULATION),
                                         mqtt.ACC_WRITE))
        telemetry = mqtt.topic(self.warehouse.id, mqtt.TELEMETRY)
        self.assertFalse(mqtt.may_access(username, telemetry, mqtt.ACC_WRITE))
        self.start()
        for name in (mqtt.TELEMETRY, mqtt.CONNECTION, mqtt.EVENTS):
            self.assertTrue(mqtt.may_access(username, mqtt.topic(self.warehouse.id, name), mqtt.ACC_WRITE))
        self.assertTrue(mqtt.may_access(username, mqtt.topic(self.warehouse.id, mqtt.STATUS), mqtt.ACC_SUBSCRIBE))
        self.assertFalse(mqtt.may_access(username, mqtt.topic(self.warehouse.id, mqtt.STATUS), mqtt.ACC_WRITE))
        # Non quelli di un'altra community
        other = create_community("Bianchi Srl")
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            other_gateway, _ = install_gateway(other)
        self.assertFalse(mqtt.may_access(other_gateway.username, telemetry, mqtt.ACC_WRITE))
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            stop(self.community)
        self.assertFalse(mqtt.may_access(username, telemetry, mqtt.ACC_WRITE))

    def test_readings_connection_and_washing(self):
        self.start()
        payload = {"time_stamp": "2026-11-17T10:41:00+00:00", "temperature": 24.0, "lightness": 50000.0,
                   "power": 5.0, "samples": 6}
        reading = mqtt.handle_message(mqtt.topic(self.warehouse.id, mqtt.TELEMETRY), json.dumps(payload).encode())
        self.assertEqual((reading.system, reading.power), (self.warehouse, 5.0))
        self.assertTrue(Device.objects.get(system=self.warehouse).online)
        mqtt.handle_message(mqtt.topic(self.shop.id, mqtt.EVENTS), b'{"event": "pump_off"}')
        self.assertIsNotNone(Device.objects.get(system=self.shop).last_cleaning)
        # Il master cade: anche gli impianti che simula non sono più sentiti
        mqtt.handle_message(mqtt.gateway_topic(self.community.id, mqtt.CONNECTION), b"offline")
        self.assertFalse(Device.objects.filter(online=True).exists())

    def test_the_worker_republishes_the_simulation(self):
        self.start()
        messages = mqtt.gateway_simulation_messages([self.community.id])
        self.assertEqual(messages[0]["topic"], mqtt.gateway_topic(self.community.id, mqtt.SIMULATION))
        self.assertTrue(messages[0]["retain"])
        self.assertEqual(len(json.loads(messages[0]["payload"])["systems"]), 2)


class RealSensorTests(SimulationTestCase):
    """Un impianto simulato che riceve un sensore vero non è più simulato."""

    def test_a_bridge_with_credentials(self):
        self.start()
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            device, token = install_with_token(self.warehouse, self.admin)
        self.assertFalse(SimulatedSensor.objects.filter(system=self.warehouse).exists())
        self.assertTrue(mqtt.authenticate(device.username, token))
        self.published = published(publish)
        self.assertEqual([system["system"] for system in self.simulation_message()["systems"]], ["Negozio"])

    def test_a_lora_node(self):
        self.start()
        node = LoRaNode.objects.create(gateway=self.gateway, node_id="B4E3D2C1", key=KEY)
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            assign_node(node, self.shop, self.admin)
        self.published = published(publish)
        self.assertFalse(SimulatedSensor.objects.filter(system=self.shop).exists())
        self.assertEqual(Device.objects.get(system=self.shop).token_hash, "")
        self.assertEqual([system["system"] for system in self.simulation_message()["systems"]], ["Magazzino"])

    def test_uninstall_from_the_installation_page(self):
        self.start()
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            uninstall_device(self.shop)
        self.assertFalse(SimulatedSensor.objects.filter(system=self.shop).exists())
        self.assertFalse(Device.objects.filter(system=self.shop).exists())

    def test_uninstall_the_master(self):
        self.start()
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            uninstall_gateway(self.community)
        self.assertFalse(SimulatedSensor.objects.exists())
        self.assertFalse(Device.objects.exists())

    def test_community_page(self):
        self.start()
        self.client.force_login(create_technician())
        response = self.client.get(f"/sp/communities/{self.community.id}/")
        self.assertContains(response, "sensore simulato")


class ServerAddressTests(SimulationTestCase):
    """L'indirizzo del PC nella rete locale, da scrivere nel master: il server in docker non lo vede da solo."""

    @override_settings(SERVER_LAN_HOST="")
    def test_unknown_without_start_sh(self):
        response = self.client.get("/sp/simulation/")
        self.assertIsNone(response.context["server_address"])
        self.assertContains(response, "./start.sh")

    @override_settings(SERVER_LAN_HOST="192.168.1.62", SERVER_LAN_PORT=8000)
    def test_the_address_given_by_start_sh(self):
        with mock.patch("SP.simulation._answers", return_value=True) as answers:
            response = self.client.get(self.url)
        answers.assert_called_once_with("http://192.168.1.62:8000")
        self.assertContains(response, "http://192.168.1.62:8000")
        self.assertContains(response, "Risponde")
        self.assertContains(response, "Avanzate")

    @override_settings(SERVER_LAN_HOST="192.168.1.60", SERVER_LAN_PORT=8000)
    def test_an_old_address(self):
        # Il PC ha cambiato IP dopo l'avvio: la pagina lo dice
        with mock.patch("SP.simulation._answers", return_value=False):
            response = self.client.get("/sp/simulation/")
        self.assertContains(response, "http://192.168.1.60:8000")
        self.assertContains(response, "Non risponde")
        self.assertContains(response, "non ha più questo indirizzo")

    @override_settings(SERVER_LAN_HOST="192.168.1.60")
    def test_the_address_of_the_page(self):
        # Aperta dal telefono con l'indirizzo della rete locale: vale quello, senza prove
        with mock.patch("SP.simulation._answers") as answers:
            response = self.client.get(self.url, HTTP_HOST="192.168.1.62:8000")
        answers.assert_not_called()
        self.assertEqual(response.context["server_address"], {"url": "http://192.168.1.62:8000", "reachable": True})
        with mock.patch("SP.simulation._answers", return_value=True):
            response = self.client.get(self.url, HTTP_HOST="localhost:8000")
        self.assertEqual(response.context["server_address"]["url"], "http://192.168.1.60:8000")

    def test_the_check_asks_the_site(self):
        from SP.simulation import _answers
        page = mock.MagicMock(status=200)
        page.read.return_value = b"<title>Accedi \xc2\xb7 Solar Family</title>"
        page.__enter__.return_value = page
        with mock.patch("SP.simulation.urllib.request.urlopen", return_value=page) as urlopen:
            self.assertTrue(_answers("http://192.168.1.62:8000"))
        self.assertEqual(urlopen.call_args.args[0], "http://192.168.1.62:8000/sp/login/")
        with mock.patch("SP.simulation.urllib.request.urlopen", side_effect=OSError("timed out")):
            self.assertFalse(_answers("http://192.168.1.60:8000"))
