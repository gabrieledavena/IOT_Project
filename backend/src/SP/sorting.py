"""Ordinamento delle tabelle: il campo "Ordina per" in alto a destra (templates/partials/sort_select.html).

Ogni tabella ha i suoi ordinamenti, {chiave: (etichetta, campi di order_by)}, e il primo è quello predefinito. La
scelta arriva con ?sort=<chiave> e resta nei link delle pagine, nei filtri e nelle ricerche della tabella.
"""
from django.db.models import Case, CharField, F, IntegerField, Q, Value, When
from django.db.models.functions import Concat, Lower, Trim

from .models import PhotovoltaicSystem

Status = PhotovoltaicSystem.Status
checked = Q(last_check__isnull=False)

# Gli impianti per stato, dal problema più grave; i mai controllati a parte, perché il loro stato è solo quello iniziale
SYSTEM_STATES = {
    "broken": checked & Q(status=Status.BROKEN),
    "fault": checked & Q(status=Status.FAULT),
    "dirty": checked & Q(status=Status.DIRTY),
    "ok": checked & Q(status=Status.OK),
    "new": Q(last_check__isnull=True),
}


def problems_first():
    """Impianti con un problema prima: guasto, probabile guasto, pannelli sporchi, a posto, mai controllati."""
    return Case(*(When(condition, then=Value(rank)) for rank, condition in enumerate(SYSTEM_STATES.values())),
                output_field=IntegerField())


def disconnected_first():
    """Impianti con il sensore scollegato prima, poi quelli senza sensore, poi quelli con il sensore collegato."""
    return Case(When(device__online=False, then=Value(0)), When(device__isnull=True, then=Value(1)),
                default=Value(2), output_field=IntegerField())


def person_name(field):
    """Il nome di un utente come lo mostra il filtro person: nome e cognome, o lo username se mancano."""
    full_name = Trim(Concat(F(f"{field}__first_name"), Value(" "), F(f"{field}__last_name"), output_field=CharField()))
    no_name = Q(**{f"{field}__first_name": "", f"{field}__last_name": ""})
    return Lower(Case(When(no_name, then=F(f"{field}__username")), default=full_name, output_field=CharField()))


class Ordering:
    """L'ordinamento scelto con ?sort= tra quelli di una tabella, o quello predefinito."""

    def __init__(self, request, options):
        self.options = options
        sort = request.GET.get("sort")
        self.key = sort if sort in options else next(iter(options))

    @property
    def fields(self):
        return self.options[self.key][1]

    @property
    def choices(self):
        return [(key, label) for key, (label, _) in self.options.items()]

    def apply(self, queryset):
        return queryset.order_by(*self.fields)
