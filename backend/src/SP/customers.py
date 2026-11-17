"""Utenti appena registrati, in attesa di una community: solo i tecnici li associano a quella del loro impianto.

Chi si registra, privato o di un'azienda, non sceglie la community: finché un tecnico non lo associa, la sua home
dice che la registrazione è riuscita e che si aspetta il tecnico. Il primo utente di una community ne diventa il
titolare (Customer.save). Le community le creano, modificano ed eliminano i tecnici (SP/communities.py).
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
from .forms import CommunityAssignmentForm
from .models import Customer
from .sorting import Ordering


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
        # Si torna alla stessa ricerca, con lo stesso ordinamento
        back = {name: request.POST.get(name, "").strip() for name in ("q", "sort")}
        back = {name: value for name, value in back.items() if value}
        url = reverse("SP:customer_assignment")
        return redirect(f"{url}?{urlencode(back)}" if back else url)
