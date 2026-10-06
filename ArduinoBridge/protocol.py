"""Protocollo seriale (UART, 9600 baud) tra Arduino e bridge: una riga di testo per messaggio, campi separati da |.

Arduino -> bridge
    ID|SolarNode|1        identità: tipo di dispositivo e versione del protocollo (risposta a ID)
    READY                 Arduino non ha ancora la configurazione (la chiede ogni 2 secondi)
    ACK|CFG|6000          configurazione ricevuta: potenza massima in W
    D|23.40|812.0|3420.5  misura: temperatura (°C), luce (lux), potenza stimata (W)
    PUMP|ON  /  PUMP|OFF  la pompa di lavaggio si è accesa / spenta

Bridge -> Arduino
    ID                    chi sei? Il bridge lo invia alle porte seriali per trovare quella con Arduino
    CFG|6000              potenza massima dell'impianto in W
    STATUS|DRT|1          stato dell'impianto (OK, DRT, FLT); 1 se è un nuovo cambiamento a cui reagire
                          (con DRT si accende la pompa), 0 se serve solo ad allineare Arduino (i LED)
"""
import math
from collections import namedtuple

SEPARATOR = "|"

IDENTIFY = "ID"
READY = "READY"
ACK = "ACK"
DATA = "D"
PUMP = "PUMP"
CONFIG = "CFG"
STATUS = "STATUS"
UNKNOWN = "UNKNOWN"

STATUSES = ("OK", "DRT", "FLT")

# Identità del nodo: il bridge parla solo con questo tipo di dispositivo e con questa versione del protocollo
DEVICE_TYPE = "SolarNode"
PROTOCOL_VERSION = 1

Reading = namedtuple("Reading", "temperature lux power_w")
Identity = namedtuple("Identity", "device_type version")


def parse_line(line):
    """(tipo, valore) di una riga inviata da Arduino; le righe non riconosciute sono (UNKNOWN, riga)."""
    parts = line.strip().split(SEPARATOR)
    kind = parts[0]
    if kind == IDENTIFY and len(parts) == 3 and parts[2].isdigit():
        return IDENTIFY, Identity(parts[1], int(parts[2]))
    if kind == READY and len(parts) == 1:
        return READY, None
    if kind == DATA and len(parts) == 4:
        try:
            values = [float(value) for value in parts[1:]]
        except ValueError:
            return UNKNOWN, line
        # Arduino stampa nan, inf o ovf se un calcolo va fuori scala: misura scartata
        return (DATA, Reading(*values)) if all(map(math.isfinite, values)) else (UNKNOWN, line)
    if kind == PUMP and len(parts) == 2 and parts[1] in ("ON", "OFF"):
        return PUMP, parts[1] == "ON"
    if kind == ACK and len(parts) >= 2:
        return ACK, parts[1:]
    return UNKNOWN, line


def is_compatible(identity):
    """True se il dispositivo è un SolarNode che parla la stessa versione del protocollo del bridge."""
    return identity == Identity(DEVICE_TYPE, PROTOCOL_VERSION)


def identify_command():
    return IDENTIFY


def identity_line():
    """Risposta di Arduino a ID."""
    return f"{IDENTIFY}{SEPARATOR}{DEVICE_TYPE}{SEPARATOR}{PROTOCOL_VERSION}"


def config_command(max_power_kw):
    """Riga CFG con la potenza massima in W (nel server è in kW)."""
    return f"{CONFIG}{SEPARATOR}{round(max_power_kw * 1000)}"


def status_command(status, new):
    return f"{STATUS}{SEPARATOR}{status}{SEPARATOR}{1 if new else 0}"


def parse_command(line):
    """(tipo, valori) di una riga inviata dal bridge, come la legge Arduino; None se non è valida."""
    parts = line.strip().split(SEPARATOR)
    if parts == [IDENTIFY]:
        return IDENTIFY, ()
    if parts[0] == CONFIG and len(parts) == 2 and parts[1].isdigit():
        return CONFIG, (int(parts[1]),)
    if parts[0] == STATUS and len(parts) == 3 and parts[1] in STATUSES and parts[2] in ("0", "1"):
        return STATUS, (parts[1], parts[2] == "1")
    return None
