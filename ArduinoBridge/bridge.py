"""Bridge tra Arduino (porta seriale) e il broker MQTT, per un impianto fotovoltaico.

Il bridge si collega al broker con le credenziali del dispositivo dell'impianto (pagina Installazioni o comando
device_credentials del server) e da quelle sa quale impianto è:

- riceve dal topic config i dati dell'impianto e invia ad Arduino la potenza massima;
- fa la media delle misure di Arduino su ogni minuto e la pubblica sul topic telemetry;
- riceve dal topic status lo stato dell'impianto e lo inoltra ad Arduino, che con DRT aziona la pompa;
- pubblica sul topic events l'accensione e lo spegnimento della pompa.

Avvio (il token si può passare anche con la variabile d'ambiente SOLAR_DEVICE_TOKEN):

    python ArduinoBridge/bridge.py --device pv-42 --token <token>              # cerca la porta di Arduino
    python ArduinoBridge/bridge.py --device pv-42 --token <token> --serial COM2
    python ArduinoBridge/bridge.py --device pv-42 --token <token> --simulate   # Arduino simulato
    python ArduinoBridge/bridge.py --list-ports                                # porte seriali e chi risponde
"""
import argparse
import json
import logging
import os
import signal
import statistics
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    from paho.mqtt import client as mqtt
except ImportError:
    sys.exit("Manca la libreria paho-mqtt: installa i requisiti del bridge con\n"
             "    pip install -r ArduinoBridge/requirements.txt")

import protocol
import serial_ports

logger = logging.getLogger("bridge")

TOPIC_ROOT = "solarfamily/systems"
# QoS 1: il messaggio viene ripetuto finché il broker non conferma di averlo ricevuto
QOS = 1
KEEPALIVE_SECONDS = 60
# Misure tenute in memoria mentre il broker non è raggiungibile: un giorno, una al minuto
MAX_QUEUED_MESSAGES = 24 * 60
STATE_DIR = Path(__file__).resolve().with_name(".state")


def system_id_from_username(username):
    number = username.removeprefix("pv-") if username.startswith("pv-") else ""
    if not number.isdigit():
        raise ValueError(f"Username del dispositivo non valido: {username!r} (atteso pv-<id dell'impianto>)")
    return int(number)


def utc_now():
    return datetime.now(timezone.utc)


class MinuteAverager:
    """Raccoglie le misure di Arduino e restituisce la media di ogni minuto quando il minuto è finito."""

    def __init__(self):
        self.minute = None
        self.readings = []

    def add(self, reading, now):
        """Aggiunge una misura; restituisce la media del minuto precedente se questa ne apre uno nuovo."""
        completed = self.pop_completed(now)
        if self.minute is None:
            self.minute = now.replace(second=0, microsecond=0)
        self.readings.append(reading)
        return completed

    def pop_completed(self, now):
        """Media del minuto in corso se è finito, altrimenti None."""
        if self.minute is None or now.replace(second=0, microsecond=0) == self.minute:
            return None
        readings, minute = self.readings, self.minute
        self.minute, self.readings = None, []
        return {
            # Ora UTC del minuto, con il fuso orario: il server non deve indovinarlo
            "time_stamp": minute.isoformat(),
            "temperature": round(statistics.mean(r.temperature for r in readings), 2),
            "lightness": round(statistics.mean(r.lux for r in readings), 1),
            # Arduino misura in W, il server salva in kW
            "power": round(statistics.mean(r.power_w for r in readings) / 1000, 4),
            "samples": len(readings),
        }


class Bridge:
    """Collega un nodo (Arduino sulla seriale o simulato) al broker MQTT come il dispositivo di un impianto.

    Le funzioni on_* di MQTT girano nel thread di paho, il resto nel thread principale: lo stato condiviso
    e le scritture verso Arduino sono protetti da un lock.
    """

    def __init__(self, node, client, username, state_path=None, clock=utc_now):
        self.node = node
        self.client = client
        self.username = username
        self.system_id = system_id_from_username(username)
        self.state_path = state_path
        self.clock = clock
        self.averager = MinuteAverager()
        self.lock = threading.Lock()
        self.config = None
        self.status = None
        # Ultimo cambiamento di stato già inoltrato ad Arduino (da un file: vale anche dopo un riavvio del bridge)
        self.handled_change = self.load_handled_change()
        self.pending_change = None
        # Arduino ha risposto almeno una volta: solo allora gli si inviano configurazione e stato
        self.node_ready = False

    def topic(self, name):
        return f"{TOPIC_ROOT}/{self.system_id}/{name}"

    # --- MQTT ---

    def setup_client(self):
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        # Last Will: se il bridge sparisce senza salutare, il broker pubblica "offline" al posto suo
        self.client.will_set(self.topic("connection"), "offline", qos=QOS, retain=True)
        self.client.max_queued_messages_set(MAX_QUEUED_MESSAGES)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            logger.error(
                "Il broker ha rifiutato la connessione (%s): controlla username e token del dispositivo, "
                "che sia ancora installato e che il server sia acceso. Nuovo tentativo tra poco.", reason_code,
            )
            return
        logger.info("Collegato al broker come %s", self.username)
        # Retained: il broker invia subito l'ultima configurazione e l'ultimo stato pubblicati dal server
        client.subscribe([(self.topic("config"), QOS), (self.topic("status"), QOS)])
        client.publish(self.topic("connection"), "online", qos=QOS, retain=True)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            logger.warning("Scollegato dal broker (%s): riprovo...", reason_code)

    def on_message(self, client, userdata, message):
        if not message.payload:
            # Messaggio retained cancellato: il dispositivo è stato disinstallato
            logger.warning("Nessun dato su %s: il dispositivo è ancora installato?", message.topic)
            return
        try:
            data = json.loads(message.payload)
        except ValueError:
            logger.warning("Messaggio non valido su %s", message.topic)
            return
        if message.topic == self.topic("config"):
            self.on_config(data)
        elif message.topic == self.topic("status"):
            self.on_status(data)

    def on_config(self, data):
        with self.lock:
            self.config = data
            owner = f", titolare {data['owner']}" if data.get("owner") else ""
            logger.info(
                "Impianto «%s» (id %s) di %s, %s%s: potenza massima %s kW",
                data.get("name"), data.get("system_id"), data.get("community"), data.get("city"), owner,
                data.get("max_power_kw"),
            )
            if self.node_ready:
                self.send(protocol.config_command(data["max_power_kw"]))

    def on_status(self, data):
        with self.lock:
            self.status = data
            change = data.get("changed_at") or "initial"
            if change != self.handled_change:
                self.pending_change = change
            logger.info("Stato dell'impianto: %s (%s)%s", data.get("label"), data.get("status"),
                        ", nuovo cambiamento" if self.pending_change else "")
            if self.node_ready:
                self.send_status()

    def publish_json(self, name, payload):
        self.client.publish(self.topic(name), json.dumps(payload), qos=QOS)

    # --- Arduino ---

    def send(self, line):
        logger.debug("-> Arduino: %s", line)
        self.node.write_line(line)

    def send_status(self):
        """Inoltra lo stato ad Arduino; segnala il cambiamento una volta sola (con DRT parte il lavaggio)."""
        new = self.pending_change is not None
        self.send(protocol.status_command(self.status["status"], new))
        if new:
            self.handled_change, self.pending_change = self.pending_change, None
            self.save_handled_change()

    def sync_node(self):
        """Invia ad Arduino configurazione e stato attuali (all'avvio o dopo un suo riavvio)."""
        if self.config:
            self.send(protocol.config_command(self.config["max_power_kw"]))
        if self.status:
            self.send_status()

    def on_line(self, line):
        kind, value = protocol.parse_line(line)
        with self.lock:
            if kind == protocol.READY or not self.node_ready:
                # READY: Arduino si è appena acceso e non ha la configurazione
                self.node_ready = True
                self.sync_node()
        if kind == protocol.DATA:
            completed = self.averager.add(value, self.clock())
            if completed:
                self.publish_reading(completed)
        elif kind == protocol.PUMP:
            event = "pump_on" if value else "pump_off"
            logger.info("Pompa di lavaggio %s", "accesa" if value else "spenta")
            self.publish_json("events", {"event": event, "at": self.clock().isoformat()})
        elif kind == protocol.ACK:
            logger.info("Arduino configurato: %s", "|".join(value))
        elif kind == protocol.UNKNOWN:
            logger.warning("Riga non riconosciuta da Arduino: %r", value)

    def publish_reading(self, reading):
        self.publish_json("telemetry", reading)
        logger.info("Misura del minuto %s: %.3f kW (%d campioni)", reading["time_stamp"], reading["power"],
                    reading["samples"])

    def step(self):
        """Un giro del ciclo principale: legge una riga da Arduino e pubblica il minuto appena finito."""
        line = self.node.readline()
        if line:
            self.on_line(line)
        completed = self.averager.pop_completed(self.clock())
        if completed:
            self.publish_reading(completed)

    # --- Stato salvato su file ---

    def load_handled_change(self):
        try:
            return json.loads(self.state_path.read_text())["handled_change"]
        except (AttributeError, OSError, ValueError, KeyError):
            return None

    def save_handled_change(self):
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"handled_change": self.handled_change}))
        except OSError as error:
            logger.warning("Impossibile salvare lo stato del bridge: %s", error)


def print_ports():
    """--list-ports: le porte seriali del computer e, per quelle che possono essere Arduino, chi risponde."""
    ports = serial_ports.list_ports()
    print(f"Porte seriali ({serial_ports.platform_name()}):")
    for port in ports:
        description = f", {port.description}" if port.description else ""
        if port.kind in serial_ports.CANDIDATE_KINDS:
            node, outcome = serial_ports.probe(port)
            if node is not None:
                node.close()
        else:
            outcome = "non interrogata (non può essere Arduino)"
        print(f"  {port.device} ({port.kind}{description}): {outcome}")
    if not serial_ports.candidates(ports):
        print(serial_ports.help_text())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Bridge tra Arduino e il broker MQTT di Solar Family.")
    parser.add_argument("--device", default=os.environ.get("SOLAR_DEVICE"), help="Username del dispositivo, come pv-42")
    parser.add_argument("--token", default=os.environ.get("SOLAR_DEVICE_TOKEN"), help="Token del dispositivo")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--serial", default=os.environ.get("SOLAR_SERIAL_PORT", "auto"),
                        help="Porta seriale di Arduino, come COM2 o /dev/cu.usbmodem1101; con auto (default) "
                             "il bridge usa la prima porta a cui risponde un SolarNode")
    source.add_argument("--simulate", action="store_true", help="Usa un Arduino simulato invece della porta seriale")
    source.add_argument("--list-ports", action="store_true",
                        help="Elenca le porte seriali e dice su quali risponde un SolarNode, poi esce")
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    parser.add_argument("--verbose", action="store_true", help="Mostra anche le righe scambiate con Arduino")
    args = parser.parse_args(argv)
    if args.list_ports:
        return args
    if not args.device or not args.token:
        parser.error("servono --device e --token (o SOLAR_DEVICE e SOLAR_DEVICE_TOKEN): "
                     "le dà la pagina Installazioni del sito o il comando device_credentials")
    try:
        system_id_from_username(args.device)
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.list_ports:
        print_ports()
        return 0
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("paho").setLevel(logging.WARNING)

    if args.simulate:
        from simulated_node import SimulatedNode

        node = SimulatedNode()
        logger.info("Arduino simulato")
    else:
        if args.serial == "auto":
            logger.info("Ricerca di Arduino sulle porte seriali (%s)...", serial_ports.platform_name())
        try:
            node, port = serial_ports.connect(args.serial, log=logger.info)
        except serial_ports.NodeNotFound as error:
            logger.error("%s", error)
            return 1
        logger.info("Arduino SolarNode sulla porta %s a %d baud", port.device, serial_ports.BAUD_RATE)

    # Client id uguale allo username e sessione persistente: dopo una disconnessione il broker
    # ricorda le iscrizioni del bridge e gli consegna i messaggi QoS 1 arrivati nel frattempo
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.device, clean_session=False)
    client.username_pw_set(args.device, args.token)
    bridge = Bridge(node, client, args.device, state_path=STATE_DIR / f"{args.device}.json")
    bridge.setup_client()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    logger.info("Collegamento al broker %s:%d...", args.mqtt_host, args.mqtt_port)
    client.connect_async(args.mqtt_host, args.mqtt_port, keepalive=KEEPALIVE_SECONDS)
    client.loop_start()
    try:
        while not stop.is_set():
            bridge.step()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Chiusura del bridge...")
        # Uscita pulita: il Last Will non scatta, quindi "offline" lo pubblica il bridge
        if client.is_connected():
            client.publish(bridge.topic("connection"), "offline", qos=QOS, retain=True).wait_for_publish(timeout=2)
        client.disconnect()
        client.loop_stop()
        node.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
