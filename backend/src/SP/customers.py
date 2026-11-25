"""Utenti appena registrati, in attesa di una community: solo i tecnici li associano a quella del loro impianto.

Chi si registra, privato o di un'azienda, non sceglie la community: finché un tecnico non lo associa, la sua home
dice che la registrazione è riuscita e che si aspetta il tecnico. Il primo utente di una community ne diventa il
titolare (Customer.save), che deve avere telefono ed email: se gli mancano, il tecnico li inserisce associandolo.
Le community le creano, modificano ed eliminano i tecnici (SP/communities.py).
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views import View

from .access import TechnicianRequiredMixin
from .forms import CommunityAssignmentForm, ContactForm
from .models import Customer
from .sorting import Ordering

# Prefisso dei campi con i recapiti di chi diventa titolare, nelle pagine con altri campi (anche SP/communities.py)
OWNER_PREFIX = "owner"


def owner_contacts_form(customer, data=None):
    """Telefono ed email, obbligatori, del cliente che diventa o è il titolare di una community.

    Con i dati inviati vale solo se la pagina li aveva chiesti proprio per lui (campo nascosto owner-customer):
    altrimenti il form si lega ai suoi recapiti di adesso, ed è valido solo se li ha già tutti e due.
    """
    if data is None:
        return ContactForm(customer=customer, required=True, prefix=OWNER_PREFIX)
    if data.get(f"{OWNER_PREFIX}-customer") != str(customer.pk):
        data = {f"{OWNER_PREFIX}-email": customer.user.email, f"{OWNER_PREFIX}-phone": customer.phone}
    return ContactForm(data, customer=customer, required=True, prefix=OWNER_PREFIX)


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
    ORDERINGS = {
        "oldest": ("In attesa da più tempo", ("user__date_joined",)),
        "newest": ("Registrati da poco", ("-user__date_joined",)),
        "surname": ("Cognome (A-Z)", ("surname", "name")),
        "name": ("Nome (A-Z)", ("name", "surname")),
    }

    def get(self, request):
        query = request.GET.get("q", "").strip()
        ordering = Ordering(request, self.ORDERINGS)
        customers = ordering.apply(Customer.objects.filter(community__isnull=True).select_related("user"))
        total = customers.count()
        if query:
            customers = customers.filter(
                Q(name__icontains=query) | Q(surname__icontains=query) | Q(user__username__icontains=query)
                | Q(user__email__icontains=query) | Q(phone__icontains=query)
            )
        context = {
            "page": Paginator(customers, self.per_page).get_page(request.GET.get("page")),
            "query": query,
            "ordering": ordering,
            "total": total,
            # Una scelta della community per ogni utente: la lista si legge una volta per pagina. all(): il
            # queryset del campo è uno solo per tutto il processo, e letto direttamente terrebbe in memoria le
            # community della prima pagina aperta, senza quelle create dopo
            "communities": list(CommunityAssignmentForm.base_fields["community"].queryset.all()),
        }
        return render(request, self.template_name, context)


class AssignCommunityView(TechnicianRequiredMixin, View):
    """Associa l'utente alla community scelta. Se la community non ha ancora utenti lui ne diventa il titolare:
    se gli mancano telefono o email, una pagina li chiede prima di associarlo."""

    template_name = "SP/owner_contacts.html"

    def post(self, request, customer_id):
        customer = get_object_or_404(Customer.objects.select_related("user"), pk=customer_id)
        form = CommunityAssignmentForm(request.POST)
        # Si torna alla stessa ricerca, con lo stesso ordinamento
        back = {name: request.POST.get(name, "").strip() for name in ("q", "sort")}
        back = {name: value for name, value in back.items() if value}
        if not form.is_valid():
            messages.error(request, f"Scegli la community a cui associare {customer}.")
            return self.back(back)

        community = form.cleaned_data["community"]
        if community.owner_id is None and customer.community_id is None:
            contacts = owner_contacts_form(customer, request.POST)
            if not contacts.is_valid():
                context = {"customer": customer, "community": community, "contacts_form": contacts, "back": back,
                           # Errori solo dopo che il tecnico ha provato a inserirli
                           "show_errors": f"{OWNER_PREFIX}-customer" in request.POST}
                return render(request, self.template_name, context)
            with transaction.atomic():
                contacts.save()
                assigned = assign_community(customer, community)
        else:
            assigned = assign_community(customer, community)
        if assigned:
            customer.refresh_from_db()
            owner = " e ne è il titolare" if customer.is_owner else ""
            messages.success(request, f"{customer} ora fa parte della community {community}{owner}.")
        else:
            customer.refresh_from_db()
            messages.info(request, f"{customer} è già nella community {customer.community}.")
        return self.back(back)

    @staticmethod
    def back(params):
        url = reverse("SP:customer_assignment")
        return redirect(f"{url}?{urlencode(params)}" if params else url)
