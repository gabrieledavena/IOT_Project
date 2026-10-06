"""Comunicazione MQTT tra il server e i dispositivi (Arduino e bridge) installati sugli impianti.

Ogni impianto ha i suoi topic sotto solarfamily/systems/<id>/:

    telemetry   dispositivo -> server   misura media di un minuto (JSON)
    connection  dispositivo -> server   "online" / "offline" (retained, "offline" è il Last Will del bridge)
    events      dispositivo -> server   eventi della pompa di lavaggio (JSON)
    status      server -> dispositivo   stato attuale dell'impianto e ultimo cambiamento (JSON, retained)
    config      server -> dispositivo   dati dell'impianto, come la potenza massima (JSON, retained)

Il broker (Mosquitto con il plugin mosquitto-go-auth) chiede al server chi può collegarsi e a quali topic
può accedere (SP/mqtt_auth.py): un dispositivo solo ai topic del proprio impianto, il server a tutti.
"""
import hmac
import json
import logging

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Device, PhotovoltaicSystem
from .serializers import PanelDataSerializer

logger = logging.getLogger(__name__)

TOPIC_ROOT = "solarfamily/systems"

TELEMETRY = "telemetry"
CONNECTION = "connection"
EVENTS = "events"
STATUS = "status"
CONFIG = "config"

# Cosa può fare un dispositivo sui topic del proprio impianto
DEVICE_PUBLISHES = {TELEMETRY, CONNECTION, EVENTS}
DEVICE_RECEIVES = {STATUS, CONFIG}

# Tipo di accesso chiesto dal broker per un topic (campo "acc" di mosquitto-go-auth)
ACC_READ = 1
ACC_WRITE = 2
ACC_SUBSCRIBE = 4

# QoS 1: il messaggio viene ripetuto finché il broker non conferma di averlo ricevuto
QOS = 1


def topic(system_id, name):
    return f"{TOPIC_ROOT}/{system_id}/{name}"


def parse_topic(value):
    """(id dell'impianto, nome) di un topic come solarfamily/systems/42/telemetry, o None."""
    parts = value.split("/")
    if len(parts) != 4 or "/".join(parts[:2]) != TOPIC_ROOT or not parts[2].isdigit():
        return None
    return int(parts[2]), parts[3]


# --- Accesso al broker (usato da SP/mqtt_auth.py) ---

def is_server(username):
    return username == settings.MQTT_USERNAME


def authenticate(username, password):
    """True se username e password sono quelli del server o di un dispositivo installato."""
    if is_server(username):
        # Senza password configurata il server non può collegarsi (mai accettare una password vuota)
        return bool(settings.MQTT_PASSWORD) and hmac.compare_digest(str(password).encode(), settings.MQTT_PASSWORD.encode())
    system_id = Device.system_id_from_username(username)
    device = Device.objects.filter(system_id=system_id).first() if system_id else None
    return device is not None and device.check_token(password)


def may_access(username, topic_name, acc):
    """True se il client può accedere al topic: il server a tutti, un dispositivo solo a quelli del suo impianto."""
    if is_server(username):
        return True
    system_id = Device.system_id_from_username(username)
    parsed = parse_topic(topic_name)
    if system_id is None or parsed is None or parsed[0] != system_id:
        return False
    # Disinstallato: anche se è ancora collegato non può più pubblicare né ricevere nulla
    if not Device.objects.filter(system_id=system_id).exists():
        return False
    name = parsed[1]
    if acc == ACC_WRITE:
        return name in DEVICE_PUBLISHES
    if acc in (ACC_READ, ACC_SUBSCRIBE):
        return name in DEVICE_RECEIVES
    return False


# --- Messaggi del server verso i dispositivi ---

def _iso(value):
    return value.isoformat() if value else None


def status_payload(system):
    return {
        "system_id": system.id,
        "status": system.status,
        "label": system.get_status_display(),
        "previous": system.previous_status or None,
        # Il bridge reagisce una volta sola a ogni cambiamento: lo riconosce da questa data
        "changed_at": _iso(system.status_changed_at),
        "checked_at": _iso(system.last_check),
    }


def config_payload(system):
    community = system.community
    return {
        "system_id": system.id,
        "name": system.name,
        "max_power_kw": system.max_power,
        "community": community.name,
        "city": str(community.city),
        "owner": str(community.owner) if community.owner_id else None,
    }


def _message(system_id, name, payload):
    # Retained: il broker conserva l'ultimo messaggio e lo consegna subito a chi si iscrive
    return {"topic": topic(system_id, name), "payload": json.dumps(payload), "qos": QOS, "retain": True}


def status_messages(systems):
    return [_message(system.id, STATUS, status_payload(system)) for system in systems]


def state_messages(systems):
    """Configurazione e stato degli impianti, retained."""
    systems = list(systems)
    return [_message(system.id, CONFIG, config_payload(system)) for system in systems] + status_messages(systems)


def installed(systems):
    """Solo gli impianti con un dispositivo installato: agli altri non serve pubblicare nulla."""
    ids = set(Device.objects.filter(system__in=[system.id for system in systems]).values_list("system_id", flat=True))
    return [system for system in systems if system.id in ids]


def installed_systems():
    return PhotovoltaicSystem.objects.filter(device__isnull=False).select_related("community__city", "community__owner")


def publish_status(systems):
    """Pubblica lo stato degli impianti con un dispositivo (per esempio dopo un cambiamento)."""
    return publish(status_messages(installed(list(systems))))


def publish_state(systems):
    """Pubblica configurazione e stato degli impianti con un dispositivo (installazione, potenza modificata...)."""
    return publish(state_messages(installed(list(systems))))


def clear_retained(system_id):
    """Cancella i messaggi retained dell'impianto (dispositivo disinstallato): un payload vuoto retained li rimuove."""
    return publish([
        {"topic": topic(system_id, name), "payload": None, "qos": QOS, "retain": True}
        for name in (STATUS, CONFIG, CONNECTION)
    ])


def publish(messages):
    """Pubblica i messaggi con una sola connessione al broker; True se ci è riuscito.

    Se il broker non risponde non si blocca nulla: il worker ripubblica lo stato di tutti gli impianti
    ogni volta che si collega (vedi il comando mqtt_worker).
    """
    if not messages or not settings.MQTT_ENABLED:
        return False
    from paho.mqtt import publish as paho_publish

    try:
        paho_publish.multiple(
            messages,
            hostname=settings.MQTT_HOST,
            port=settings.MQTT_PORT,
            auth={"username": settings.MQTT_USERNAME, "password": settings.MQTT_PASSWORD},
        )
    except Exception as error:  # broker spento o irraggiungibile, credenziali errate
        logger.warning("MQTT: %d messaggi non pubblicati (%s)", len(messages), error)
        return False
    return True


# --- Messaggi dei dispositivi verso il server (ricevuti dal worker) ---

def handle_message(topic_name, payload):
    """Gestisce un messaggio ricevuto dal worker; restituisce l'oggetto salvato o aggiornato, se c'è."""
    parsed = parse_topic(topic_name)
    if parsed is None:
        logger.warning("MQTT: topic sconosciuto %s", topic_name)
        return None
    system_id, name = parsed
    handlers = {TELEMETRY: handle_telemetry, CONNECTION: handle_connection, EVENTS: handle_event}
    if name not in handlers:
        return None
    close_old_connections()
    try:
        return handlers[name](system_id, payload)
    except Exception:
        logger.exception("MQTT: errore nel messaggio su %s", topic_name)
        return None


def _json_object(payload, topic_name):
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict):
        logger.warning("MQTT: payload non valido su %s: %r", topic_name, payload[:200])
        return None
    return data


def handle_telemetry(system_id, payload):
    """Salva la misura di un minuto; le misure ripetute (QoS 1) vengono scartate."""
    data = _json_object(payload, topic(system_id, TELEMETRY))
    if data is None:
        return None
    serializer = PanelDataSerializer(data={**data, "system": system_id})
    if not serializer.is_valid():
        errors = serializer.errors
        if any(getattr(error, "code", None) == "unique" for error in errors.get("non_field_errors", [])):
            logger.info("MQTT: misura ripetuta dell'impianto %s scartata (%s)", system_id, data.get("time_stamp"))
        else:
            logger.warning("MQTT: misura dell'impianto %s non valida: %s", system_id, dict(errors))
        return None
    reading = serializer.save()
    Device.objects.filter(system_id=system_id).update(online=True, last_seen=timezone.now())
    return reading


def handle_connection(system_id, payload):
    """Il bridge pubblica "online" quando si collega; il broker pubblica "offline" (Last Will) se cade."""
    state = payload.decode(errors="ignore").strip() if isinstance(payload, bytes) else str(payload or "").strip()
    if state not in ("online", "offline"):
        # Payload vuoto: il messaggio retained è stato cancellato
        return None
    Device.objects.filter(system_id=system_id).update(online=state == "online", last_seen=timezone.now())
    return Device.objects.filter(system_id=system_id).first()


def handle_event(system_id, payload):
    """Eventi della pompa: pump_on all'inizio del lavaggio, pump_off alla fine."""
    data = _json_object(payload, topic(system_id, EVENTS))
    if data is None:
        return None
    now = timezone.now()
    at = parse_datetime(data["at"]) if isinstance(data.get("at"), str) else None
    event = data.get("event")
    if event == "pump_on":
        changes = {"pump_running": True}
    elif event == "pump_off":
        changes = {"pump_running": False, "last_cleaning": at or now}
    else:
        logger.warning("MQTT: evento sconosciuto dell'impianto %s: %r", system_id, event)
        return None
    Device.objects.filter(system_id=system_id).update(online=True, last_seen=now, **changes)
    logger.info("MQTT: impianto %s, %s", system_id, event)
    return Device.objects.filter(system_id=system_id).first()
