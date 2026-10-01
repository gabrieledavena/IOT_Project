"""Regole di accesso comuni alle pagine: chi vede quali impianti e quali pagine sono solo per lo staff."""
from django.contrib.auth.mixins import UserPassesTestMixin

from .models import Customer, PhotovoltaicSystem

NOT_A_CUSTOMER = "Utente non associato a un cliente."


class AccessError(Exception):
    """La pagina non si può mostrare: il messaggio compare nella pagina con lo status HTTP indicato."""

    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


class StaffRequiredMixin(UserPassesTestMixin):
    """Solo staff e amministratori: gli altri utenti ricevono 403, i visitatori vanno al login."""

    def test_func(self):
        return is_staff(self.request.user)


def is_staff(user):
    return user.is_staff or user.is_superuser


def visible_system(user, system_id):
    """Impianto che l'utente può vedere: staff e superuser tutti, i clienti quelli della propria community.

    Solleva AccessError se l'impianto non esiste o l'utente non può vederlo.
    """
    system = PhotovoltaicSystem.objects.select_related("community__city", "community__owner").filter(pk=system_id).first()
    if system is None:
        raise AccessError("Impianto non trovato.", status=404)
    if not is_staff(user):
        community = Customer.community_of(user)
        if community is None:
            raise AccessError(NOT_A_CUSTOMER, status=403)
        if community != system.community:
            raise AccessError("Non sei autorizzato a visualizzare questo impianto.", status=403)
    return system
