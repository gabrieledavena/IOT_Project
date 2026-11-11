"""Master delle community e nodi LoRa sui tetti, per i tecnici.

Il master è l'unico dispositivo della community collegato a internet: si installa sulla community con il QR del
suo display (SP/pairing.py). I nodi sui tetti, il sensore di un impianto ciascuno, non hanno display né internet:
entrano nella rete del master come i dispositivi Zigbee in quella della loro centralina.

1. Il tecnico preme "Aggiungi nodi": il master accetta nodi nuovi per Gateway.JOIN_TIME (permit join).
2. Un nodo mai abbinato chiede di entrare via LoRa; master e nodo concordano una chiave (ECDH) e il master avvisa
   il server (node_joined, SP/mqtt.py). Il nodo compare tra quelli da assegnare.
3. Il tecnico lo assegna a un impianto della community del master, dopo averlo riconosciuto con "Identifica" (il LED
   del nodo lampeggia). L'impianto riceve un dispositivo senza token: le sue misure le pubblica il master.

Il protocollo LoRa tra master e nodi è descritto in ArduinoBridge/MasterNode/ProtocolloLora.h.
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from . import mqtt
from .access import TechnicianRequiredMixin
from .installations import awaited_pairing, detach_node, uninstall_device
from .models import Community, Device, Gateway, LoRaNode

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
    transaction.on_commit(lambda: mqtt.clear_gateway_retained(community.id))


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


# --- Pagine del tecnico ---

class CommunityListView(TechnicianRequiredMixin, View):
    """Le community con il loro master: installato, collegato, quanti nodi."""

    template_name = "SP/community_list.html"
    per_page = 50

    def get(self, request):
        query = request.GET.get("q", "").strip()
        communities = Community.objects.select_related("city", "gateway").annotate(
            systems=Count("photovoltaic_systems", distinct=True), node_count=Count("gateway__nodes", distinct=True),
        ).order_by("name")
        if query:
            communities = communities.filter(Q(name__icontains=query) | Q(city__name__icontains=query))
        context = {
            "page": Paginator(communities, self.per_page).get_page(request.GET.get("page")),
            "query": query,
            "counts": {"gateways": Gateway.objects.count(), "online": Gateway.objects.filter(online=True).count(),
                       "nodes": LoRaNode.objects.count()},
        }
        return render(request, self.template_name, context)


class CommunityInstallationView(TechnicianRequiredMixin, View):
    """Master di una community e i suoi nodi: aggiunta, identificazione, assegnazione agli impianti, rimozione."""

    template_name = "SP/community_installation.html"

    def get(self, request, community_id):
        community = get_object_or_404(Community.objects.select_related("city"), pk=community_id)
        gateway = Gateway.objects.filter(community=community).select_related("installed_by").first()
        nodes = list(gateway.nodes.select_related("system__device").order_by("system", "-joined_at")) if gateway else []
        systems = community.photovoltaic_systems.select_related("device", "lora_node").order_by("name")
        pairing, pairing_step = awaited_pairing(community.pairings, gateway)
        # La pagina si ricarica quando cambia qualcosa: master, finestra, nodi e loro collegamento
        signature = "|".join([
            str(gateway.online if gateway else None), str(gateway.join_seconds_left > 0 if gateway else False),
            str(pairing_step), *(f"{node.node_id}:{node.system_id}:{node_online(node)}" for node in nodes),
        ])
        context = {
            "community": community,
            "gateway": gateway,
            "pending": [node for node in nodes if node.system_id is None],
            "assigned": [node for node in nodes if node.system_id is not None],
            "systems": systems,
            "pairing": pairing,
            "pairing_step": pairing_step,
            "signature": signature,
        }
        return render(request, self.template_name, context)

    def post(self, request, community_id):
        community = get_object_or_404(Community, pk=community_id)
        gateway = Gateway.objects.filter(community=community).first()
        action = request.POST.get("action")
        node = (LoRaNode.objects.select_related("gateway", "system").filter(
            gateway=gateway, node_id=request.POST.get("node", "")).first() if gateway else None)

        if gateway is None:
            messages.error(request, "La community non ha un master.")
        elif action == "permit_join":
            if not gateway.online:
                messages.error(request, "Il master è scollegato: i nodi si aggiungono solo con il master collegato.")
            else:
                with transaction.atomic():
                    permit_join(gateway)
                messages.success(request, "Il master accetta nodi nuovi per 3 minuti: accendi i nodi sui tetti.")
        elif action == "uninstall":
            with transaction.atomic():
                uninstall_gateway(community)
            messages.success(request, "Master disinstallato: le sue credenziali non funzionano più e i suoi nodi sono stati rimossi.")
        elif node is None:
            messages.error(request, "Nodo non trovato.")
        elif action == "identify":
            with transaction.atomic():
                identify(node)
            messages.info(request, f"Il LED del nodo {node.node_id} lampeggia per {IDENTIFY_SECONDS} secondi.")
        elif action == "assign":
            system_id = request.POST.get("system", "")
            system = community.photovoltaic_systems.filter(pk=system_id).first() if system_id.isdigit() else None
            if system is None:
                messages.error(request, "Scegli un impianto della community.")
            else:
                with transaction.atomic():
                    assign_node(node, system, request.user)
                messages.success(request, f"Il nodo {node.node_id} è il sensore di {system.name}.")
        elif action == "remove":
            with transaction.atomic():
                remove_node(node)
            messages.success(request, f"Nodo {node.node_id} rimosso: esce dalla rete del master e torna come nuovo.")
        return redirect("SP:community_installation", community_id=community.id)
