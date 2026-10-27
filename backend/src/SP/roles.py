"""Ruoli del personale: i tecnici installano e gestiscono gli impianti, i consulenti preparano i preventivi.

Un ruolo è un gruppo di Django, che solo gli amministratori (superuser) assegnano, creando l'utente
nell'admin. Gli amministratori hanno tutti i ruoli. All'admin accedono solo loro e i tecnici, che vi
gestiscono impianti, dispositivi e interventi con i permessi del gruppo (migrazione 0012).
"""
TECHNICIANS = "Tecnici"
CONSULTANTS = "Consulenti"
ROLES = {TECHNICIANS: "Tecnico", CONSULTANTS: "Consulente"}


def has_role(user, group):
    """True se l'utente è nel gruppo del ruolo o è un amministratore."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    # I gruppi si leggono una volta sola per utente (cioè per richiesta)
    if not hasattr(user, "_role_names"):
        user._role_names = set(user.groups.values_list("name", flat=True))
    return group in user._role_names


def is_technician(user):
    return has_role(user, TECHNICIANS)


def is_consultant(user):
    return has_role(user, CONSULTANTS)
