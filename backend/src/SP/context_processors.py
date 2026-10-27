"""Dati comuni a tutte le pagine."""
from .models import Customer, Intervention
from .roles import is_consultant, is_technician


def roles(request):
    """Ruoli dell'utente, per il menu: cliente, tecnico, consulente (gli amministratori sono tecnici e consulenti)."""
    user = request.user
    if not user.is_authenticated:
        return {}
    return {
        "is_customer": Customer.objects.filter(user=user).exists(),
        "is_technician": is_technician(user),
        "is_consultant": is_consultant(user),
    }


def pending_interventions(request):
    """Richieste di intervento da accettare, per il contatore nel menu dei tecnici."""
    if not is_technician(request.user):
        return {}
    return {"pending_interventions": Intervention.objects.filter(status=Intervention.Status.REQUESTED).count()}
