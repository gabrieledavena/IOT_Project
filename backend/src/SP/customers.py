"""Utenti appena registrati, in attesa di una community: solo i tecnici li associano a quella del loro impianto.

Chi si registra, privato o di un'azienda, non sceglie la community: finché un tecnico non lo associa, la sua home
dice che la registrazione è riuscita e che si aspetta il tecnico. Il primo utente di una community ne diventa il
titolare (Customer.save). Le community, come quella di una famiglia, le creano, modificano ed eliminano i tecnici
(e gli amministratori, anche dall'admin).
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views import View

from .access import TechnicianRequiredMixin
from .forms import CommunityAssignmentForm, CommunityForm
from .gateways import uninstall_gateway
from .models import Community, Customer


def assign_community(customer, community):
    """Associa alla community un utente che non ne ha ancora una; False se nel frattempo ne ha già una."""
    with transaction.atomic():
        customer = Customer.objects.select_for_update().get(pk=customer.pk)
        if customer.community_id is not None:
            return False
        customer.community = community
        customer.save(update_fields=["community"])
    return True


class PendingCustomerListView(TechnicianRequiredMixin, View):
    """Gli utenti senza community, dal primo che si è registrato, ognuno con la scelta della community."""

    template_name = "SP/customer_assignment.html"
    per_page = 20

    def get(self, request):
        query = request.GET.get("q", "").strip()
        customers = Customer.objects.filter(community__isnull=True).select_related("user").order_by("user__date_joined")
        total = customers.count()
        if query:
            customers = customers.filter(
                Q(name__icontains=query) | Q(surname__icontains=query) | Q(user__username__icontains=query)
                | Q(user__email__icontains=query) | Q(phone__icontains=query)
            )
        context = {
            "page": Paginator(customers, self.per_page).get_page(request.GET.get("page")),
            "query": query,
            "total": total,
            # Una scelta della community per ogni utente: la lista si legge una volta per pagina. all(): il
            # queryset del campo è uno solo per tutto il processo, e letto direttamente terrebbe in memoria le
            # community della prima pagina aperta, senza quelle create dopo
            "communities": list(CommunityAssignmentForm.base_fields["community"].queryset.all()),
        }
        return render(request, self.template_name, context)


class AssignCommunityView(TechnicianRequiredMixin, View):
    def post(self, request, customer_id):
        customer = get_object_or_404(Customer, pk=customer_id)
        form = CommunityAssignmentForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Scegli la community a cui associare {customer}.")
        elif assign_community(customer, form.cleaned_data["community"]):
            messages.success(request, f"{customer} ora fa parte della community {form.cleaned_data['community']}.")
        else:
            customer.refresh_from_db()
            messages.info(request, f"{customer} è già nella community {customer.community}.")
        # Si torna alla stessa ricerca
        query = request.POST.get("q", "").strip()
        url = reverse("SP:customer_assignment")
        return redirect(f"{url}?{urlencode({'q': query})}" if query else url)


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
            if customer is not None and assign_community(customer, community):
                messages.success(request, f"Community {community} creata: {customer} ne è il titolare.")
            else:
                messages.success(request, f"Community {community} creata.")
            return redirect("SP:customer_assignment")
        return render(request, self.template_name, {"form": form, "customer": customer})


class ManageCommunitiesView(TechnicianRequiredMixin, View):
    """Tutte le community, con titolare, utenti e impianti, da modificare o eliminare."""

    template_name = "SP/community_manage_list.html"
    per_page = 50

    def get(self, request):
        query = request.GET.get("q", "").strip()
        communities = Community.objects.select_related("city", "owner").annotate(
            users=Count("customers", distinct=True), systems=Count("photovoltaic_systems", distinct=True),
        ).order_by("name")
        if query:
            communities = communities.filter(Q(name__icontains=query) | Q(city__name__icontains=query))
        context = {"page": Paginator(communities, self.per_page).get_page(request.GET.get("page")), "query": query}
        return render(request, self.template_name, context)


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
        return redirect("SP:community_manage_list")

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
                   "has_gateway": hasattr(community, "gateway")}
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
        return redirect("SP:community_manage_list")
