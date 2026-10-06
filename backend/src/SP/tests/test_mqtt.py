import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from SP import mqtt
from SP.models import Device, PanelData, PhotovoltaicSystem
from SP.tests.helpers import create_community, create_customer, create_system, install_device, utc

SERVER = "solarfamily-server"


def telemetry(**fields):
    return json.dumps({"time_stamp": "2026-10-06T10:41:00+00:00", "temperature": 21.5, "lightness": 812.0,
                       "power": 3.42, "samples": 40, **fields}).encode()


class TopicTests(TestCase):
    def test_parse_topic(self):
        self.assertEqual(mqtt.parse_topic("solarfamily/systems/42/telemetry"), (42, "telemetry"))
        for topic in ("solarfamily/systems/+/telemetry", "solarfamily/systems/42", "other/systems/42/status",
                      "solarfamily/systems/42/status/extra", "solarfamily/systems/#"):
            with self.subTest(topic=topic):
                self.assertIsNone(mqtt.parse_topic(topic))


@override_settings(MQTT_USERNAME=SERVER, MQTT_PASSWORD="server-secret")
class BrokerAuthTests(TestCase):
    """Le richieste che mosquitto-go-auth invia a Django: 200 autorizzato, 403 rifiutato."""

    def setUp(self):
        community = create_community()
        self.system = create_system(community)
        self.other = create_system(community, "Other")
        self.device, self.token = install_device(self.system)
        self.username = self.device.username

    def post(self, endpoint, **data):
        return self.client.post(f"/sp/mqtt/auth/{endpoint}/", json.dumps(data), content_type="application/json").status_code

    def login(self, username, password, clientid=None):
        return self.post("user", username=username, password=password, clientid=clientid or username)

    def acl(self, topic, acc, username=None):
        username = username or self.username
        return self.post("acl", username=username, clientid=username, topic=topic, acc=acc)

    def test_device_logs_in_with_its_token_and_its_username_as_client_id(self):
        self.assertEqual(self.login(self.username, self.token), 200)
        self.assertEqual(self.login(self.username, "wrong"), 403)
        self.assertEqual(self.login(self.username, self.token, clientid="someone-else"), 403)
        # Un token vale solo per il suo impianto
        self.assertEqual(self.login(f"pv-{self.other.id}", self.token), 403)
        self.assertEqual(self.login("mario", self.token), 403)

    def test_regenerated_or_uninstalled_device_cannot_log_in(self):
        new_token = self.device.new_token()
        self.device.save()
        self.assertEqual(self.login(self.username, self.token), 403)
        self.assertEqual(self.login(self.username, new_token), 200)

        self.device.delete()
        self.assertEqual(self.login(self.username, new_token), 403)
        # Se è ancora collegato non può più pubblicare
        self.assertEqual(self.acl(f"solarfamily/systems/{self.system.id}/telemetry", mqtt.ACC_WRITE), 403)

    def test_server_logs_in_with_its_password_and_is_superuser(self):
        self.assertEqual(self.login(SERVER, "server-secret", clientid="auto-123"), 200)
        self.assertEqual(self.login(SERVER, "wrong"), 403)
        self.assertEqual(self.post("superuser", username=SERVER), 200)
        self.assertEqual(self.post("superuser", username=self.username), 403)
        self.assertEqual(self.acl("solarfamily/systems/+/telemetry", mqtt.ACC_SUBSCRIBE, username=SERVER), 200)

    @override_settings(MQTT_PASSWORD="")
    def test_server_without_configured_password_cannot_log_in(self):
        self.assertEqual(self.login(SERVER, ""), 403)

    def test_device_publishes_and_receives_only_on_the_topics_of_its_system(self):
        own = f"solarfamily/systems/{self.system.id}"
        for name in ("telemetry", "connection", "events"):
            self.assertEqual(self.acl(f"{own}/{name}", mqtt.ACC_WRITE), 200, name)
        for name in ("status", "config"):
            self.assertEqual(self.acl(f"{own}/{name}", mqtt.ACC_SUBSCRIBE), 200, name)
            self.assertEqual(self.acl(f"{own}/{name}", mqtt.ACC_READ), 200, name)

        # Non può fingersi il server, né leggere le misure, né accedere ad altri impianti
        self.assertEqual(self.acl(f"{own}/status", mqtt.ACC_WRITE), 403)
        self.assertEqual(self.acl(f"{own}/telemetry", mqtt.ACC_SUBSCRIBE), 403)
        self.assertEqual(self.acl(f"solarfamily/systems/{self.other.id}/telemetry", mqtt.ACC_WRITE), 403)
        self.assertEqual(self.acl("solarfamily/systems/+/status", mqtt.ACC_SUBSCRIBE), 403)
        self.assertEqual(self.acl("#", mqtt.ACC_SUBSCRIBE), 403)
        self.assertEqual(self.acl(f"{own}/telemetry", "write"), 403)

    def test_only_post_is_accepted(self):
        self.assertEqual(self.client.get("/sp/mqtt/auth/user/").status_code, 405)
        self.assertEqual(self.client.post("/sp/mqtt/auth/user/", "not json", content_type="application/json").status_code, 403)


class HandleMessageTests(TestCase):
    def setUp(self):
        self.system = create_system(create_community())
        self.device, _ = install_device(self.system)
        self.topic = f"solarfamily/systems/{self.system.id}"

    def test_telemetry_is_saved_and_the_device_is_online(self):
        reading = mqtt.handle_message(f"{self.topic}/telemetry", telemetry())

        self.assertEqual(reading.system, self.system)
        self.assertEqual(reading.time_stamp, utc(2026, 10, 6, 10, 41))
        self.assertEqual((reading.power, reading.temperature, reading.lightness), (3.42, 21.5, 812.0))
        self.device.refresh_from_db()
        self.assertTrue(self.device.online)
        self.assertLess(timezone.now() - self.device.last_seen, timedelta(seconds=5))

    def test_time_zone_of_the_bridge_is_kept(self):
        reading = mqtt.handle_message(f"{self.topic}/telemetry", telemetry(time_stamp="2026-10-06T12:41:00+02:00"))

        self.assertEqual(reading.time_stamp, utc(2026, 10, 6, 10, 41))

    def test_repeated_reading_is_discarded(self):
        # QoS 1: il broker può consegnare due volte la stessa misura
        mqtt.handle_message(f"{self.topic}/telemetry", telemetry())
        with self.assertLogs("SP.mqtt", "INFO") as logs:
            self.assertIsNone(mqtt.handle_message(f"{self.topic}/telemetry", telemetry(power=9.0)))

        self.assertEqual(PanelData.objects.get().power, 3.42)
        self.assertIn("ripetuta", logs.output[0])

    def test_invalid_payloads_are_discarded(self):
        with self.assertLogs("SP.mqtt", "WARNING"):
            for payload in (b"not json", b"[1, 2]", telemetry(power="tanta"), json.dumps({"power": 1}).encode()):
                self.assertIsNone(mqtt.handle_message(f"{self.topic}/telemetry", payload))
            # Impianto che non esiste
            self.assertIsNone(mqtt.handle_message("solarfamily/systems/999/telemetry", telemetry()))
        self.assertFalse(PanelData.objects.exists())

    def test_connection_and_last_will(self):
        mqtt.handle_message(f"{self.topic}/connection", b"online")
        self.device.refresh_from_db()
        self.assertTrue(self.device.online)

        mqtt.handle_message(f"{self.topic}/connection", b"offline")
        self.device.refresh_from_db()
        self.assertFalse(self.device.online)

        # Messaggio retained cancellato: nessun cambiamento
        self.assertIsNone(mqtt.handle_message(f"{self.topic}/connection", b""))

    def test_pump_events(self):
        with self.assertLogs("SP.mqtt", "INFO"):
            mqtt.handle_message(f"{self.topic}/events", json.dumps({"event": "pump_on"}).encode())
        self.device.refresh_from_db()
        self.assertTrue(self.device.pump_running)

        with self.assertLogs("SP.mqtt", "INFO"):
            mqtt.handle_message(f"{self.topic}/events",
                                json.dumps({"event": "pump_off", "at": "2026-10-06T10:42:10+00:00"}).encode())
        self.device.refresh_from_db()
        self.assertFalse(self.device.pump_running)
        self.assertEqual(self.device.last_cleaning, utc(2026, 10, 6, 10, 42, 10))

    def test_messages_of_the_server_are_ignored(self):
        self.assertIsNone(mqtt.handle_message(f"{self.topic}/status", b"{}"))


@override_settings(MQTT_ENABLED=True, MQTT_HOST="broker", MQTT_PORT=1883, MQTT_USERNAME=SERVER, MQTT_PASSWORD="pw")
class PublishTests(TestCase):
    def setUp(self):
        community = create_community("Rossi Trasporti")
        create_customer(community)  # il primo utente è il titolare
        self.system = create_system(community, "Magazzino", max_power=6.5)
        self.not_installed = create_system(community, "Tettoia")
        install_device(self.system)

    def test_state_is_published_retained_only_for_installed_systems(self):
        with mock.patch("paho.mqtt.publish.multiple") as multiple:
            self.assertTrue(mqtt.publish_state([self.system, self.not_installed]))

        messages = multiple.call_args.args[0]
        self.assertEqual(multiple.call_args.kwargs["hostname"], "broker")
        self.assertEqual(multiple.call_args.kwargs["auth"], {"username": SERVER, "password": "pw"})
        self.assertEqual([m["topic"] for m in messages], [
            f"solarfamily/systems/{self.system.id}/config", f"solarfamily/systems/{self.system.id}/status",
        ])
        self.assertTrue(all(m["retain"] and m["qos"] == 1 for m in messages))
        config = json.loads(messages[0]["payload"])
        self.assertEqual(config["max_power_kw"], 6.5)
        self.assertEqual(config["community"], "Rossi Trasporti")
        self.assertEqual(config["owner"], "Mario Rossi")
        status = json.loads(messages[1]["payload"])
        self.assertEqual((status["status"], status["label"], status["changed_at"]), ("OK", "OK", None))

    def test_unreachable_broker_does_not_raise(self):
        with mock.patch("paho.mqtt.publish.multiple", side_effect=ConnectionRefusedError("refused")), \
                self.assertLogs("SP.mqtt", "WARNING"):
            self.assertFalse(mqtt.publish_state([self.system]))

    @override_settings(MQTT_ENABLED=False)
    def test_disabled_publishing(self):
        with mock.patch("paho.mqtt.publish.multiple") as multiple:
            self.assertFalse(mqtt.publish_state([self.system]))
        multiple.assert_not_called()

    def test_clear_retained(self):
        with mock.patch("paho.mqtt.publish.multiple") as multiple:
            mqtt.clear_retained(self.system.id)

        messages = multiple.call_args.args[0]
        self.assertEqual({m["topic"].rsplit("/", 1)[1] for m in messages}, {"status", "config", "connection"})
        self.assertTrue(all(m["payload"] is None and m["retain"] for m in messages))


class SetStatusTests(TestCase):
    def test_change_date_and_previous_status_only_when_the_status_changes(self):
        system = create_system(create_community())
        first, second = utc(2026, 10, 1, 8), utc(2026, 10, 3, 8)

        self.assertTrue(system.set_status(PhotovoltaicSystem.Status.DIRTY, first))
        self.assertFalse(system.set_status(PhotovoltaicSystem.Status.DIRTY, second))

        system.refresh_from_db()
        self.assertEqual((system.status, system.previous_status), ("DRT", "OK"))
        self.assertEqual(system.status_changed_at, first)
        self.assertEqual(system.last_check, second)


class DeviceTests(TestCase):
    def test_username_and_token(self):
        device, token = install_device(create_system(create_community()))

        self.assertEqual(Device.system_id_from_username(device.username), device.system_id)
        self.assertEqual(len(token), 40)
        self.assertNotIn(token, device.token_hash)
        self.assertTrue(device.check_token(token))
        self.assertFalse(device.check_token(token[:-1] + "x"))
        for username in ("pv-", "pv-abc", "solarfamily-server", "42"):
            self.assertIsNone(Device.system_id_from_username(username))
