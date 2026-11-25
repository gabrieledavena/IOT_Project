"""Dati comuni a tutte le pagine."""
from .models import Customer, Intervention
from .roles import is_consultant, is_technician


def roles(request):
    """Ruoli dell'utente, per il menu: cliente, tecnico, consulente (gli amministratori sono tecnici e consulenti).

    Le notifiche Telegram del titolare non sono nel menu ma nel suo profilo (SP/views.py, ProfileView)."""
    user = request.user
    if not user.is_authenticated:
        return {}
    return {
        # Solo con una community: chi è in attesa che un tecnico gliela assegni non ha ancora impianti da vedere
        "is_customer": Customer.objects.filter(user=user, community__isnull=False).exists(),
        "is_technician": is_technician(user),
        "is_consultant": is_consultant(user),
    }


def pending_interventions(request):
    """Richieste di intervento da accettare e utenti da associare a una community, per i contatori nel menu
    dei tecnici."""
    if not is_technician(request.user):
        return {}
    return {
        "pending_interventions": Intervention.objects.filter(status=Intervention.Status.REQUESTED).count(),
        "pending_customers": Customer.objects.filter(community__isnull=True).count(),
    }


def owner_notices(request):
    """Avvisi in cima alle pagine del titolare di una community (templates/base.html): i giorni proposti dai tecnici
    che aspettano la sua risposta e, se è titolare da prima che fossero obbligatori, i recapiti che gli mancano.

    Non nelle pagine dove li gestisce già: la proposta e il profilo.
    """
    user = request.user
    if not user.is_authenticated:
        return {}
    customer = Customer.objects.select_related("community__owner", "user").filter(user=user).first()
    if customer is None or not customer.is_owner:
        return {}
    match = getattr(request, "resolver_match", None)
    view = match.view_name if match else ""
    notices = {}
    if view != "SP:intervention_proposal":
        proposals = (Intervention.objects.filter(system__community=customer.community_id,
                                                 status=Intervention.Status.PROPOSED)
                     .select_related("system").order_by("proposed_date"))
        notices["owner_proposals"] = list(proposals)
    if view != "SP:profile":
        notices["owner_without_contacts"] = not customer.has_contacts
    return notices
