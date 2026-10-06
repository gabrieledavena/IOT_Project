"""Porte seriali: come trovare quella a cui è collegato Arduino (SolarNode) su Windows, macOS e Linux.

Il collegamento è lo stesso ovunque (UART a 9600 baud, 8N1, righe di testo); cambia il nome della porta:

- Windows: COM1, COM2, ... Una scheda USB riceve da Windows un numero COM (si vede in Gestione dispositivi).
  Con SimulIDE si usa una coppia virtuale di com0com: SimulIDE apre COM1, il bridge COM2, e quello che uno
  scrive l'altro lo legge. Una porta COM la può aprire un solo programma alla volta.
- macOS: file in /dev. Ogni porta compare due volte: /dev/tty.* aspetta il segnale di linea pronta, /dev/cu.*
  è quella da usare per collegarsi. Arduino originali: /dev/cu.usbmodem...; cloni con CH340:
  /dev/cu.usbserial-... o /dev/cu.wchusbserial... Le altre (Bluetooth, cuffie, debug-console) non sono Arduino.
  Non c'è com0com: la coppia virtuale per SimulIDE si crea con socat (vedi VIRTUAL_PORTS).
- Linux: /dev/ttyACM0 (Arduino originali), /dev/ttyUSB0 (cloni); coppie virtuali con socat.

Il nome non basta a riconoscere Arduino. In modalità automatica il bridge apre solo le porte che possono esserlo
(schede USB e porte virtuali), invia ID e aspetta ID|SolarNode|<versione del protocollo>: usa la prima che
risponde così. Le altre restano chiuse, comprese quelle Bluetooth, che aprendole proverebbero a collegarsi.
"""
import os
import re
import sys
import time
from collections import namedtuple

import protocol

BAUD_RATE = 9600
READ_TIMEOUT = 0.1
# Una scheda collegata via USB si riavvia quando si apre la porta: le serve circa un secondo per rispondere
IDENTIFY_TIMEOUT = 3.0
IDENTIFY_INTERVAL = 0.5

# Lato bridge della coppia virtuale creata con socat su macOS e Linux (SimulIDE apre l'altro lato):
#   socat -d -d pty,raw,echo=0,link=/tmp/solarnode-simulide pty,raw,echo=0,link=/tmp/solarnode-bridge
VIRTUAL_PORTS = ("/tmp/solarnode-bridge",)
SOCAT_COMMAND = ("socat -d -d pty,raw,echo=0,link=/tmp/solarnode-simulide "
                 "pty,raw,echo=0,link=/tmp/solarnode-bridge")

# Produttori (vendor id USB) delle schede Arduino originali; le altre schede USB sono cloni o adattatori
ARDUINO_VIDS = {0x2341, 0x2A03}

# Tipi di porta, nell'ordine in cui vengono provate
ARDUINO = "Arduino USB"
USB_SERIAL = "scheda USB-seriale"
VIRTUAL = "porta virtuale"
OTHER = "altra porta"
CANDIDATE_KINDS = (ARDUINO, USB_SERIAL, VIRTUAL)

Port = namedtuple("Port", "device kind description")


class NodeNotFound(Exception):
    """Nessuna porta con un SolarNode che risponde: il messaggio spiega cosa è stato provato e cosa fare."""


def platform_name(platform=sys.platform):
    return {"win32": "Windows", "darwin": "macOS"}.get(platform, "Linux")


def normalize(device, platform=sys.platform):
    """Su macOS /dev/tty.* aspetta un segnale che Arduino non manda: si usa la stessa porta come /dev/cu.*."""
    if platform == "darwin" and device.startswith("/dev/tty."):
        return "/dev/cu." + device.removeprefix("/dev/tty.")
    return device


def port_kind(info):
    """Tipo di una porta elencata da pyserial (serial.tools.list_ports)."""
    if info.vid in ARDUINO_VIDS:
        return ARDUINO
    if info.vid is not None:
        return USB_SERIAL
    # Coppie virtuali su Windows: com0com o altri emulatori ("Virtual Serial Port")
    text = f"{info.description} {info.hwid}".lower()
    if "com0com" in text or "virtual" in text:
        return VIRTUAL
    return OTHER


def natural_key(device):
    """COM2 prima di COM10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", device)]


def list_ports(comports=None, virtual_ports=VIRTUAL_PORTS, platform=sys.platform):
    """Tutte le porte seriali del computer, con il loro tipo: prima quelle che possono essere Arduino."""
    if comports is None:
        from serial.tools import list_ports as pyserial_list_ports

        comports = pyserial_list_ports.comports()
    ports, seen = [], set()
    for info in comports:
        device = normalize(info.device, platform)
        if device not in seen:
            seen.add(device)
            description = info.description if info.description not in (None, "n/a") else ""
            ports.append(Port(device, port_kind(info), description))
    if platform != "win32":
        for path in virtual_ports:
            if os.path.exists(path) and path not in seen:
                ports.append(Port(path, VIRTUAL, "coppia virtuale di socat"))
    order = {kind: rank for rank, kind in enumerate(CANDIDATE_KINDS + (OTHER,))}
    return sorted(ports, key=lambda port: (order[port.kind], natural_key(port.device)))


def candidates(ports):
    return [port for port in ports if port.kind in CANDIDATE_KINDS]


class SerialNode:
    """Arduino su una porta seriale; readline non blocca più di 0,1 secondi."""

    def __init__(self, port):
        self.serial = port
        self.buffer = b""

    @classmethod
    def open(cls, device):
        import serial

        # Su macOS e Linux più programmi possono aprire la stessa porta e rubarsi i dati a vicenda: il lock
        # esclusivo lo impedisce (su Windows una porta COM è sempre di un solo programma)
        options = {"exclusive": True} if os.name == "posix" else {}
        return cls(serial.Serial(device, BAUD_RATE, timeout=READ_TIMEOUT, **options))

    def readline(self):
        chunk = self.serial.readline()
        if not chunk:
            return None
        self.buffer += chunk
        if not self.buffer.endswith(b"\n"):
            # Riga arrivata solo in parte: il resto arriverà alla prossima lettura
            return None
        line, self.buffer = self.buffer, b""
        return line.decode("utf-8", errors="ignore").strip() or None

    def write_line(self, line):
        self.serial.write(f"{line}\n".encode())

    def close(self):
        if self.serial.is_open:
            self.serial.close()


def open_error(error, platform=sys.platform):
    """Perché una porta non si apre, detto in modo comprensibile."""
    text = str(error)
    # Una porta già aperta: su macOS e Linux c'è il lock del bridge, Windows risponde "accesso negato"
    if "exclusively lock" in text or (platform == "win32" and "PermissionError" in text):
        return "occupata da un altro programma (un altro bridge, SimulIDE o il monitor seriale di Arduino IDE)"
    return f"non si può aprire ({error})"


def identify(node, timeout=IDENTIFY_TIMEOUT, clock=time.monotonic):
    """Chiede al dispositivo chi è (ID), ripetendo la domanda finché non risponde o scade il tempo.

    Restituisce l'identità (o None) e l'ultima altra riga ricevuta, utile a capire cosa c'è sulla porta.
    """
    deadline, next_query, last_line = clock() + timeout, clock(), None
    while clock() < deadline:
        if clock() >= next_query:
            node.write_line(protocol.identify_command())
            next_query = clock() + IDENTIFY_INTERVAL
        line = node.readline()
        if not line:
            continue
        kind, value = protocol.parse_line(line)
        if kind == protocol.IDENTIFY:
            return value, last_line
        last_line = line
    return None, last_line


def probe(port, open_node=SerialNode.open, timeout=IDENTIFY_TIMEOUT, clock=time.monotonic):
    """Apre la porta e verifica che ci sia un SolarNode compatibile.

    Restituisce il nodo, lasciato aperto (riaprire la porta riavvierebbe una scheda USB), oppure None;
    in entrambi i casi anche la descrizione dell'esito.
    """
    try:
        node = open_node(port.device)
    except Exception as error:  # porta occupata da un altro programma, inesistente o senza permessi
        return None, open_error(error)
    try:
        identity, last_line = identify(node, timeout, clock)
    except Exception as error:  # dispositivo scollegato mentre lo si interroga
        node.close()
        return None, f"errore di comunicazione ({error})"
    if identity is not None and protocol.is_compatible(identity):
        return node, f"{identity.device_type}, protocollo {identity.version}"
    node.close()
    if identity is None and last_line is None:
        return None, "nessuna risposta"
    if identity is None:
        return None, f"risponde, ma non è un SolarNode aggiornato (ha inviato {last_line!r})"
    if identity.device_type != protocol.DEVICE_TYPE:
        return None, f"è un altro dispositivo ({identity.device_type})"
    return None, (f"SolarNode con il protocollo {identity.version}, mentre il bridge usa il "
                  f"{protocol.PROTOCOL_VERSION}: aggiorna lo sketch o il bridge")


def help_text(platform=sys.platform):
    """Cosa controllare se il bridge non trova Arduino."""
    simulide = ("avvia la simulazione in SimulIDE e controlla che sia caricato lo sketch SolarNode aggiornato "
                "(ArduinoBridge/SolarNode)")
    if platform == "win32":
        return (f"Collega Arduino via USB, oppure {simulide}, con la porta seriale del circuito su COM1 e la coppia "
                "com0com COM1-COM2 installata.")
    return (f"Collega Arduino via USB, oppure {simulide}, con la porta seriale del circuito su "
            f"/tmp/solarnode-simulide e la coppia virtuale creata con:\n    {SOCAT_COMMAND}")


def find_node(ports, open_node=SerialNode.open, timeout=IDENTIFY_TIMEOUT, clock=time.monotonic, log=print):
    """Prova le porte candidate nell'ordine e restituisce (nodo aperto, porta) della prima con un SolarNode."""
    tried = []
    for port in ports:
        node, outcome = probe(port, open_node, timeout, clock)
        log(f"Porta {port.device} ({port.kind}): {outcome}")
        if node is not None:
            return node, port
        tried.append(f"  {port.device}: {outcome}")
    if not tried:
        raise NodeNotFound(f"Nessuna porta seriale che possa essere Arduino. {help_text()}")
    raise NodeNotFound("Nessun SolarNode risponde sulle porte provate:\n" + "\n".join(tried) + "\n" + help_text())


def connect(device="auto", open_node=SerialNode.open, timeout=IDENTIFY_TIMEOUT, log=print):
    """Apre la porta di Arduino: quella indicata o, con "auto", la prima a cui risponde un SolarNode."""
    if device == "auto":
        return find_node(candidates(list_ports()), open_node, timeout, log=log)
    port = Port(normalize(device), "scelta con --serial", "")
    node, outcome = probe(port, open_node, timeout)
    if node is None:
        raise NodeNotFound(f"Non è possibile usare la porta {port.device}: {outcome}.\n{help_text()}")
    return node, port
