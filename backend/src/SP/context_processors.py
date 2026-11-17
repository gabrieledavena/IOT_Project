"""Dati comuni a tutte le pagine."""
from .models import Customer, Intervention
from .roles import is_consultant, is_technician


def roles(request):
    """Ruoli dell'utente, per il menu: cliente (e titolare della sua community), tecnico, consulente (gli
    amministratori sono tecnici e consulenti)."""
    user = request.user
    if not user.is_authenticated:
        return {}
    customer = Customer.objects.select_related("community").filter(user=user).first()
    # Solo con una community: chi è in attesa che un tecnico gliela assegni non ha ancora impianti da vedere
    is_customer = customer is not None and customer.community is not None
    return {
        "is_customer": is_customer,
        # Il titolare riceve gli avvisi dei guasti su Telegram
        "is_community_owner": is_customer and customer.is_owner,
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
