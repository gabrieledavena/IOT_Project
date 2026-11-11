"""Master delle community e nodi LoRa (SP/gateways.py, SP/mqtt.py, SP/pairing.py)."""
import json
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase
from django.utils import timezone

from SP import mqtt
from SP.gateways import assign_node, install_gateway
from SP.installations import install_device as install_with_token
from SP.models import Device, DevicePairing, Gateway, LoRaNode
from SP.tests.helpers import create_community, create_customer, create_system, create_technician, install_device

HOST = "192.168.1.10:8000"
NODE = "A3F2C1D0"
KEY = "00112233445566778899aabbccddeeff"


def published(publish):
    """Messaggi pubblicati, nell'ordine: (topic, payload decodificato)."""
    result = []
    for call in publish.call_args_list:
        for message in call.args[0]:
            payload = message["payload"]
            result.append((message["topic"], json.loads(payload) if payload else payload))
    return result


class GatewayTestCase(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.system = create_system(self.community, "Magazzino")
        self.other_system = create_system(self.community, "Sede")
        self.staff = create_technician()
        self.client.force_login(self.staff)

    def page_url(self, community=None):
        return f"/sp/installations/communities/{(community or self.community).id}/"

    def install(self, online=True):
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            gateway, token = install_gateway(self.community, self.staff)
        Gateway.objects.filter(pk=gateway.pk).update(online=online)
        gateway.refresh_from_db()
        return gateway, token

    def event(self, gateway, **data):
        with mock.patch("SP.mqtt.publish") as publish:
            result = mqtt.handle_message(mqtt.gateway_topic(gateway.community_id, mqtt.EVENTS), json.dumps(data).encode())
        self.published = published(publish)
        return result

    def join(self, gateway, node=NODE, key=KEY):
        Gateway.objects.filter(pk=gateway.pk).update(join_until=timezone.now() + timedelta(minutes=3))
        return self.event(gateway, event="node_joined", node=node, key=key, kind="roof", rssi=-48.0, snr=9.5)

    def action(self, action, **data):
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.page_url(), {"action": action, **data}, follow=True)
        self.published = published(publish)
        return response


class GatewayPairingTests(GatewayTestCase):
    def test_the_master_is_installed_on_a_community_with_the_qr(self):
        device = Client(HTTP_HOST=HOST)
        pairing = device.post("/sp/devices/pairing/", {"role": "gateway"}).json()
        self.assertEqual(DevicePairing.objects.get().role, DevicePairing.ROLE_GATEWAY)

        # La pagina del QR propone le community, non gli impianti
        response = self.client.get(f"/sp/pair/{pairing['code']}/")
        self.assertContains(response, "Installa il master")
        self.assertContains(response, "Rossi Trasporti")
        self.assertNotContains(response, "Magazzino")

        response = self.client.post(f"/sp/pair/{pairing['code']}/", {"community": self.community.id}, follow=True)
        self.assertRedirects(response, self.page_url())
        self.assertContains(response, "In attesa del master")

        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            data = device.post("/sp/devices/pairing/credentials/", {"code": pairing["code"], "secret": pairing["secret"]}).json()
        self.assertEqual(data["role"], "gateway")
        self.assertEqual(data["username"], f"gw-{self.community.id}")
        self.assertEqual((data["community_id"], data["community"]), (self.community.id, "Rossi Trasporti"))
        self.assertTrue(mqtt.authenticate(data["username"], data["token"]))
        self.assertEqual(Gateway.objects.get().installed_by, self.staff)
        # La configurazione dei nodi (ancora nessuno), retained
        topic, payload = published(publish)[0]
        self.assertEqual(topic, mqtt.gateway_topic(self.community.id, mqtt.CONFIG))
        self.assertEqual(payload["nodes"], [])

    def test_invalid_role(self):
        self.assertEqual(Client().post("/sp/devices/pairing/", {"role": "admin"}).status_code, 400)

    def test_codes_go_to_their_page(self):
        device = Client(HTTP_HOST=HOST)
        master = device.post("/sp/devices/pairing/", {"role": "gateway"}).json()
        sensor = device.post("/sp/devices/pairing/").json()
        response = self.client.post(f"/sp/installations/{self.system.id}/pair/", {"code": master["code"]}, follow=True)
        self.assertContains(response, "È il codice di un master")
        response = self.client.post(f"{self.page_url()}pair/", {"code": sensor["code"]}, follow=True)
        self.assertContains(response, "È il codice di un dispositivo")
        response = self.client.post(f"{self.page_url()}pair/", {"code": master["code"]}, follow=True)
        self.assertContains(response, "In attesa del master")

    def test_a_new_master_keeps_the_nodes(self):
        gateway, old_token = self.install()
        self.join(gateway)
        with mock.patch("SP.mqtt.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            _, token = install_gateway(self.community, self.staff)
        self.assertFalse(mqtt.authenticate(gateway.username, old_token))
        self.assertTrue(mqtt.authenticate(gateway.username, token))
        self.assertEqual(published(publish)[0][1]["nodes"][0]["key"], KEY)


class BrokerAccessTests(GatewayTestCase):
    def test_topics_of_the_master(self):
        gateway, _ = self.install()
        username, cid = gateway.username, self.community.id
        for name in (mqtt.CONNECTION, mqtt.EVENTS):
            self.assertTrue(mqtt.may_access(username, mqtt.gateway_topic(cid, name), mqtt.ACC_WRITE))
        for name in (mqtt.CONFIG, mqtt.COMMANDS):
            self.assertTrue(mqtt.may_access(username, mqtt.gateway_topic(cid, name), mqtt.ACC_SUBSCRIBE))
            self.assertFalse(mqtt.may_access(username, mqtt.gateway_topic(cid, name), mqtt.ACC_WRITE))
        other = create_community("Bianchi Srl")
        self.assertFalse(mqtt.may_access(username, mqtt.gateway_topic(other.id, mqtt.EVENTS), mqtt.ACC_WRITE))

    def test_topics_of_the_systems_of_its_nodes(self):
        gateway, _ = self.install()
        node = self.join(gateway)
        username = gateway.username
        telemetry = mqtt.topic(self.system.id, mqtt.TELEMETRY)
        # Nodo da assegnare: nessun impianto
        self.assertFalse(mqtt.may_access(username, telemetry, mqtt.ACC_WRITE))
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            assign_node(node, self.system, self.staff)
        for name in (mqtt.TELEMETRY, mqtt.CONNECTION, mqtt.EVENTS):
            self.assertTrue(mqtt.may_access(username, mqtt.topic(self.system.id, name), mqtt.ACC_WRITE))
        self.assertTrue(mqtt.may_access(username, mqtt.topic(self.system.id, mqtt.STATUS), mqtt.ACC_SUBSCRIBE))
        self.assertFalse(mqtt.may_access(username, mqtt.topic(self.system.id, mqtt.STATUS), mqtt.ACC_WRITE))
        self.assertFalse(mqtt.may_access(username, mqtt.topic(self.other_system.id, mqtt.TELEMETRY), mqtt.ACC_WRITE))
        # Il master di un'altra community no
        other = create_community("Bianchi Srl")
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            other_gateway, _ = install_gateway(other)
        self.assertFalse(mqtt.may_access(other_gateway.username, telemetry, mqtt.ACC_WRITE))

    def test_the_device_of_a_node_cannot_connect(self):
        gateway, _ = self.install()
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            assign_node(self.join(gateway), self.system, self.staff)
        device = Device.objects.get(system=self.system)
        self.assertEqual(device.token_hash, "")
        for password in ("", "x"):
            self.assertFalse(mqtt.authenticate(device.username, password))


class GatewayMessageTests(GatewayTestCase):
    def test_a_node_joins_only_while_the_window_is_open(self):
        gateway, _ = self.install()
        self.assertIsNone(self.event(gateway, event="node_joined", node=NODE, key=KEY))
        self.assertFalse(LoRaNode.objects.exists())

        node = self.join(gateway)
        self.assertEqual((node.node_id, node.key, node.kind, node.rssi), (NODE, KEY, "roof", -48.0))
        self.assertIsNone(node.system)
        topic, payload = self.published[0]
        self.assertEqual(topic, mqtt.gateway_topic(self.community.id, mqtt.CONFIG))
        self.assertEqual(payload["nodes"], [{"id": NODE, "key": KEY, "system_id": None, "system": None}])

        # Appena chiusa vale ancora (ritardi della rete), dopo no
        Gateway.objects.filter(pk=gateway.pk).update(join_until=timezone.now() - timedelta(seconds=10))
        self.assertIsNotNone(self.event(gateway, event="node_joined", node="B0000001", key=KEY))
        Gateway.objects.filter(pk=gateway.pk).update(join_until=timezone.now() - timedelta(minutes=2))
        self.assertIsNone(self.event(gateway, event="node_joined", node="B0000002", key=KEY))

    def test_invalid_events(self):
        gateway, _ = self.install()
        Gateway.objects.filter(pk=gateway.pk).update(join_until=timezone.now() + timedelta(minutes=3))
        for data in ({"event": "node_joined", "node": "xyz", "key": KEY}, {"event": "node_joined", "node": NODE, "key": "abc"},
                     {"event": "reboot", "node": NODE}):
            self.assertIsNone(self.event(gateway, **data))
        self.assertIsNone(mqtt.handle_message(mqtt.gateway_topic(999, mqtt.EVENTS), b'{"event": "node_left"}'))
        self.assertFalse(LoRaNode.objects.exists())

    def test_a_node_that_joins_again_is_new(self):
        gateway, _ = self.install()
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            assign_node(self.join(gateway), self.system, self.staff)
        node = self.join(gateway, key="ff" * 16)
        self.assertEqual(node.key, "ff" * 16)
        self.assertIsNone(node.system)
        self.assertFalse(Device.objects.filter(system=self.system).exists())

    def test_a_node_leaves(self):
        gateway, _ = self.install()
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            assign_node(self.join(gateway), self.system, self.staff)
        self.event(gateway, event="node_left", node=NODE)
        self.assertFalse(LoRaNode.objects.exists())
        self.assertFalse(Device.objects.filter(system=self.system).exists())
        self.assertIn((mqtt.topic(self.system.id, mqtt.STATUS), None), self.published)

    def test_when_the_master_goes_offline_its_nodes_do_too(self):
        gateway, _ = self.install()
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            assign_node(self.join(gateway), self.system, self.staff)
        # Il master pubblica "online" per il nodo che sente
        mqtt.handle_message(mqtt.topic(self.system.id, mqtt.CONNECTION), b"online")
        self.assertTrue(Device.objects.get(system=self.system).online)
        mqtt.handle_message(mqtt.gateway_topic(self.community.id, mqtt.CONNECTION), b"offline")
        self.assertFalse(Gateway.objects.get().online)
        self.assertFalse(Device.objects.get(system=self.system).online)
        mqtt.handle_message(mqtt.gateway_topic(self.community.id, mqtt.CONNECTION), b"online")
        self.assertTrue(Gateway.objects.get().online)


class CommunityPageTests(GatewayTestCase):
    def test_technicians_only(self):
        for url in ("/sp/installations/communities/", self.page_url(), f"{self.page_url()}pair/"):
            self.client.force_login(create_customer(self.community, username=f"cliente{len(url)}"))
            self.assertEqual(self.client.get(url).status_code if not url.endswith("pair/") else
                             self.client.post(url, {"code": "X"}).status_code, 403)

    def test_list_of_the_communities(self):
        self.install()
        create_community("Bianchi Srl")
        response = self.client.get("/sp/installations/communities/")
        self.assertContains(response, "Rossi Trasporti")
        self.assertContains(response, "Collegato")
        self.assertContains(response, "Non installato")
        self.assertContains(self.client.get("/sp/installations/"), "/sp/installations/communities/")

    def test_add_nodes(self):
        gateway, _ = self.install(online=False)
        response = self.action("permit_join")
        self.assertContains(response, "Il master è scollegato")
        self.assertEqual(self.published, [])

        Gateway.objects.filter(pk=gateway.pk).update(online=True)
        response = self.action("permit_join")
        self.assertContains(response, "accetta nodi nuovi per 3 minuti")
        self.assertContains(response, "data-countdown")
        self.assertEqual(self.published, [(mqtt.gateway_topic(self.community.id, mqtt.COMMANDS),
                                           {"command": "permit_join", "seconds": 180})])
        self.assertGreater(Gateway.objects.get().join_seconds_left, 170)

    def test_identify_and_assign_a_node(self):
        gateway, _ = self.install()
        self.join(gateway)
        response = self.client.get(self.page_url())
        self.assertContains(response, "Da assegnare")
        self.assertContains(response, NODE)

        self.action("identify", node=NODE)
        self.assertEqual(self.published, [(mqtt.gateway_topic(self.community.id, mqtt.COMMANDS),
                                           {"command": "identify", "node": NODE, "seconds": 10})])

        response = self.action("assign", node=NODE, system=self.system.id)
        self.assertContains(response, f"Il nodo {NODE} è il sensore di Magazzino")
        self.assertContains(response, "Sensori degli impianti")
        node = LoRaNode.objects.get()
        self.assertEqual(node.system, self.system)
        device = Device.objects.get(system=self.system)
        self.assertEqual(device.installed_by, self.staff)
        topics = [topic for topic, _ in self.published]
        self.assertIn(mqtt.gateway_topic(self.community.id, mqtt.CONFIG), topics)
        self.assertIn(mqtt.topic(self.system.id, mqtt.STATUS), topics)
        config = dict(self.published)[mqtt.gateway_topic(self.community.id, mqtt.CONFIG)]
        self.assertEqual(config["nodes"][0]["system_id"], self.system.id)
        # La pagina dell'impianto mostra il nodo
        self.assertContains(self.client.get(f"/sp/installations/{self.system.id}/"), f"nodo LoRa <code>{NODE}</code>")

    def test_a_node_replaces_the_sensor_of_the_system(self):
        gateway, _ = self.install()
        bridge, token = install_device(self.system)
        self.join(gateway)
        self.join(gateway, node="B0000001")
        self.action("assign", node="B0000001", system=self.system.id)
        self.assertFalse(mqtt.authenticate(bridge.username, token))
        self.action("assign", node=NODE, system=self.system.id)
        self.assertIsNone(LoRaNode.objects.get(node_id="B0000001").system)
        self.assertEqual(LoRaNode.objects.get(node_id=NODE).system, self.system)
        # Spostato su un altro impianto, il primo resta senza sensore
        self.action("assign", node=NODE, system=self.other_system.id)
        self.assertFalse(Device.objects.filter(system=self.system).exists())
        self.assertTrue(Device.objects.filter(system=self.other_system).exists())

    def test_only_systems_of_the_community(self):
        gateway, _ = self.install()
        self.join(gateway)
        elsewhere = create_system(create_community("Bianchi Srl"), "Altrove")
        for system in (elsewhere.id, "abc", ""):
            self.assertContains(self.action("assign", node=NODE, system=system), "Scegli un impianto della community")
        self.assertIsNone(LoRaNode.objects.get().system)

    def test_a_bridge_takes_the_place_of_the_node(self):
        gateway, _ = self.install()
        node = self.join(gateway)
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            assign_node(node, self.system, self.staff)
            _, token = install_with_token(self.system, self.staff)
        self.assertIsNone(LoRaNode.objects.get().system)
        self.assertTrue(mqtt.authenticate(f"pv-{self.system.id}", token))

    def test_remove_a_node(self):
        gateway, _ = self.install()
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            assign_node(self.join(gateway), self.system, self.staff)
        response = self.action("remove", node=NODE)
        self.assertContains(response, "torna come nuovo")
        self.assertFalse(LoRaNode.objects.exists())
        self.assertFalse(Device.objects.filter(system=self.system).exists())
        commands = mqtt.gateway_topic(self.community.id, mqtt.COMMANDS)
        config = mqtt.gateway_topic(self.community.id, mqtt.CONFIG)
        topics = [topic for topic, _ in self.published]
        # Prima il comando, poi la configurazione senza il nodo
        self.assertLess(topics.index(commands), topics.index(config))
        self.assertEqual(dict(self.published)[commands], {"command": "remove", "node": NODE})
        self.assertEqual(dict(self.published)[config]["nodes"], [])

    def test_unknown_node(self):
        self.install()
        self.assertContains(self.action("identify", node="FFFFFFFF"), "Nodo non trovato")

    def test_uninstall_the_master(self):
        gateway, token = self.install()
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            assign_node(self.join(gateway), self.system, self.staff)
        response = self.action("uninstall")
        self.assertContains(response, "Master disinstallato")
        self.assertFalse(Gateway.objects.exists())
        self.assertFalse(LoRaNode.objects.exists())
        self.assertFalse(Device.objects.filter(system=self.system).exists())
        self.assertFalse(mqtt.authenticate(gateway.username, token))
        self.assertIn((mqtt.gateway_topic(self.community.id, mqtt.CONFIG), None), self.published)
