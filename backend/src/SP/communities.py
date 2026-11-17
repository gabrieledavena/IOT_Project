"""Le community, per i tecnici: anagrafica, master con i nodi LoRa e impianti con il loro sensore.

La lista mostra ogni community con titolare, utenti, impianti (quanti con il sensore collegato) e master. La pagina
di una community ha il suo master, i nodi sui tetti da assegnare agli impianti (SP/gateways.py) e gli impianti, da
cui si apre l'installazione di ognuno (SP/installations.py); da lì si crea anche un nuovo impianto della community.

Le community, come quella di una famiglia, le creano, modificano ed eliminano i tecnici (e gli amministratori, anche
dall'admin). Il primo utente associato a una community ne diventa il titolare (SP/customers.py).
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from .access import TechnicianRequiredMixin
from .customers import assign_community
from .forms import CommunityForm
from .gateways import (
    IDENTIFY_SECONDS, assign_node, identify, node_online, permit_join, remove_node, uninstall_gateway,
)
from .installations import awaited_pairing
from .models import Community, Customer, Device, Gateway, LoRaNode, PhotovoltaicSystem
from .sorting import Ordering, disconnected_first, problems_first


class CommunityListView(TechnicianRequiredMixin, View):
    """Tutte le community con titolare, utenti, impianti e loro sensori, master e nodi."""

    template_name = "SP/community_list.html"
    per_page = 50
    ORDERINGS = {
        "name": ("Nome (A-Z)", ("name",)),
        "name_desc": ("Nome (Z-A)", ("-name",)),
        "city": ("Città", ("city__name", "name")),
        "systems": ("Più impianti", ("-systems", "name")),
        "users": ("Più utenti", ("-users", "name")),
        "offline": ("Più sensori scollegati", ("-offline", "name")),
        "without_sensor": ("Più impianti senza sensore", ("-without_sensor", "name")),
        "pending_nodes": ("Più nodi da assegnare", ("-pending_nodes", "name")),
    }

    def get(self, request):
        query = request.GET.get("q", "").strip()
        ordering = Ordering(request, self.ORDERINGS)
        communities = Community.objects.select_related("city", "owner", "gateway").annotate(
            users=Count("customers", distinct=True),
            systems=Count("photovoltaic_systems", distinct=True),
            # Impianti con il sensore collegato, scollegato o senza sensore
            online=Count("photovoltaic_systems", filter=Q(photovoltaic_systems__device__online=True), distinct=True),
            offline=Count("photovoltaic_systems", filter=Q(photovoltaic_systems__device__online=False), distinct=True),
            without_sensor=Count("photovoltaic_systems", filter=Q(photovoltaic_systems__device__isnull=True),
                                 distinct=True),
            node_count=Count("gateway__nodes", distinct=True),
            pending_nodes=Count("gateway__nodes", filter=Q(gateway__nodes__system__isnull=True), distinct=True),
        )
        if query:
            # Anche per nome di un impianto, con una sottoquery: così i conteggi restano di tutti gli impianti
            systems = PhotovoltaicSystem.objects.filter(name__icontains=query).values("community")
            communities = communities.filter(Q(name__icontains=query) | Q(city__name__icontains=query)
                                             | Q(pk__in=systems))
        context = {
            "page": Paginator(ordering.apply(communities), self.per_page).get_page(request.GET.get("page")),
            "query": query,
            "ordering": ordering,
            "counts": {
                "communities": Community.objects.count(),
                "systems": PhotovoltaicSystem.objects.count(),
                "online": Device.objects.filter(online=True).count(),
                "gateways": Gateway.objects.count(),
                "gateways_online": Gateway.objects.filter(online=True).count(),
                "nodes": LoRaNode.objects.count(),
                "pending_nodes": LoRaNode.objects.filter(system__isnull=True).count(),
            },
        }
        return render(request, self.template_name, context)


class CommunityDetailView(TechnicianRequiredMixin, View):
    """Una community: il master e i suoi nodi (aggiunta, identificazione, assegnazione agli impianti, rimozione) e
    gli impianti con il loro sensore."""

    template_name = "SP/community_detail.html"
    # Ordinamenti della tabella degli impianti
    ORDERINGS = {
        "name": ("Nome (A-Z)", ("name",)),
        "name_desc": ("Nome (Z-A)", ("-name",)),
        "id": ("Id", ("id",)),
        "power_desc": ("Potenza più alta", ("-max_power", "name")),
        "power": ("Potenza più bassa", ("max_power", "name")),
        "status": ("Prima i problemi", (problems_first(), "name")),
        "sensor": ("Prima i sensori scollegati", (disconnected_first(), "name")),
    }

    def get(self, request, community_id):
        ordering = Ordering(request, self.ORDERINGS)
        community = get_object_or_404(Community.objects.select_related("city", "owner"), pk=community_id)
        gateway = Gateway.objects.filter(community=community).select_related("installed_by").first()
        nodes = list(gateway.nodes.select_related("system__device").order_by("system", "-joined_at")) if gateway else []
        systems = list(ordering.apply(community.photovoltaic_systems.select_related("device", "lora_node")))
        pairing, pairing_step = awaited_pairing(community.pairings, gateway)
        # La pagina si ricarica quando cambia qualcosa: master, finestra, nodi e collegamento dei sensori
        signature = "|".join([
            str(gateway.online if gateway else None), str(gateway.join_seconds_left > 0 if gateway else False),
            str(pairing_step), *(f"{node.node_id}:{node.system_id}:{node_online(node)}" for node in nodes),
            *(f"{system.id}:{system.device.online if hasattr(system, 'device') else None}" for system in systems),
        ])
        context = {
            "community": community,
            "users": community.customers.count(),
            "gateway": gateway,
            "pending": [node for node in nodes if node.system_id is None],
            "assigned": [node for node in nodes if node.system_id is not None],
            "systems": systems,
            "ordering": ordering,
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
        # Stessa pagina, con lo stesso ordinamento degli impianti
        return redirect(request.get_full_path())


class NewCommunityView(TechnicianRequiredMixin, View):
    """Nuova community; con ?customer=<id> vi si associa subito l'utente in attesa, che ne diventa il titolare."""

    template_name = "SP/community_form.html"

    def get(self, request):
        return self.handle(request, CommunityForm())

    def post(self, request):
        return self.handle(request, CommunityForm(request.POST))

    def handle(self, request, form):
        customer_id = request.GET.get("customer")
        customer = None
        if customer_id and customer_id.isdigit():
            customer = Customer.objects.filter(pk=customer_id, community__isnull=True).first()

        if form.is_bound and form.is_valid():
            community = Community.objects.create(name=form.cleaned_data["name"], city=form.cleaned_data["city"])
            if customer is not None:
                if assign_community(customer, community):
                    messages.success(request, f"Community {community} creata: {customer} ne è il titolare.")
                else:
                    messages.success(request, f"Community {community} creata.")
                # Si torna agli altri utenti in attesa
                return redirect("SP:customer_assignment")
            messages.success(request, f"Community {community} creata.")
            return redirect("SP:community_detail", community_id=community.id)
        return render(request, self.template_name, {"form": form, "customer": customer})


class EditCommunityView(TechnicianRequiredMixin, View):
    """Nome, comune e titolare di una community; i suoi utenti."""

    template_name = "SP/community_form.html"

    def get(self, request, community_id):
        community = get_object_or_404(Community.objects.select_related("city"), pk=community_id)
        return self.render(request, community, CommunityForm(community=community))

    def post(self, request, community_id):
        community = get_object_or_404(Community.objects.select_related("city"), pk=community_id)
        form = CommunityForm(request.POST, community=community)
        if not form.is_valid():
            return self.render(request, community, form)
        community.name, community.city = form.cleaned_data["name"], form.cleaned_data["city"]
        if "owner" in form.cleaned_data:
            community.owner = form.cleaned_data["owner"]
        community.save()
        messages.success(request, f"Community {community} aggiornata.")
        return redirect("SP:community_detail", community_id=community.id)

    def render(self, request, community, form):
        members = community.customers.select_related("user").order_by("surname", "name")
        return render(request, self.template_name, {"form": form, "community": community, "members": members})


class DeleteCommunityView(TechnicianRequiredMixin, View):
    """Elimina una community senza impianti: i suoi utenti tornano in attesa di una community.

    Con gli impianti se ne andrebbero misure e interventi: prima vanno eliminati o spostati gli impianti.
    """

    template_name = "SP/community_delete.html"

    def load(self, community_id):
        community = get_object_or_404(Community.objects.select_related("city", "owner"), pk=community_id)
        return community, community.photovoltaic_systems.count(), community.customers.count()

    def get(self, request, community_id):
        community, systems, users = self.load(community_id)
        context = {"community": community, "systems": systems, "users": users,
                   "has_gateway": hasattr(community, "gateway"),
                   "system_list": community.photovoltaic_systems.order_by("name") if systems else []}
        return render(request, self.template_name, context)

    def post(self, request, community_id):
        community, systems, users = self.load(community_id)
        if systems:
            messages.error(request, f"{community} ha {systems} impianti: prima vanno eliminati o spostati.")
            return redirect("SP:community_delete", community_id=community.id)
        with transaction.atomic():
            # Il master, se c'è, smette di collegarsi e perde la configurazione
            uninstall_gateway(community)
            community.delete()
        if users:
            messages.success(request, f"Community {community} eliminata: i suoi {users} utenti sono in attesa di "
                                      "un'altra community.")
        else:
            messages.success(request, f"Community {community} eliminata.")
        return redirect("SP:community_list")
