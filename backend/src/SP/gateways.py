"""Master delle community e nodi LoRa sui tetti, che i tecnici installano dalla pagina della community.

Il master è l'unico dispositivo della community collegato a internet: si installa sulla community con il QR del
suo display (SP/pairing.py). I nodi sui tetti, il sensore di un impianto ciascuno, non hanno display né internet:
entrano nella rete del master come i dispositivi Zigbee in quella della loro centralina.

1. Il tecnico preme "Aggiungi nodi": il master accetta nodi nuovi per Gateway.JOIN_TIME (permit join).
2. Un nodo mai abbinato chiede di entrare via LoRa; master e nodo concordano una chiave (ECDH) e il master avvisa
   il server (node_joined, SP/mqtt.py). Il nodo compare tra quelli da assegnare.
3. Il tecnico lo assegna a un impianto della community del master, dopo averlo riconosciuto con "Identifica" (il LED
   del nodo lampeggia). L'impianto riceve un dispositivo senza token: le sue misure le pubblica il master.

La pagina con il master e i nodi è in SP/communities.py; il protocollo LoRa tra master e nodi è descritto in
ArduinoBridge/MasterNode/ProtocolloLora.h.
"""
from django.db import transaction
from django.utils import timezone

from . import mqtt
from .installations import detach_node, uninstall_device
from .models import Device, Gateway

IDENTIFY_SECONDS = 10


def install_gateway(community, staff=None):
    """Installa il master sulla community, o gli dà nuove credenziali se c'è già; restituisce master e token.

    I nodi restano: un master nuovo riceve le loro chiavi con la configurazione e non vanno riabbinati.
    """
    gateway = Gateway.objects.filter(community=community).first() or Gateway(community=community)
    token = gateway.new_token()
    if gateway.pk is None:
        gateway.installed_by = staff
    gateway.save()
    transaction.on_commit(lambda: mqtt.publish_gateway_config(gateway))
    return gateway, token


def uninstall_gateway(community):
    """Disinstalla il master e i suoi nodi: gli impianti dei nodi restano senza dispositivo."""
    gateway = Gateway.objects.filter(community=community).first()
    if gateway is None:
        return
    for node in gateway.nodes.filter(system__isnull=False):
        uninstall_device(node.system)
    gateway.delete()
    # L'id letto subito: se poi si elimina anche la community, al commit community.id è None
    community_id = community.id
    transaction.on_commit(lambda: mqtt.clear_gateway_retained(community_id))


def permit_join(gateway):
    """Apre la finestra "Aggiungi nodi": il master accetta nodi nuovi per Gateway.JOIN_TIME."""
    gateway.join_until = timezone.now() + Gateway.JOIN_TIME
    gateway.save(update_fields=["join_until"])
    seconds = int(Gateway.JOIN_TIME.total_seconds())
    transaction.on_commit(lambda: mqtt.publish_command(gateway, "permit_join", seconds=seconds))


def identify(node):
    """Il LED del nodo lampeggia veloce: così il tecnico capisce quale nodo è sul tetto."""
    transaction.on_commit(lambda: mqtt.publish_command(node.gateway, "identify", node=node.node_id,
                                                       seconds=IDENTIFY_SECONDS))


def assign_node(node, system, staff=None):
    """Il nodo diventa il sensore dell'impianto, che deve essere della community del suo master.

    Un impianto ha un solo sensore: un altro nodo dell'impianto torna da assegnare, e un dispositivo con le
    credenziali (un bridge) non può più collegarsi. Il nodo lascia l'impianto che aveva prima.
    """
    previous = node.system
    if previous == system:
        return
    detach_node(system)
    if previous is not None:
        uninstall_device(previous)
    device = Device.objects.filter(system=system).first() or Device(system=system, installed_by=staff)
    device.token_hash = ""   # Niente accesso diretto al broker: per il nodo pubblica il master
    device.online = False    # Lo segna collegato il master, appena sente il nodo
    device.save()
    node.system = system
    node.save(update_fields=["system"])
    gateway = node.gateway
    transaction.on_commit(lambda: (mqtt.publish_gateway_config(gateway), mqtt.publish_state([system])))


def remove_node(node):
    """Il master fa uscire il nodo dalla rete (il nodo torna come nuovo) e lo dimentica."""
    gateway, node_id = node.gateway, node.node_id
    # Prima il comando, poi i nodi senza di lui: il master conserva la chiave finché non l'ha fatto uscire
    transaction.on_commit(lambda: mqtt.publish_command(gateway, "remove", node=node_id))
    if node.system_id is not None:
        uninstall_device(node.system)
    node.delete()
    transaction.on_commit(lambda: mqtt.publish_gateway_config(gateway))


def node_online(node):
    """True se il master sente il nodo (il dispositivo del suo impianto è collegato)."""
    device = getattr(node.system, "device", None) if node.system_id else None
    return bool(device and device.online)
