"""Filtri per mostrare lo stato degli interventi con lo stesso colore e la stessa icona in tutte le pagine."""
from django import template

from SP.models import Intervention

register = template.Library()

Status = Intervention.Status

# Classe del pallino colorato (app.css, .status) e icona (templates/partials/icons.html) di ogni stato
STYLES = {
    Status.REQUESTED: ("requested", "send"),
    Status.ACCEPTED: ("accepted", "user"),
    Status.DONE: ("done", "check"),
}


@register.filter
def status_badge(status):
    """Classe del pallino di uno stato dell'intervento."""
    return STYLES[status][0]


@register.filter
def status_icon(status):
    """Icona di uno stato dell'intervento."""
    return STYLES[status][1]


@register.filter
def person(user):
    """Nome e cognome dell'utente, o lo username se mancano; "-" se l'utente non c'è."""
    if user is None:
        return "-"
    return user.get_full_name() or user.username
