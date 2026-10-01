"""Filtri per mostrare lo stato degli interventi con lo stesso colore e la stessa icona in tutte le pagine."""
from django import template

from SP.models import Intervention

register = template.Library()

Status = Intervention.Status

STYLES = {
    Status.REQUESTED: ("bg-warning text-dark", "fa-paper-plane"),
    Status.ACCEPTED: ("bg-primary", "fa-user-check"),
    Status.DONE: ("bg-success", "fa-check-double"),
}


@register.filter
def status_badge(status):
    """Classi Bootstrap del badge di uno stato dell'intervento."""
    return STYLES[status][0]


@register.filter
def status_icon(status):
    """Icona Font Awesome di uno stato dell'intervento."""
    return STYLES[status][1]


@register.filter
def person(user):
    """Nome e cognome dell'utente, o lo username se mancano; "-" se l'utente non c'è."""
    if user is None:
        return "-"
    return user.get_full_name() or user.username
