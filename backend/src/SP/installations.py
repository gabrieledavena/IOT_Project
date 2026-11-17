"""Installazione dei dispositivi sugli impianti, per lo staff: simula il tecnico che configura Arduino e bridge.

Installare un dispositivo genera le credenziali con cui il bridge si collega al broker MQTT come quell'impianto
(username pv-<id> e un token mostrato una volta sola). Le stesse pagine servono per i test: si può installare un
dispositivo su un impianto qualsiasi, anche dei dati demo, e impostarne lo stato per provare la pompa di lavaggio.
Un dispositivo con il display si installa invece con il suo QR, senza copiare il token (SP/pairing.py); i nodi
LoRa sui tetti, tramite il master della community (SP/gateways.py). Gli impianti si trovano nella pagina della
loro community (SP/communities.py).
"""
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from . import mqtt, telegram
from .access import TechnicianRequiredMixin
from .forms import ForceStatusForm, InstallationForm
from .models import Community, Device, LoRaNode, PhotovoltaicSystem

# Il token appena generato resta nella sessione fino alla pagina successiva, che lo mostra una volta sola
NEW_TOKEN_SESSION_KEY = "new_device_token"


def detach_node(system):
    """L'impianto non ha più come sensore il suo nodo LoRa, che torna da assegnare nella pagina del master."""
    node = LoRaNode.objects.select_related("gateway__community").filter(system=system).first()
    if node is None:
        return
    node.system = None
    node.save(update_fields=["system"])
    transaction.on_commit(lambda: mqtt.publish_gateway_config(node.gateway))


def install_device(system, staff=None):
    """Installa il dispositivo sull'impianto, o gli dà nuove credenziali se c'è già; restituisce il token.

    Se il sensore dell'impianto era un nodo LoRa, ora è il dispositivo con le credenziali: il nodo si stacca.
    """
    detach_node(system)
    device = Device.objects.filter(system=system).first() or Device(system=system)
    token = device.new_token()
    if device.pk is None:
        device.installed_by = staff
    device.save()
    # Il bridge riceve subito potenza massima e stato, anche se si collega più tardi (messaggi retained)
    transaction.on_commit(lambda: mqtt.publish_state([system]))
    return device, token


def uninstall_device(system):
    detach_node(system)
    Device.objects.filter(system=system).delete()
    # L'id letto subito: se poi si elimina anche l'impianto, al commit system.id è None
    system_id = system.id
    transaction.on_commit(lambda: mqtt.clear_retained(system_id))


def delete_system(system):
    """Elimina l'impianto con le sue misure e i suoi interventi.

    Il dispositivo smette di funzionare e i suoi messaggi retained si cancellano; il nodo LoRa, se il sensore era
    un nodo, torna da assegnare nella pagina del master.
    """
    uninstall_device(system)
    system.delete()


def force_status(system, status):
    """Imposta lo stato come un controllo automatico e lo pubblica, anche se è uguale a quello attuale.

    Vale come un nuovo cambiamento, così si può riprovare la reazione del dispositivo (la pompa con DRT e FLT);
    vale anche come controllo: il prossimo automatico arriva dopo due giorni. Con un guasto (BRK) il titolare
    riceve l'avviso su Telegram, anche questo ogni volta, così si può riprovare.
    """
    now = timezone.now()
    system.previous_status, system.status, system.status_changed_at, system.last_check = system.status, status, now, now
    system.save(update_fields=["previous_status", "status", "status_changed_at", "last_check"])
    transaction.on_commit(lambda: mqtt.publish_status([system]))
    transaction.on_commit(lambda: telegram.notify_broken([system]))


def bridge_commands(device, token):
    """Comandi per avviare il bridge con le credenziali del dispositivo: con Arduino (SimulIDE) o simulato.

    Con Arduino il bridge trova da solo la porta seriale a cui risponde (Windows, macOS e Linux).
    """
    base = f"python ArduinoBridge/bridge.py --device {device.username} --token {token}"
    return {"serial": base, "simulate": f"{base} --simulate"}


def systems_with_devices():
    return PhotovoltaicSystem.objects.select_related(
        "community__city", "device", "device__installed_by", "lora_node__gateway",
    )


def search(systems, query):
    """Impianti il cui nome o quello della community contiene il testo cercato."""
    return systems.filter(Q(name__icontains=query) | Q(community__name__icontains=query)) if query else systems


def awaited_pairing(pairings, device):
    """Installazione con il QR di cui la pagina aspetta l'esito, e la fase, o (None, None).

    pairings sono gli abbinamenti dell'impianto (o della community, per il master); device il suo dispositivo
    (o il master). Fasi: "pickup" il dispositivo deve ritirare le credenziali, "broker" le ha ritirate e deve
    collegarsi al broker.
    """
    pairing = pairings.filter(expires_at__gt=timezone.now()).order_by("-claimed_at").first()
    if pairing is None:
        return None, None
    if pairing.delivered_at is None:
        return pairing, "pickup"
    # Collegato dopo aver ricevuto le credenziali: "online" da prima è del dispositivo che è stato sostituito
    if device and device.online and device.last_seen and device.last_seen >= pairing.delivered_at:
        return None, None
    return pairing, "broker"


class NewInstallationView(TechnicianRequiredMixin, View):
    """Nuovo impianto di una community, installato insieme al suo dispositivo.

    Con ?community=<id>, dalla pagina della community, la community è già scelta e la pagina vi riporta.
    """

    template_name = "SP/installation_new.html"

    @cached_property
    def community(self):
        community_id = self.request.GET.get("community", "")
        return Community.objects.filter(pk=community_id).first() if community_id.isdigit() else None

    def get(self, request, **kwargs):
        form = InstallationForm(initial={"community": self.community.id} if self.community else None)
        return render(request, self.template_name, self.context(form))

    def post(self, request, **kwargs):
        form = InstallationForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, self.context(form))
        with transaction.atomic():
            system = form.save()
            self.install(system)
        return redirect("SP:installation_detail", system_id=system.id)

    def context(self, form):
        return {"form": form, "community": self.community}

    def install(self, system):
        """Installa il dispositivo sul nuovo impianto: il token compare una volta sola nella pagina seguente."""
        device, token = install_device(system, self.request.user)
        self.request.session[NEW_TOKEN_SESSION_KEY] = {"system_id": system.id, "token": token}
        messages.success(self.request, f"Impianto {system.name} creato e dispositivo {device.username} installato.")


class InstallationDetailView(TechnicianRequiredMixin, View):
    """Dispositivo di un impianto: installazione, nuove credenziali, disinstallazione e stato simulato."""

    template_name = "SP/installation_detail.html"

    def get(self, request, system_id):
        system = get_object_or_404(systems_with_devices(), pk=system_id)
        new_token = request.session.pop(NEW_TOKEN_SESSION_KEY, None)
        token = new_token["token"] if new_token and new_token["system_id"] == system.id else None
        return self.render(request, system, ForceStatusForm(initial={"status": system.status}), token)

    def post(self, request, system_id):
        system = get_object_or_404(systems_with_devices(), pk=system_id)
        action = request.POST.get("action")
        device = getattr(system, "device", None)

        if action in ("install", "regenerate"):
            with transaction.atomic():
                device, token = install_device(system, request.user)
            request.session[NEW_TOKEN_SESSION_KEY] = {"system_id": system.id, "token": token}
            if action == "install":
                messages.success(request, f"Dispositivo {device.username} installato.")
            else:
                messages.warning(request, "Nuove credenziali generate: quelle precedenti non funzionano più.")
        elif action == "uninstall" and device:
            with transaction.atomic():
                uninstall_device(system)
            messages.success(request, "Dispositivo disinstallato: le sue credenziali non funzionano più.")
        elif action == "force_status":
            form = ForceStatusForm(request.POST)
            if not form.is_valid():
                return self.render(request, system, form, None, status=400)
            with transaction.atomic():
                force_status(system, form.cleaned_data["status"])
            sent = " e inviato al dispositivo" if device else ""
            messages.success(request, f"Stato impostato a «{system.get_status_display()}»{sent}.")
        return redirect("SP:installation_detail", system_id=system.id)

    def render(self, request, system, form, token, status=200):
        device = getattr(system, "device", None)
        pairing, pairing_step = awaited_pairing(system.pairings, device)
        context = {
            "system": system,
            "device": device,
            "token": token,
            "pairing": pairing,
            "pairing_step": pairing_step,
            "commands": bridge_commands(device, token) if device and token else None,
            "form": form,
            "topics": [mqtt.topic(system.id, name) for name in
                       (mqtt.TELEMETRY, mqtt.CONNECTION, mqtt.EVENTS, mqtt.STATUS, mqtt.CONFIG)],
        }
        return render(request, self.template_name, context, status=status)


class DeleteSystemView(TechnicianRequiredMixin, View):
    """Elimina un impianto, dopo una pagina di conferma con quello che si perde.

    ?next= riporta alla pagina da cui si arriva (per esempio l'eliminazione della community).
    """

    template_name = "SP/installation_delete.html"

    def next_url(self, request):
        url = request.POST.get("next") or request.GET.get("next")
        if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()},
                                                   require_https=request.is_secure()):
            return url
        return None

    def get(self, request, system_id):
        system = get_object_or_404(systems_with_devices(), pk=system_id)
        context = {
            "system": system,
            "device": getattr(system, "device", None),
            "node": getattr(system, "lora_node", None),
            "readings": system.panel_data.count(),
            "interventions": system.interventions.count(),
            "next": self.next_url(request) or "",
        }
        return render(request, self.template_name, context)

    def post(self, request, system_id):
        system = get_object_or_404(systems_with_devices(), pk=system_id)
        name, community = system.name, system.community
        with transaction.atomic():
            delete_system(system)
        messages.success(request, f"Impianto {name} di {community.name} eliminato.")
        next_url = self.next_url(request)
        return redirect(next_url) if next_url else redirect("SP:community_detail", community_id=community.id)
