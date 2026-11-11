"""Installazione di un dispositivo con il QR del suo display: il tecnico non deve copiare username e token.

1. Il dispositivo, collegato al Wi-Fi, chiede un codice (POST devices/pairing/) e mostra sul display il QR con
   l'indirizzo della pagina di abbinamento (pair/<codice>/), e sotto il codice stesso.
2. Il tecnico scansiona il QR con il telefono, o scrive il codice nella pagina dell'impianto, e sceglie l'impianto:
   uno esistente o uno nuovo. Il master di una community (role=gateway nella richiesta del codice) si installa
   invece su una community, da questa pagina o da quella della community.
3. Il dispositivo chiede ogni POLL_INTERVAL secondi se è stato abbinato (POST devices/pairing/credentials/, con
   codice e segreto): appena il tecnico ha scelto riceve username, token e indirizzo del broker, e si collega.

Le richieste del dispositivo sono form (code=...&secret=...) senza login: lo riconosce il segreto, che riceve
insieme al codice. Il token viene generato al ritiro come con "Installa dispositivo" (SP/installations.py): se
l'impianto (o la community) aveva già un dispositivo (o un master), le sue credenziali smettono di funzionare.
"""
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .access import TechnicianRequiredMixin
from .gateways import install_gateway
from .installations import NewInstallationView, install_device, search, systems_with_devices
from .models import Community, DevicePairing, PhotovoltaicSystem

POLL_INTERVAL = 5  # secondi tra due richieste del dispositivo
CHOICES_SHOWN = 20
INVALID_CODE = "Codice non valido o scaduto: controlla il QR sul display del dispositivo."
GATEWAY_CODE = "È il codice di un master: abbinalo nella pagina della sua community."
DEVICE_CODE = "È il codice di un dispositivo: abbinalo nella pagina del suo impianto."


def new_pairing(role=DevicePairing.ROLE_DEVICE):
    """Nuova richiesta di abbinamento; restituisce la richiesta e il segreto, che riceve solo il dispositivo."""
    now = timezone.now()
    # Le richieste scadute non servono più: si cancellano qui, così non si accumulano
    DevicePairing.objects.filter(expires_at__lte=now).delete()
    pairing = DevicePairing(created_at=now, expires_at=now + DevicePairing.LIFETIME, role=role)
    secret = pairing.new_secret()
    while DevicePairing.objects.filter(code=pairing.code).exists():
        secret = pairing.new_secret()
    pairing.save()
    return pairing, secret


def open_pairing(code):
    """Richiesta non scaduta e con le credenziali non ancora ritirate, o None."""
    return (DevicePairing.objects.select_related("system", "community", "claimed_by")
            .filter(code=DevicePairing.normalize_code(code), expires_at__gt=timezone.now(), delivered_at__isnull=True)
            .first())


def claim(pairing, target, staff):
    """Il tecnico sceglie l'impianto del dispositivo, o la community del master.

    False se la richiesta è scaduta o un altro tecnico ha già scelto.
    """
    field = "community" if pairing.is_gateway else "system"
    now = timezone.now()
    with transaction.atomic():
        claimed = DevicePairing.objects.filter(
            pk=pairing.pk, system__isnull=True, community__isnull=True, expires_at__gt=now,
        ).update(**{field: target}, claimed_by=staff, claimed_at=now, expires_at=now + DevicePairing.WAIT_TIME)
        if claimed:
            # Un solo dispositivo abbinato per impianto (un master per community), l'ultimo: gli altri non
            # ricevono le credenziali
            DevicePairing.objects.filter(**{field: target}).exclude(pk=pairing.pk).delete()
    return bool(claimed)


def deliver(pairing):
    """Installa il dispositivo (o il master); restituisce l'installato e il token, o (None, None) se già ritirati."""
    now = timezone.now()
    with transaction.atomic():
        # Una volta sola, anche con due richieste insieme. La richiesta resta per WAIT_TIME: intanto la pagina
        # dell'impianto aspetta che il dispositivo si colleghi al broker
        if not DevicePairing.objects.filter(pk=pairing.pk, delivered_at__isnull=True).update(
                delivered_at=now, expires_at=now + DevicePairing.WAIT_TIME):
            return None, None
        if pairing.is_gateway:
            return install_gateway(pairing.community, pairing.claimed_by)
        return install_device(pairing.system, pairing.claimed_by)


def broker_address(request):
    """Host e porta del broker per il dispositivo che ha fatto la richiesta (vedi MQTT_PUBLIC_HOST)."""
    host = settings.MQTT_PUBLIC_HOST or urlsplit(f"//{request.get_host()}").hostname
    return host, settings.MQTT_PUBLIC_PORT


def target_page(target):
    if isinstance(target, Community):
        return redirect("SP:community_installation", community_id=target.id)
    return redirect("SP:installation_detail", system_id=target.id)


# --- Richieste del dispositivo ---

def _expired():
    return JsonResponse({"status": "expired"}, status=404)


@csrf_exempt
@require_POST
def start(request):
    """Il dispositivo chiede un codice: lo riceve con il segreto e l'indirizzo della pagina di abbinamento (il QR).

    Il master di una community lo chiede con role=gateway.
    """
    role = request.POST.get("role", DevicePairing.ROLE_DEVICE)
    if role not in dict(DevicePairing.ROLES):
        return JsonResponse({"status": "invalid", "error": "role"}, status=400)
    pairing, secret = new_pairing(role)
    return JsonResponse({
        "code": pairing.code,
        "secret": secret,
        "url": request.build_absolute_uri(reverse("SP:pair", args=[pairing.code])),
        "expires_in": int(DevicePairing.LIFETIME.total_seconds()),
        "interval": POLL_INTERVAL,
    }, status=201)


@csrf_exempt
@require_POST
def credentials(request):
    """Il dispositivo chiede se è stato abbinato.

    202 "pending" (QR non ancora letto) o "scanned" (il tecnico sta scegliendo l'impianto): riprovare dopo
    POLL_INTERVAL secondi. 200 "installed" con le credenziali, una volta sola. 404 "expired" se il codice è
    scaduto, è già stato usato o il segreto è sbagliato: serve un nuovo codice.
    """
    pairing = open_pairing(request.POST.get("code", ""))
    if pairing is None or not pairing.check_secret(request.POST.get("secret", "")):
        return _expired()
    if pairing.target is None:
        return JsonResponse({"status": "scanned" if pairing.scanned_at else "pending"}, status=202)
    installed, token = deliver(pairing)
    if installed is None:
        return _expired()
    host, port = broker_address(request)
    answer = {"status": "installed", "role": pairing.role, "username": installed.username, "token": token,
              "mqtt_host": host, "mqtt_port": port}
    if pairing.is_gateway:
        answer.update(community_id=pairing.community.id, community=pairing.community.name)
    else:
        answer.update(system_id=pairing.system.id, system=pairing.system.name)
    return JsonResponse(answer)


# --- Pagine del tecnico ---

def claim_and_show(request, pairing, target):
    """Abbina il dispositivo e apre la pagina dell'impianto (o della community), che ne aspetta il collegamento."""
    if pairing is not None and claim(pairing, target, request.user):
        messages.success(request, f"Dispositivo {pairing.display_code} abbinato: riceverà le credenziali entro pochi secondi.")
    else:
        messages.error(request, INVALID_CODE)
    return target_page(target)


class PairingView(TechnicianRequiredMixin, View):
    """Pagina aperta dal QR del dispositivo: il tecnico sceglie l'impianto (esistente o nuovo) o la community."""

    template_name = "SP/pairing.html"

    def get(self, request, code):
        pairing = open_pairing(code)
        if pairing is None:
            return render(request, self.template_name, {"pairing": None}, status=404)
        if pairing.scanned_at is None:
            # Il display del dispositivo mostra che il QR è stato letto
            DevicePairing.objects.filter(pk=pairing.pk).update(scanned_at=timezone.now())
        query = request.GET.get("q", "").strip()
        choices = self.communities(query) if pairing.is_gateway else self.systems(query)
        return render(request, self.template_name, {"pairing": pairing, "query": query, "choices": choices[:CHOICES_SHOWN]})

    def systems(self, query):
        if query:
            return search(systems_with_devices(), query).order_by("community__name", "name")
        # Di solito l'impianto è appena stato creato: i più recenti senza dispositivo
        return systems_with_devices().filter(device__isnull=True).order_by("-id")

    def communities(self, query):
        communities = Community.objects.select_related("city", "gateway").order_by("name")
        if query:
            return communities.filter(Q(name__icontains=query) | Q(city__name__icontains=query))
        return communities.filter(gateway__isnull=True)

    def post(self, request, code):
        pairing = open_pairing(code)
        model, field = (Community, "community") if pairing is not None and pairing.is_gateway else (PhotovoltaicSystem, "system")
        target_id = request.POST.get(field, "")
        if not target_id.isdigit():
            raise Http404("Impianto o community non indicati.")
        return claim_and_show(request, pairing, get_object_or_404(model, pk=target_id))


class PairingCodeView(TechnicianRequiredMixin, View):
    """Codice del display scritto nella pagina dell'impianto, se il QR non si riesce a scansionare."""

    def post(self, request, system_id):
        system = get_object_or_404(PhotovoltaicSystem, pk=system_id)
        pairing = open_pairing(request.POST.get("code", ""))
        if pairing is not None and pairing.is_gateway:
            messages.error(request, GATEWAY_CODE)
            return target_page(system)
        return claim_and_show(request, pairing, system)


class CommunityPairingCodeView(TechnicianRequiredMixin, View):
    """Codice del display del master scritto nella pagina della community."""

    def post(self, request, community_id):
        community = get_object_or_404(Community, pk=community_id)
        pairing = open_pairing(request.POST.get("code", ""))
        if pairing is not None and not pairing.is_gateway:
            messages.error(request, DEVICE_CODE)
            return target_page(community)
        return claim_and_show(request, pairing, community)


class PairedInstallationView(NewInstallationView):
    """Nuovo impianto per il dispositivo del QR: appena creato, il dispositivo ne riceve le credenziali."""

    @cached_property
    def pairing(self):
        pairing = open_pairing(self.kwargs["code"])
        return None if pairing is not None and pairing.is_gateway else pairing

    def get(self, request, code):
        if self.pairing is None or self.pairing.system_id:
            return redirect("SP:pair", code=code)
        return super().get(request)

    def context(self, form):
        return {"form": form, "pairing": self.pairing}

    def install(self, system):
        # Se nel frattempo il codice è scaduto l'impianto viene creato lo stesso: basta scansionare il nuovo QR
        if self.pairing is not None and claim(self.pairing, system, self.request.user):
            messages.success(self.request, f"Impianto {system.name} creato: il dispositivo {self.pairing.display_code} "
                                           "riceverà le credenziali entro pochi secondi.")
        else:
            messages.warning(self.request, f"Impianto {system.name} creato, ma il codice del dispositivo è scaduto: "
                                           "scansiona il nuovo QR del display e scegli questo impianto.")
