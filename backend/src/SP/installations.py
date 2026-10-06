"""Installazione dei dispositivi sugli impianti, per lo staff: simula il tecnico che configura Arduino e bridge.

Installare un dispositivo genera le credenziali con cui il bridge si collega al broker MQTT come quell'impianto
(username pv-<id> e un token mostrato una volta sola). Le stesse pagine servono per i test: si può installare un
dispositivo su un impianto qualsiasi, anche dei dati demo, e impostarne lo stato per provare la pompa di lavaggio.
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from . import mqtt
from .access import StaffRequiredMixin
from .forms import ForceStatusForm, InstallationForm
from .models import Device, PhotovoltaicSystem

# Il token appena generato resta nella sessione fino alla pagina successiva, che lo mostra una volta sola
NEW_TOKEN_SESSION_KEY = "new_device_token"


def install_device(system, staff=None):
    """Installa il dispositivo sull'impianto, o gli dà nuove credenziali se c'è già; restituisce il token."""
    device = Device.objects.filter(system=system).first() or Device(system=system)
    token = device.new_token()
    if device.pk is None:
        device.installed_by = staff
    device.save()
    # Il bridge riceve subito potenza massima e stato, anche se si collega più tardi (messaggi retained)
    transaction.on_commit(lambda: mqtt.publish_state([system]))
    return device, token


def uninstall_device(system):
    Device.objects.filter(system=system).delete()
    transaction.on_commit(lambda: mqtt.clear_retained(system.id))


def force_status(system, status):
    """Imposta lo stato come un controllo automatico e lo pubblica, anche se è uguale a quello attuale.

    Vale come un nuovo cambiamento, così si può riprovare la reazione del dispositivo (la pompa con DRT);
    vale anche come controllo: il prossimo automatico arriva dopo due giorni.
    """
    now = timezone.now()
    system.previous_status, system.status, system.status_changed_at, system.last_check = system.status, status, now, now
    system.save(update_fields=["previous_status", "status", "status_changed_at", "last_check"])
    transaction.on_commit(lambda: mqtt.publish_status([system]))


def bridge_commands(device, token):
    """Comandi per avviare il bridge con le credenziali del dispositivo: con Arduino (SimulIDE) o simulato.

    Con Arduino il bridge trova da solo la porta seriale a cui risponde (Windows, macOS e Linux).
    """
    base = f"python ArduinoBridge/bridge.py --device {device.username} --token {token}"
    return {"serial": base, "simulate": f"{base} --simulate"}


def systems_with_devices():
    return PhotovoltaicSystem.objects.select_related("community__city", "device", "device__installed_by")


class InstallationListView(StaffRequiredMixin, View):
    """Tutti gli impianti con lo stato del loro dispositivo: installato, collegato o no."""

    template_name = "SP/installation_list.html"
    per_page = 50
    FILTERS = {
        "online": Q(device__online=True),
        "offline": Q(device__isnull=False, device__online=False),
        "none": Q(device__isnull=True),
    }

    def get(self, request):
        systems = systems_with_devices()
        query = request.GET.get("q", "").strip()
        if query:
            systems = systems.filter(Q(name__icontains=query) | Q(community__name__icontains=query))
        selected = request.GET.get("device") if request.GET.get("device") in self.FILTERS else ""
        if selected:
            systems = systems.filter(self.FILTERS[selected])
        systems = systems.order_by("community__name", "name")
        context = {
            "page": Paginator(systems, self.per_page).get_page(request.GET.get("page")),
            "query": query,
            "selected": selected,
            "counts": {
                "installed": Device.objects.count(),
                "online": Device.objects.filter(online=True).count(),
                "systems": PhotovoltaicSystem.objects.count(),
            },
        }
        return render(request, self.template_name, context)


class NewInstallationView(StaffRequiredMixin, View):
    """Nuovo impianto di una community, installato insieme al suo dispositivo."""

    template_name = "SP/installation_new.html"

    def get(self, request):
        return render(request, self.template_name, {"form": InstallationForm()})

    def post(self, request):
        form = InstallationForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})
        with transaction.atomic():
            system = form.save()
            device, token = install_device(system, request.user)
        request.session[NEW_TOKEN_SESSION_KEY] = {"system_id": system.id, "token": token}
        messages.success(request, f"Impianto {system.name} creato e dispositivo {device.username} installato.")
        return redirect("SP:installation_detail", system_id=system.id)


class InstallationDetailView(StaffRequiredMixin, View):
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
        context = {
            "system": system,
            "device": device,
            "token": token,
            "commands": bridge_commands(device, token) if device and token else None,
            "form": form,
            "topics": [mqtt.topic(system.id, name) for name in
                       (mqtt.TELEMETRY, mqtt.CONNECTION, mqtt.EVENTS, mqtt.STATUS, mqtt.CONFIG)],
        }
        return render(request, self.template_name, context, status=status)
