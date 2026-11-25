"""Tag e filtri dell'interfaccia: icone, voce attiva del menu e colore degli stati degli impianti."""
from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from SP.models import PhotovoltaicSystem

register = template.Library()

Status = PhotovoltaicSystem.Status

# Classe del pallino colorato (app.css, .status) per ogni esito del controllo automatico
STATUS_CLASSES = {
    Status.OK: "ok",
    Status.DIRTY: "dirty",
    Status.FAULT: "fault",
    Status.BROKEN: "broken",
}


@register.simple_tag
def icon(name, label=""):
    """Icona del disegno templates/partials/icons.html. Senza label è decorativa e i lettori di schermo la saltano."""
    if label:
        return format_html('<svg class="icon" role="img" aria-label="{}"><use href="#i-{}"></use></svg>', label, name)
    return format_html('<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-{}"></use></svg>', name)


@register.simple_tag(takes_context=True)
def current(context, *view_names):
    """aria-current="page" sulla voce del menu della pagina aperta: una delle viste indicate, o una che inizia con un
    nome che finisce con "_" (per esempio "SP:community_" vale per tutte le pagine delle community)."""
    match = getattr(context.get("request"), "resolver_match", None)
    view = match.view_name if match else ""
    for name in view_names:
        if view == name or (name.endswith("_") and view.startswith(name)):
            return mark_safe(' aria-current="page"')
    return ""


@register.filter
def status_class(status):
    """Classe del pallino dello stato di un impianto: "new" se non è ancora stato controllato."""
    return STATUS_CLASSES.get(status, "new")


@register.filter
def sky(cloud_cover):
    """Il cielo di una giornata in parole, dalla copertura nuvolosa media (%)."""
    try:
        cloud_cover = float(cloud_cover)
    except (TypeError, ValueError):
        return ""
    if cloud_cover != cloud_cover:  # NaN: dato mancante
        return ""
    if cloud_cover >= 80:
        return "Cielo coperto"
    if cloud_cover >= 50:
        return "Nuvoloso"
    if cloud_cover >= 20:
        return "Poco nuvoloso"
    return "Sereno"


@register.filter
def scale(value, bounds):
    """Posizione di value tra gli estremi "minimo,massimo", in percentuale da 0 a 100, per le barre di misura.

    Restituisce testo con il punto decimale, come lo vuole il CSS (un numero verrebbe scritto con la virgola)."""
    try:
        low, high = (float(bound) for bound in bounds.split(","))
        value = float(value)
    except (TypeError, ValueError):
        return "0"
    if value != value:  # NaN: dato mancante
        return "0"
    return f"{min(max((value - low) / (high - low) * 100, 0), 100):.1f}"


@register.filter
def to_city(name):
    """"a Modena", ma "ad Aosta": la preposizione davanti al nome di una città."""
    name = str(name)
    return f"ad {name}" if name[:1].lower() == "a" else f"a {name}"


# Il meteo previsto in parole, per ogni icona di forecast/views.py (weather_icon)
WEATHER_LABELS = {"sun": "Sereno", "partly": "Poco nuvoloso", "cloud": "Nuvoloso", "rain": "Pioggia", "snow": "Neve"}


@register.filter
def weather_label(icon_name):
    return WEATHER_LABELS.get(icon_name, "")
