"""Simulazione degli impianti senza dispositivo di una community, per le demo con un solo nodo vero.

Con due schede (il master e un nodo sul tetto) una community ha un solo impianto con un sensore vero. Dalla pagina
Simulazione (solo amministratori, /sp/simulation/) si simulano gli altri: ogni impianto della community senza
dispositivo riceve un sensore simulato (SimulatedSensor) e un dispositivo senza token, come quello di un nodo LoRa.

Li simula il master della community, che li tratta come nodi sul tetto senza radio. Il server gli manda gli impianti
simulati con i valori dei loro sensori (topic simulation del master, retained, SP/mqtt.py) e il master:
  - li mostra sul display, una pagina per impianto come per i nodi veri, e li somma alla produzione della community;
  - pubblica per loro la presenza (connection), una misura ogni 10 secondi mediata sul minuto (telemetry) e i
    lavaggi (events) quando lo stato diventa DRT o FLT, come il nodo sul tetto.
Il server quindi li salva e li controlla come gli altri impianti. I valori (potenza, temperatura, luce) li sceglie
l'amministratore: il master li riceve subito e li mostra e usa dalla misura seguente.

Un impianto smette di essere simulato quando si ferma la simulazione, quando riceve un sensore vero (dispositivo
con le credenziali, QR del display o nodo LoRa) o quando si disinstalla il master della community.
"""
from django.contrib import messages
import ipaddress
import urllib.request
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from . import mqtt
from .access import AdminRequiredMixin
from .forms import SimulatedValuesForm
from .models import Community, Device, Gateway, SimulatedSensor

# Scenari proposti dalla pagina: produzione (frazione della potenza installata), temperatura (°C) e luce (lux)
PRESETS = {
    "sun": {"label": "Sole pieno", "share": 0.85, "temperature": 30.0, "lightness": 90000.0},
    "hazy": {"label": "Sole velato", "share": 0.5, "temperature": 24.0, "lightness": 50000.0},
    "clouds": {"label": "Nuvoloso", "share": 0.2, "temperature": 18.0, "lightness": 15000.0},
    "night": {"label": "Notte", "share": 0.0, "temperature": 12.0, "lightness": 0.0},
}
DEFAULT_PRESET = "hazy"


def preset_values(system, preset):
    values = PRESETS[preset]
    return {
        "power": round(system.max_power * values["share"], 2),
        "temperature": values["temperature"],
        "lightness": values["lightness"],
    }


def candidates(community):
    """Impianti della community che si possono simulare: quelli senza un dispositivo (sensore vero o simulato)."""
    return community.photovoltaic_systems.filter(device__isnull=True).order_by("name")


def simulated_systems(community):
    return (community.photovoltaic_systems.filter(simulated_sensor__isnull=False)
            .select_related("simulated_sensor", "device").order_by("name"))


def start(community, staff=None):
    """Simula gli impianti della community ancora senza dispositivo; restituisce quelli aggiunti.

    Serve il master: è lui a simulare i sensori. Gli impianti già simulati restano con i loro valori.
    """
    gateway = Gateway.objects.filter(community=community).first()
    if gateway is None:
        raise ValueError("La community non ha un master.")
    systems = list(candidates(community))
    for system in systems:
        # Senza token, come il dispositivo di un nodo LoRa: non si collega al broker, pubblica per lui il master
        Device.objects.create(system=system, token_hash="", installed_by=staff)
        SimulatedSensor.objects.create(system=system, **preset_values(system, DEFAULT_PRESET))
    if systems:
        community_id = community.id
        # Prima stato e configurazione degli impianti (retained), poi l'elenco al master, che vi si iscrive
        transaction.on_commit(lambda: (mqtt.publish_state(systems), mqtt.publish_gateway_simulation(community_id)))
    return systems


def stop(community):
    """Smette di simulare gli impianti della community: tornano senza dispositivo. Restituisce quanti erano."""
    systems = list(simulated_systems(community))
    for system in systems:
        _remove(system)
    if systems:
        ids, community_id = [system.id for system in systems], community.id
        transaction.on_commit(lambda: (
            mqtt.publish_gateway_simulation(community_id), *(mqtt.clear_retained(system_id) for system_id in ids),
        ))
    return len(systems)


def release(system):
    """L'impianto riceve un sensore vero o perde il dispositivo: se era simulato, il master smette di simularlo.

    Elimina il sensore simulato e il suo dispositivo; chi la chiama installa il nuovo dispositivo o cancella i
    messaggi retained dell'impianto.
    """
    if not SimulatedSensor.objects.filter(system=system).exists():
        return False
    _remove(system)
    community_id = system.community_id
    transaction.on_commit(lambda: mqtt.publish_gateway_simulation(community_id))
    return True


def _remove(system):
    SimulatedSensor.objects.filter(system=system).delete()
    Device.objects.filter(system=system).delete()
    # Dispositivo e sensore appena eliminati non restino nella cache dell'oggetto (select_related)
    for cached in ("device", "simulated_sensor"):
        system._state.fields_cache.pop(cached, None)


def set_values(sensor, power, temperature, lightness):
    """Nuovi valori del sensore simulato: il master li riceve subito (topic simulation)."""
    sensor.power, sensor.temperature, sensor.lightness = power, temperature, lightness
    sensor.save(update_fields=["power", "temperature", "lightness", "updated_at"])
    community_id = sensor.system.community_id
    transaction.on_commit(lambda: mqtt.publish_gateway_simulation(community_id))


# --- Indirizzo del server nella rete locale ---

def _lan_address(host):
    """True se host è un indirizzo IP della rete locale (non localhost)."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback


def _answers(url):
    """True se all'indirizzo risponde questo sito: il server lo prova da sé, uscendo dal container verso il PC."""
    try:
        with urllib.request.urlopen(url + reverse("SP:login"), timeout=1.5) as response:
            return response.status == 200 and b"Solar Family" in response.read()
    except (OSError, ValueError):
        return False


def server_address(request):
    """Indirizzo a cui il master e il telefono raggiungono il server nella rete locale, o None se non è noto.

    È quello da scrivere nella configurazione del Wi-Fi del master (Avanzate) e da cui il telefono apre i QR di
    abbinamento. Il server gira in docker e non vede la rete del PC: se la pagina è aperta con un indirizzo della
    rete locale (per esempio dal telefono) vale quello, altrimenti quello che start.sh ha passato avviando docker
    compose (SERVER_LAN_HOST). Quest'ultimo può essere vecchio, se poi il PC ha cambiato rete o IP: il server
    controlla che risponda ancora (reachable).
    """
    parts = urlsplit(f"//{request.get_host()}")
    if parts.hostname and _lan_address(parts.hostname):
        port = f":{parts.port}" if parts.port and parts.port != 80 else ""
        return {"url": f"http://{parts.hostname}{port}", "reachable": True}
    if not settings.SERVER_LAN_HOST:
        return None
    port = f":{settings.SERVER_LAN_PORT}" if settings.SERVER_LAN_PORT != 80 else ""
    url = f"http://{settings.SERVER_LAN_HOST}{port}"
    return {"url": url, "reachable": _answers(url)}


# --- Pagine (solo amministratori) ---

def last_reading(system):
    reading = system.panel_data.order_by("-time_stamp").first()
    if reading is None:
        return None
    return {"time": timezone.localtime(reading.time_stamp).strftime("%d/%m %H:%M"), "power": reading.power,
            "temperature": reading.temperature, "lightness": reading.lightness}


def sensor_state(system):
    """Collegamento, lavaggio, stato e ultima misura salvata dal server di un impianto, per la pagina."""
    device = getattr(system, "device", None)
    return {
        "id": system.id,
        "online": bool(device and device.online),
        "last_seen": timezone.localtime(device.last_seen).strftime("%H:%M:%S") if device and device.last_seen else None,
        "pump_running": bool(device and device.pump_running),
        "status": system.status,
        "status_label": system.get_status_display(),
        "reading": last_reading(system),
    }


class SimulationListView(AdminRequiredMixin, View):
    """Le community con un master, che può simulare i loro impianti senza sensore."""

    template_name = "SP/simulation_list.html"

    def get(self, request):
        communities = (Community.objects.filter(gateway__isnull=False).select_related("city", "gateway")
                       .annotate(systems=Count("photovoltaic_systems", distinct=True),
                                 simulated=Count("photovoltaic_systems",
                                                 filter=Q(photovoltaic_systems__simulated_sensor__isnull=False),
                                                 distinct=True),
                                 without_sensor=Count("photovoltaic_systems",
                                                      filter=Q(photovoltaic_systems__device__isnull=True),
                                                      distinct=True))
                       .order_by("-gateway__online", "name"))
        return render(request, self.template_name,
                      {"communities": communities, "server_address": server_address(request)})


class SimulationView(AdminRequiredMixin, View):
    """Una community: avvia o ferma la simulazione dei suoi impianti senza sensore e ne cambia i valori.

    I valori si salvano anche senza ricaricare la pagina: con Accept: application/json la risposta è JSON.
    """

    template_name = "SP/simulation_detail.html"

    def get(self, request, community_id):
        community = get_object_or_404(Community.objects.select_related("city"), pk=community_id)
        return self.render(request, community)

    def post(self, request, community_id):
        community = get_object_or_404(Community, pk=community_id)
        action = request.POST.get("action")
        if action == "save":
            return self.save(request, community)
        if action == "start":
            if not Gateway.objects.filter(community=community).exists():
                messages.error(request, "La community non ha un master: è lui a simulare i sensori.")
            else:
                with transaction.atomic():
                    systems = start(community, request.user)
                if systems:
                    messages.success(request, f"Simulazione avviata per {len(systems)} impiant"
                                              f"{'o' if len(systems) == 1 else 'i'}: il master li mostra sul display.")
                else:
                    messages.info(request, "Nessun impianto senza sensore da simulare.")
        elif action == "stop":
            with transaction.atomic():
                count = stop(community)
            messages.success(request, f"Simulazione fermata: {count} impiant{'o' if count == 1 else 'i'} "
                                      f"di nuovo senza sensore.")
        return redirect("SP:simulation_detail", community_id=community.id)

    def save(self, request, community):
        wants_json = "application/json" in request.headers.get("Accept", "")
        sensor = SimulatedSensor.objects.select_related("system").filter(
            system_id=request.POST.get("system") or 0, system__community=community).first()
        if sensor is None:
            if wants_json:
                return JsonResponse({"errors": {"system": ["Impianto non simulato."]}}, status=404)
            messages.error(request, "L'impianto non è più simulato.")
            return redirect("SP:simulation_detail", community_id=community.id)
        form = SimulatedValuesForm(request.POST, instance=sensor)
        if not form.is_valid():
            if wants_json:
                return JsonResponse({"errors": form.errors}, status=400)
            return self.render(request, community, invalid=form, status=400)
        with transaction.atomic():
            set_values(sensor, **form.cleaned_data)
        if wants_json:
            return JsonResponse({"system": sensor.system_id, **form.cleaned_data})
        messages.success(request, f"Valori di {sensor.system.name} inviati al master.")
        return redirect("SP:simulation_detail", community_id=community.id)

    def render(self, request, community, invalid=None, status=200):
        gateway = Gateway.objects.filter(community=community).first()
        systems = list(community.photovoltaic_systems.select_related("device", "lora_node", "simulated_sensor")
                       .order_by("name"))
        simulated = []
        for system in systems:
            sensor = getattr(system, "simulated_sensor", None)
            if sensor is not None:
                form = invalid if invalid is not None and invalid.instance.pk == sensor.pk else SimulatedValuesForm(
                    instance=sensor)
                simulated.append({"system": system, "form": form, "state": sensor_state(system)})
        context = {
            "community": community,
            "gateway": gateway,
            "simulated": simulated,
            "real": [{"system": system, "state": sensor_state(system)} for system in systems
                     if hasattr(system, "device") and not hasattr(system, "simulated_sensor")],
            "candidates": [system for system in systems if not hasattr(system, "device")],
            "presets": PRESETS,
            "server_address": server_address(request),
        }
        return render(request, self.template_name, context, status=status)


class SimulationStatusView(AdminRequiredMixin, View):
    """Master e sensori della community in JSON: la pagina li aggiorna ogni pochi secondi."""

    def get(self, request, community_id):
        community = get_object_or_404(Community, pk=community_id)
        gateway = Gateway.objects.filter(community=community).first()
        systems = community.photovoltaic_systems.filter(device__isnull=False).select_related("device")
        return JsonResponse({
            "gateway": {
                "online": bool(gateway and gateway.online),
                "last_seen": (timezone.localtime(gateway.last_seen).strftime("%H:%M:%S")
                              if gateway and gateway.last_seen else None),
            },
            "systems": [sensor_state(system) for system in systems],
        })
