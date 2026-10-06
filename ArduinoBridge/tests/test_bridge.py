"""Test del bridge, senza broker né porta seriale. Da ArduinoBridge: python -m unittest discover -s tests -t ."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import protocol
from bridge import Bridge, MinuteAverager, parse_args
from simulated_node import CLEANING_SECONDS, SimulatedNode

SYSTEM_ID = 42
CONFIG = {"system_id": SYSTEM_ID, "name": "Magazzino", "max_power_kw": 6.5, "community": "Rossi Trasporti",
          "city": "Modena (MO)", "owner": "Mario Rossi"}


def status(code, changed_at="2026-10-06T08:00:00+00:00"):
    return {"system_id": SYSTEM_ID, "status": code, "label": code, "previous": "OK", "changed_at": changed_at}


def message(name, payload):
    return SimpleNamespace(topic=f"solarfamily/systems/{SYSTEM_ID}/{name}", payload=json.dumps(payload).encode())


class FakeClient:
    """Registra quello che il bridge pubblica e a cosa si iscrive."""

    def __init__(self):
        self.published = []
        self.subscribed = []
        self.will = None

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def subscribe(self, topics):
        self.subscribed += topics

    def will_set(self, topic, payload, qos, retain):
        self.will = (topic, payload, qos, retain)

    def max_queued_messages_set(self, count):
        pass

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def payloads(self, name):
        return [json.loads(payload) for topic, payload, _, _ in self.published if topic.endswith(f"/{name}")]


class FakeNode:
    def __init__(self, lines=()):
        self.lines = list(lines)
        self.written = []

    def readline(self):
        return self.lines.pop(0) if self.lines else None

    def write_line(self, line):
        self.written.append(line)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


class ProtocolTests(unittest.TestCase):
    def test_lines_from_arduino(self):
        self.assertEqual(protocol.parse_line("D|23.40|812.0|3420.5\r\n"),
                         (protocol.DATA, protocol.Reading(23.4, 812.0, 3420.5)))
        self.assertEqual(protocol.parse_line("READY"), (protocol.READY, None))
        self.assertEqual(protocol.parse_line("ID|SolarNode|1\r\n"),
                         (protocol.IDENTIFY, protocol.Identity("SolarNode", 1)))
        self.assertEqual(protocol.parse_line("PUMP|ON"), (protocol.PUMP, True))
        self.assertEqual(protocol.parse_line("PUMP|OFF"), (protocol.PUMP, False))
        self.assertEqual(protocol.parse_line("ACK|CFG|6500"), (protocol.ACK, ["CFG", "6500"]))
        # Valori fuori scala (Arduino stampa nan, inf, ovf) e righe del vecchio formato
        for line in ("D|nan|812.0|3420.5", "D|23.4|inf|3420.5", "D|23.4|ovf|1", "23.4|812.0|3420.5", "D|1|2"):
            with self.subTest(line=line):
                self.assertEqual(protocol.parse_line(line)[0], protocol.UNKNOWN)

    def test_commands_to_arduino(self):
        self.assertEqual(protocol.config_command(6.5), "CFG|6500")
        self.assertEqual(protocol.status_command("DRT", True), "STATUS|DRT|1")
        self.assertEqual(protocol.parse_command("ID"), (protocol.IDENTIFY, ()))
        self.assertEqual(protocol.identity_line(), "ID|SolarNode|1")
        self.assertTrue(protocol.is_compatible(protocol.Identity("SolarNode", 1)))
        self.assertEqual(protocol.parse_command("CFG|6500"), (protocol.CONFIG, (6500,)))
        self.assertEqual(protocol.parse_command("STATUS|FLT|0"), (protocol.STATUS, ("FLT", False)))
        self.assertIsNone(protocol.parse_command("STATUS|XYZ|1"))
        self.assertIsNone(protocol.parse_command("CFG|-5"))


class MinuteAveragerTests(unittest.TestCase):
    def test_average_of_each_minute_in_kw_with_utc_time(self):
        averager = MinuteAverager()
        start = datetime(2026, 10, 6, 10, 41, 2, tzinfo=timezone.utc)
        for seconds, power in ((0, 3000.0), (20, 3500.0), (40, 4000.0)):
            self.assertIsNone(averager.add(protocol.Reading(20.0, 800.0, power), start + timedelta(seconds=seconds)))
        self.assertIsNone(averager.pop_completed(start + timedelta(seconds=50)))

        completed = averager.pop_completed(start + timedelta(seconds=60))

        self.assertEqual(completed, {"time_stamp": "2026-10-06T10:41:00+00:00", "temperature": 20.0,
                                     "lightness": 800.0, "power": 3.5, "samples": 3})
        self.assertIsNone(averager.pop_completed(start + timedelta(minutes=5)))

    def test_reading_of_a_new_minute_closes_the_previous_one(self):
        averager = MinuteAverager()
        start = datetime(2026, 10, 6, 10, 41, 59, tzinfo=timezone.utc)
        averager.add(protocol.Reading(20.0, 800.0, 1000.0), start)

        completed = averager.add(protocol.Reading(20.0, 800.0, 2000.0), start + timedelta(seconds=2))

        self.assertEqual((completed["power"], completed["samples"]), (1.0, 1))
        self.assertEqual(averager.readings, [protocol.Reading(20.0, 800.0, 2000.0)])


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "pv-42.json"
        self.clock = Clock(datetime(2026, 10, 6, 10, 41, 0, tzinfo=timezone.utc))

    def bridge(self, node=None):
        bridge = Bridge(node or FakeNode(), FakeClient(), f"pv-{SYSTEM_ID}", self.state_path, clock=self.clock)
        bridge.setup_client()
        return bridge


class BridgeMqttTests(BridgeTestCase):
    def test_on_connect_subscribes_to_its_topics_and_says_it_is_online(self):
        bridge = self.bridge()
        bridge.on_connect(bridge.client, None, None, SimpleNamespace(is_failure=False), None)

        self.assertEqual(bridge.client.subscribed, [("solarfamily/systems/42/config", 1),
                                                    ("solarfamily/systems/42/status", 1)])
        self.assertIn(("solarfamily/systems/42/connection", "online", 1, True), bridge.client.published)
        # Last Will: "offline" retained se il bridge cade senza salutare
        self.assertEqual(bridge.client.will, ("solarfamily/systems/42/connection", "offline", 1, True))

    def test_refused_connection_does_not_subscribe(self):
        bridge = self.bridge()
        with self.assertLogs("bridge", "ERROR"):
            bridge.on_connect(bridge.client, None, None, SimpleNamespace(is_failure=True), None)
        self.assertEqual(bridge.client.subscribed, [])

    def test_config_and_status_wait_for_arduino(self):
        node = FakeNode()
        bridge = self.bridge(node)
        bridge.on_message(None, None, message("config", CONFIG))
        bridge.on_message(None, None, message("status", status("DRT")))
        self.assertEqual(node.written, [])

        # Arduino si fa vivo: riceve potenza massima e stato, e con il nuovo DRT avvia il lavaggio
        node.lines.append("READY")
        bridge.step()

        self.assertEqual(node.written, ["CFG|6500", "STATUS|DRT|1"])

    def test_each_status_change_starts_the_cleaning_only_once(self):
        node = FakeNode(["READY"])
        bridge = self.bridge(node)
        bridge.step()
        bridge.on_message(None, None, message("status", status("DRT")))
        # Lo stesso messaggio di nuovo (QoS 1 o retained alla riconnessione)
        bridge.on_message(None, None, message("status", status("DRT")))
        self.assertEqual(node.written, ["STATUS|DRT|1", "STATUS|DRT|0"])

        # Dopo un riavvio del bridge il cambiamento è già stato gestito (salvato su file)
        node = FakeNode(["READY"])
        restarted = self.bridge(node)
        restarted.on_message(None, None, message("status", status("DRT")))
        restarted.step()
        self.assertEqual(node.written, ["STATUS|DRT|0"])

        # Un nuovo cambiamento, anche verso lo stesso stato, avvia di nuovo il lavaggio
        restarted.on_message(None, None, message("status", status("DRT", "2026-10-08T08:00:00+00:00")))
        self.assertEqual(node.written[-1], "STATUS|DRT|1")

    def test_arduino_restarted_gets_config_and_status_again(self):
        node = FakeNode(["D|20|800|1000"])
        bridge = self.bridge(node)
        bridge.on_message(None, None, message("config", CONFIG))
        bridge.on_message(None, None, message("status", status("DRT")))
        bridge.step()
        node.written.clear()

        node.lines.append("READY")
        bridge.step()

        self.assertEqual(node.written, ["CFG|6500", "STATUS|DRT|0"])

    def test_new_power_from_the_server_is_sent_to_arduino(self):
        node = FakeNode(["READY"])
        bridge = self.bridge(node)
        bridge.step()

        bridge.on_message(None, None, message("config", {**CONFIG, "max_power_kw": 8.0}))

        self.assertEqual(node.written[-1], "CFG|8000")

    def test_removed_retained_message_is_ignored(self):
        bridge = self.bridge()
        with self.assertLogs("bridge", "WARNING"):
            bridge.on_message(None, None, SimpleNamespace(topic="solarfamily/systems/42/status", payload=b""))
        self.assertIsNone(bridge.status)


class BridgeArduinoTests(BridgeTestCase):
    def test_readings_are_published_once_a_minute(self):
        node = FakeNode(["D|20.0|800.0|3000.0", "D|22.0|900.0|4000.0"])
        bridge = self.bridge(node)
        bridge.step()
        self.clock.now += timedelta(seconds=30)
        bridge.step()
        self.assertEqual(bridge.client.payloads("telemetry"), [])

        self.clock.now += timedelta(seconds=30)
        bridge.step()

        [reading] = bridge.client.payloads("telemetry")
        self.assertEqual(reading, {"time_stamp": "2026-10-06T10:41:00+00:00", "temperature": 21.0,
                                   "lightness": 850.0, "power": 3.5, "samples": 2})

    def test_pump_events_are_published(self):
        bridge = self.bridge(FakeNode(["PUMP|ON", "PUMP|OFF"]))
        bridge.step()
        bridge.step()

        events = bridge.client.payloads("events")
        self.assertEqual([event["event"] for event in events], ["pump_on", "pump_off"])
        self.assertEqual(events[0]["at"], "2026-10-06T10:41:00+00:00")

    def test_unknown_lines_are_logged(self):
        bridge = self.bridge(FakeNode(["Avvio..."]))
        with self.assertLogs("bridge", "WARNING"):
            bridge.step()


class SimulatedNodeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        noon = datetime(2026, 6, 21, 12, 0)
        self.node = SimulatedNode(clock=lambda: self.now, local_time=lambda: noon, sleep=lambda seconds: None)

    def lines(self):
        lines = []
        while (line := self.node.readline()) is not None:
            lines.append(line)
        return lines

    def test_asks_for_the_configuration_then_sends_readings(self):
        self.assertEqual(self.lines(), ["READY"])
        self.node.write_line("ID")
        self.assertEqual(self.lines(), ["ID|SolarNode|1"])

        self.node.write_line("CFG|6500")
        lines = self.lines()

        self.assertEqual(lines[0], "ACK|CFG|6500")
        kind, reading = protocol.parse_line(lines[1])
        self.assertEqual(kind, protocol.DATA)
        self.assertTrue(3000 < reading.power_w <= 6500 * 1.1)

    def test_dirty_panels_start_a_10_seconds_cleaning(self):
        self.node.write_line("CFG|6500")
        self.lines()

        self.node.write_line("STATUS|DRT|1")
        self.assertIn("PUMP|ON", self.lines())
        self.now += CLEANING_SECONDS - 1
        self.assertNotIn("PUMP|OFF", self.lines())
        self.now += 1
        self.assertIn("PUMP|OFF", self.lines())

    def test_no_cleaning_when_the_status_is_only_aligned_and_ok_stops_the_pump(self):
        self.node.write_line("STATUS|DRT|0")
        self.assertNotIn("PUMP|ON", self.lines())

        self.node.write_line("STATUS|DRT|1")
        self.node.write_line("STATUS|OK|1")
        self.assertEqual([line for line in self.lines() if line.startswith("PUMP")], ["PUMP|ON", "PUMP|OFF"])


class EndToEndTests(BridgeTestCase):
    def test_bridge_with_simulated_arduino(self):
        now = [0.0]
        node = SimulatedNode(clock=lambda: now[0], local_time=lambda: datetime(2026, 6, 21, 12, 0),
                             sleep=lambda seconds: None)
        bridge = self.bridge(node)
        bridge.on_message(None, None, message("config", CONFIG))
        bridge.on_message(None, None, message("status", status("DRT")))

        # Un minuto e mezzo: una misura ogni 1,5 secondi
        for _ in range(60):
            for _ in range(5):
                bridge.step()
            now[0] += 1.5
            self.clock.now += timedelta(seconds=1.5)

        [reading] = bridge.client.payloads("telemetry")
        self.assertEqual(reading["samples"], 40)
        self.assertTrue(0 < reading["power"] <= 6.5 * 1.1)
        self.assertEqual([event["event"] for event in bridge.client.payloads("events")], ["pump_on", "pump_off"])


class ArgumentsTests(unittest.TestCase):
    def setUp(self):
        # Le variabili d'ambiente del bridge cambierebbero i valori predefiniti
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("SOLAR_DEVICE", "SOLAR_DEVICE_TOKEN", "SOLAR_SERIAL_PORT"):
            os.environ.pop(name, None)

    def test_device_and_token_are_required(self):
        # argparse stampa l'errore ed esce: lo si nasconde
        for argv in (["--device", "pv-42"], ["--device", "mario", "--token", "x"],
                     ["--device", "pv-42", "--token", "abc", "--simulate", "--serial", "COM2"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parse_args(argv)
        self.assertTrue(parse_args(["--device", "pv-42", "--token", "abc", "--simulate"]).simulate)

    def test_serial_port_is_found_automatically_unless_given(self):
        self.assertEqual(parse_args(["--device", "pv-42", "--token", "abc"]).serial, "auto")
        self.assertEqual(parse_args(["--device", "pv-42", "--token", "abc", "--serial", "COM2"]).serial, "COM2")
        # Per elencare le porte non servono le credenziali
        self.assertTrue(parse_args(["--list-ports"]).list_ports)


if __name__ == "__main__":
    unittest.main()
