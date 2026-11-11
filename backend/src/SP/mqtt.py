"""Comunicazione MQTT tra il server e i dispositivi (Arduino e bridge) installati sugli impianti.

Ogni impianto ha i suoi topic sotto solarfamily/systems/<id>/:

    telemetry   dispositivo -> server   misura media di un minuto (JSON)
    connection  dispositivo -> server   "online" / "offline" (retained, "offline" è il Last Will del bridge)
    events      dispositivo -> server   eventi della pompa di lavaggio (JSON)
    status      server -> dispositivo   stato attuale dell'impianto e ultimo cambiamento (JSON, retained)
    config      server -> dispositivo   dati dell'impianto, come la potenza massima (JSON, retained)

Il master di una community (Gateway), a cui i nodi sui tetti parlano via LoRa, ha i suoi topic sotto
solarfamily/gateways/<id della community>/ e pubblica per gli impianti dei suoi nodi sui loro topic:

    connection  master -> server    "online" / "offline" (retained, "offline" è il Last Will)
    events      master -> server    nodi entrati nella rete o usciti (JSON)
    config      server -> master    nodi della community, con chiave e impianto (JSON, retained)
    commands    server -> master    aggiungi nodi, identifica, rimuovi (JSON)

Il broker (Mosquitto con il plugin mosquitto-go-auth) chiede al server chi può collegarsi e a quali topic
può accedere (SP/mqtt_auth.py): un dispositivo solo ai topic del proprio impianto, un master ai propri e a
quelli degli impianti dei suoi nodi, il server a tutti.
"""
import hmac
import json
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Device, Gateway, LoRaNode, PhotovoltaicSystem
from .serializers import PanelDataSerializer

logger = logging.getLogger(__name__)

TOPIC_ROOT = "solarfamily/systems"
GATEWAY_ROOT = "solarfamily/gateways"

TELEMETRY = "telemetry"
CONNECTION = "connection"
EVENTS = "events"
STATUS = "status"
CONFIG = "config"
COMMANDS = "commands"

# Cosa può fare un dispositivo sui topic del proprio impianto
DEVICE_PUBLISHES = {TELEMETRY, CONNECTION, EVENTS}
DEVICE_RECEIVES = {STATUS, CONFIG}
GATEWAY_PUBLISHES = {CONNECTION, EVENTS}
GATEWAY_RECEIVES = {CONFIG, COMMANDS}

# Tipo di accesso chiesto dal broker per un topic (campo "acc" di mosquitto-go-auth)
ACC_READ = 1
ACC_WRITE = 2
ACC_SUBSCRIBE = 4

# QoS 1: il messaggio viene ripetuto finché il broker non conferma di averlo ricevuto
QOS = 1


def topic(system_id, name):
    return f"{TOPIC_ROOT}/{system_id}/{name}"


def gateway_topic(community_id, name):
    return f"{GATEWAY_ROOT}/{community_id}/{name}"


def _parse(value, root):
    parts = value.split("/")
    if len(parts) != 4 or "/".join(parts[:2]) != root or not parts[2].isdigit():
        return None
    return int(parts[2]), parts[3]


def parse_topic(value):
    """(id dell'impianto, nome) di un topic come solarfamily/systems/42/telemetry, o None."""
    return _parse(value, TOPIC_ROOT)


def parse_gateway_topic(value):
    """(id della community, nome) di un topic come solarfamily/gateways/7/events, o None."""
    return _parse(value, GATEWAY_ROOT)


# --- Accesso al broker (usato da SP/mqtt_auth.py) ---

def is_server(username):
    return username == settings.MQTT_USERNAME


def authenticate(username, password):
    """True se username e password sono quelli del server o di un dispositivo installato."""
    if is_server(username):
        # Senza password configurata il server non può collegarsi (mai accettare una password vuota)
        return bool(settings.MQTT_PASSWORD) and hmac.compare_digest(str(password).encode(), settings.MQTT_PASSWORD.encode())
    community_id = Gateway.community_id_from_username(username)
    if community_id is not None:
        gateway = Gateway.objects.filter(community_id=community_id).first()
        return gateway is not None and gateway.check_token(password)
    system_id = Device.system_id_from_username(username)
    device = Device.objects.filter(system_id=system_id).first() if system_id else None
    # I dispositivi dei nodi LoRa non hanno token: non si collegano, pubblica per loro il master
    return device is not None and bool(device.token_hash) and device.check_token(password)


def may_access(username, topic_name, acc):
    """True se il client può accedere al topic: il server a tutti, un dispositivo solo a quelli del suo impianto."""
    if is_server(username):
        return True
    community_id = Gateway.community_id_from_username(username)
    if community_id is not None:
        return _gateway_may_access(community_id, topic_name, acc)
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


def _gateway_may_access(community_id, topic_name, acc):
    """Il master accede ai propri topic e, come un dispositivo, a quelli degli impianti dei suoi nodi."""
    if not Gateway.objects.filter(community_id=community_id).exists():
        return False
    own = parse_gateway_topic(topic_name)
    if own is not None:
        if own[0] != community_id:
            return False
        publishes, receives = GATEWAY_PUBLISHES, GATEWAY_RECEIVES
        name = own[1]
    else:
        parsed = parse_topic(topic_name)
        if parsed is None or not LoRaNode.objects.filter(
                system_id=parsed[0], gateway__community_id=community_id).exists():
            return False
        publishes, receives = DEVICE_PUBLISHES, DEVICE_RECEIVES
        name = parsed[1]
    if acc == ACC_WRITE:
        return name in publishes
    if acc in (ACC_READ, ACC_SUBSCRIBE):
        return name in receives
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


def gateway_config_payload(gateway):
    """Nodi della community per il master: con la chiave, anche dopo un riavvio o con un master nuovo."""
    nodes = gateway.nodes.select_related("system").order_by("node_id")
    return {
        "community_id": gateway.community_id,
        "community": gateway.community.name,
        "nodes": [
            {"id": node.node_id, "key": node.key, "system_id": node.system_id,
             "system": node.system.name if node.system else None}
            for node in nodes
        ],
    }


def gateway_config_messages(gateways):
    return [
        {"topic": gateway_topic(gateway.community_id, CONFIG), "payload": json.dumps(gateway_config_payload(gateway)),
         "qos": QOS, "retain": True}
        for gateway in gateways
    ]


def publish_gateway_config(gateway):
    return publish(gateway_config_messages([gateway]))


def publish_command(gateway, command, **fields):
    """Comando al master (non retained: se è scollegato non lo riceve, e le pagine non lo propongono)."""
    payload = json.dumps({"command": command, **fields})
    return publish([{"topic": gateway_topic(gateway.community_id, COMMANDS), "payload": payload, "qos": QOS,
                     "retain": False}])


def clear_gateway_retained(community_id):
    return publish([
        {"topic": gateway_topic(community_id, name), "payload": None, "qos": QOS, "retain": True}
        for name in (CONFIG, CONNECTION)
    ])


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
    handlers = {TELEMETRY: handle_telemetry, CONNECTION: handle_connection, EVENTS: handle_event}
    if parsed is None:
        parsed = parse_gateway_topic(topic_name)
        handlers = {CONNECTION: handle_gateway_connection, EVENTS: handle_gateway_event}
    if parsed is None:
        logger.warning("MQTT: topic sconosciuto %s", topic_name)
        return None
    target_id, name = parsed
    if name not in handlers:
        return None
    close_old_connections()
    try:
        return handlers[name](target_id, payload)
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


def _connection_state(payload):
    return payload.decode(errors="ignore").strip() if isinstance(payload, bytes) else str(payload or "").strip()


def handle_connection(system_id, payload):
    """Il bridge pubblica "online" quando si collega; il broker pubblica "offline" (Last Will) se cade.

    Per un nodo LoRa lo pubblica il master: "online" quando lo sente, "offline" se non lo sente più.
    """
    state = _connection_state(payload)
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


# --- Messaggi dei master delle community ---

# Il master chiude la finestra "Aggiungi nodi" da solo: un nodo che entra appena dopo, per i ritardi della
# rete, vale ancora
JOIN_GRACE = timedelta(seconds=30)
NODE_ID = re.compile(r"^[0-9A-F]{8}$")
NODE_KEY = re.compile(r"^[0-9a-f]{32}$")


def handle_gateway_connection(community_id, payload):
    """Come per un dispositivo; se il master cade, anche i suoi nodi non arrivano più al server."""
    state = _connection_state(payload)
    if state not in ("online", "offline"):
        return None
    now = timezone.now()
    Gateway.objects.filter(community_id=community_id).update(online=state == "online", last_seen=now)
    if state == "offline":
        Device.objects.filter(system__lora_node__gateway__community_id=community_id).update(online=False)
    return Gateway.objects.filter(community_id=community_id).first()


def _uninstall_node_device(node):
    """Il nodo non è più il sensore del suo impianto: il dispositivo dell'impianto viene disinstallato."""
    if node.system_id is None:
        return
    system_id = node.system_id
    Device.objects.filter(system_id=system_id).delete()
    clear_retained(system_id)


def handle_gateway_event(community_id, payload):
    """Nodi entrati nella rete del master (node_joined, solo con la finestra aperta) o usciti (node_left).

    Un nodo che rientra, anche dopo un reset, torna da assegnare: per il master è un dispositivo nuovo.
    """
    data = _json_object(payload, gateway_topic(community_id, EVENTS))
    gateway = Gateway.objects.select_related("community").filter(community_id=community_id).first()
    if data is None or gateway is None:
        return None
    event, node_id = data.get("event"), str(data.get("node", "")).upper()
    if not NODE_ID.match(node_id):
        logger.warning("MQTT: master %s, nodo non valido %r", gateway, data.get("node"))
        return None
    now = timezone.now()
    if event == "node_joined":
        key = str(data.get("key", "")).lower()
        if not NODE_KEY.match(key):
            logger.warning("MQTT: master %s, chiave del nodo %s non valida", gateway, node_id)
            return None
        if gateway.join_until is None or now > gateway.join_until + JOIN_GRACE:
            logger.warning("MQTT: master %s, nodo %s entrato con la finestra chiusa: ignorato", gateway, node_id)
            return None
        previous = LoRaNode.objects.filter(node_id=node_id).first()
        if previous is not None:
            _uninstall_node_device(previous)
        node, _ = LoRaNode.objects.update_or_create(node_id=node_id, defaults={
            "gateway": gateway, "key": key, "kind": str(data.get("kind") or "roof")[:20], "system": None,
            "joined_at": now, "rssi": _number(data.get("rssi")), "snr": _number(data.get("snr")),
        })
        Gateway.objects.filter(pk=gateway.pk).update(online=True, last_seen=now)
        publish_gateway_config(gateway)
        logger.info("MQTT: master %s, nodo %s entrato nella rete", gateway, node_id)
        return node
    if event == "node_left":
        node = LoRaNode.objects.filter(node_id=node_id, gateway=gateway).first()
        if node is None:
            return None
        _uninstall_node_device(node)
        node.delete()
        publish_gateway_config(gateway)
        logger.info("MQTT: master %s, nodo %s uscito dalla rete", gateway, node_id)
        return node
    logger.warning("MQTT: evento sconosciuto del master %s: %r", gateway, event)
    return None


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def installed_gateways():
    return Gateway.objects.select_related("community")
