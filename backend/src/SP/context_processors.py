"""Dati comuni a tutte le pagine."""
from .access import is_staff
from .models import Intervention


def pending_interventions(request):
    """Richieste di intervento da accettare, per il contatore nel menu dello staff."""
    if not is_staff(request.user):
        return {}
    return {"pending_interventions": Intervention.objects.filter(status=Intervention.Status.REQUESTED).count()}
