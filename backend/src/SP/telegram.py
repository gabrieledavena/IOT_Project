"""Bot Telegram: avvisa il titolare di una community quando un suo impianto ha un guasto e gli dice lo stato degli impianti.

Collegamento: il titolare apre la pagina "Notifiche Telegram" del sito, dove un link apre il bot con un codice
(https://t.me/<bot>?start=<codice>). Quando preme "Avvia", Telegram manda al bot il messaggio "/start <codice>": il
server trova il cliente con quel codice e ne salva la chat. Solo da quel momento il bot può scrivergli, perché un bot
Telegram non può scrivere per primo a un utente.

Il server parla con Telegram solo con richieste HTTPS alla Bot API: invia i messaggi con sendMessage e legge quelli
ricevuti dal bot con getUpdates (long polling, comando telegram_bot). Così non serve un indirizzo pubblico, che
invece servirebbe a Telegram per chiamare il server (webhook).

Gli avvisi arrivano solo al titolare della community, e solo quando un impianto diventa un guasto confermato (BRK):
il probabile guasto (FLT) si risolve da solo in un'ora con il lavaggio dei pannelli, il guasto richiede un tecnico.

Il titolare chiede al bot lo stato degli impianti (/stato), il report di un giorno (/report) e l'ultima lettura di
un impianto (/lettura). Sotto il report e la lettura ci sono dei bottoni (inline keyboard): il giorno prima o dopo,
gli altri impianti. Un bottone premuto arriva al bot come callback_query con i suoi dati, per esempio "report:2026-11-16"
o "lettura:42", e il bot sostituisce il messaggio con quello nuovo invece di scriverne un altro.

Quando il tecnico propone un altro giorno per l'intervento (SP/interventions.py), il titolare riceve la proposta con
i bottoni per approvarla o rifiutarla; il rifiuto annulla la richiesta, quindi il bot chiede di confermarlo. Le
proposte ancora senza risposta sono anche in /intervento. Sul sito il titolare risponde dalla pagina della proposta.
"""
import logging
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import dateformat, timezone
from django.utils.html import escape
from django.utils.timesince import timesince
from django.views import View

from .access import NOT_A_CUSTOMER, AccessError
from .forms import (
    OWNER_WITHOUT_CONTACTS, InterventionRequestForm, intervention_date_range, owner_contacts_missing, request_unavailable,
)
from .models import Customer, Intervention, PhotovoltaicSystem
from .reports import day_report, latest_reading

logger = logging.getLogger(__name__)
Status = PhotovoltaicSystem.Status

API_URL = "https://api.telegram.org/bot{token}/{method}"
REQUEST_TIMEOUT = 10
# getUpdates tiene aperta la richiesta finché il bot riceve un messaggio o passano POLL_TIMEOUT secondi
POLL_TIMEOUT = 50

# Comandi mostrati da Telegram nel menu del bot (setMyCommands); /start lo usa il link del sito
COMMANDS = {
    "stato": "Stato degli impianti della community",
    "report": "Report di un giorno: /report, /report ieri o /report 15/11",
    "lettura": "Ultima lettura di un impianto",
    "intervento": "Chiedi l'intervento di un tecnico per un guasto o rispondi a una nuova data",
    "stop": "Non ricevere più gli avvisi",
    "aiuto": "Cosa sa fare il bot",
}
STATUS_ICONS = {Status.OK: "✅", Status.DIRTY: "🧹", Status.FAULT: "⚠️", Status.BROKEN: "🔴"}
# Tipi di bottone: i dati sono "<tipo>:<valore>", al massimo 64 byte per Telegram
REPORT_BUTTON = "report"
READING_BUTTON = "lettura"
# Richiesta di intervento: impianto (e settimana da mostrare), giorno scelto, invio senza note, annullamento
INTERVENTION_BUTTON = "intervento"  # intervento:<impianto>[:<primo giorno della settimana>]
DAY_BUTTON = "giorno"  # giorno:<impianto>:<giorno>
SEND_BUTTON = "invia"  # invia:<impianto>:<giorno>
CANCEL_BUTTON = "annulla"
# Nuovo giorno proposto dal tecnico: <tipo>:<intervento>:<giorno proposto>. Il giorno serve a riconoscere un
# bottone di una proposta superata da un'altra
PROPOSAL_BUTTON = "proposta"  # mostra la proposta
APPROVE_BUTTON = "approva"
REJECT_BUTTON = "rifiuta"  # chiede di confermare il rifiuto
CONFIRM_REJECT_BUTTON = "rifiuto"  # rifiuto confermato: la richiesta è annullata
PROPOSAL_BUTTONS = (PROPOSAL_BUTTON, APPROVE_BUTTON, REJECT_BUTTON, CONFIRM_REJECT_BUTTON)
DAYS_PER_PAGE = 7
# Dopo aver scelto il giorno il bot aspetta la nota per il tecnico per al massimo mezz'ora
NOTE_TIMEOUT = 30 * 60
# Ultima lettura più vecchia di così: il dispositivo probabilmente non sta inviando misure
STALE_READING = timedelta(minutes=10)
# Sotto questa quota di minuti con misure il report avverte che l'energia reale è più alta
MIN_REPORT_COVERAGE = 0.95
BAD_DATE = "Non capisco la data. Esempi: /report, /report ieri, /report 15/11, /report 15/11/2026."

NOT_LINKED = (
    "Questo account Telegram non è collegato a Solar Family. Il titolare di una community lo collega con il link "
    "della pagina <b>Notifiche Telegram</b> del sito."
)
NOT_OWNER = "Solo il titolare della community riceve gli avvisi e lo stato degli impianti su Telegram."
SERVER_ERROR = (
    "⚠️ Non sono riuscito a completare l'operazione per un errore del server: riprova tra poco. Se il problema "
    "continua, puoi fare lo stesso dal sito."
)


class TelegramError(Exception):
    """Telegram non ha risposto o ha rifiutato la richiesta; code è il codice di errore della Bot API, se c'è."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Reply:
    """Risposta del bot: testo in HTML e, se servono, righe di bottoni sotto il messaggio."""

    text: str
    buttons: list | None = None


def button(text, kind, value):
    return {"text": text, "callback_data": f"{kind}:{value}"}


def reply_markup(buttons):
    return {"reply_markup": {"inline_keyboard": buttons}} if buttons else {}


def enabled():
    """True se il bot è configurato: senza TELEGRAM_BOT_TOKEN il sito funziona come prima, ma senza avvisi."""
    return bool(settings.TELEGRAM_BOT_TOKEN)


def call(method, wait=REQUEST_TIMEOUT, **params):
    """Chiama un metodo della Bot API e ne restituisce il risultato; solleva TelegramError se non riesce."""
    url = API_URL.format(token=settings.TELEGRAM_BOT_TOKEN, method=method)
    try:
        data = requests.post(url, json=params, timeout=wait).json()
    except (requests.RequestException, ValueError) as error:
        # Solo il tipo di errore: il messaggio di requests contiene l'URL, cioè il token del bot
        raise TelegramError(f"{method}: {type(error).__name__}") from error
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description')}", data.get("error_code"))
    return data["result"]


def send_message(chat_id, text, buttons=None):
    """Invia un messaggio (in HTML, con i bottoni se ci sono) alla chat; restituisce True se Telegram l'ha accettato.

    Se l'utente ha bloccato il bot Telegram risponde 403: la chat si scollega, così non si riprova a ogni avviso.
    """
    try:
        call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True},
             **reply_markup(buttons))
    except TelegramError as error:
        logger.warning("Telegram message to chat %s not sent: %s", chat_id, error)
        if error.code == 403:
            unlink(chat_id)
        return False
    return True


def edit_message(chat_id, message_id, text, buttons=None):
    """Sostituisce testo e bottoni di un messaggio del bot, dopo che l'utente ha premuto uno dei suoi bottoni."""
    try:
        call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
             link_preview_options={"is_disabled": True}, **reply_markup(buttons))
    except TelegramError as error:
        # Lo stesso bottone premuto di nuovo senza dati nuovi: Telegram rifiuta un messaggio identico, va bene così
        if "message is not modified" not in str(error):
            logger.warning("Telegram message %s of chat %s not edited: %s", message_id, chat_id, error)


def get_updates(offset=None):
    """Messaggi ricevuti dal bot dopo `offset`. Chiedere quelli da `offset` conferma a Telegram quelli precedenti,
    che non tornano più; quelli non confermati (per esempio con il servizio fermo) restano per 24 ore."""
    params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
    if offset is not None:
        params["offset"] = offset
    return call("getUpdates", wait=POLL_TIMEOUT + REQUEST_TIMEOUT, **params)


def set_commands():
    """Registra i comandi del bot, che Telegram mostra nel menu della chat."""
    call("setMyCommands", commands=[{"command": name, "description": text} for name, text in COMMANDS.items()])


def site_url(path):
    return settings.SITE_URL.rstrip("/") + path


def link_url(customer):
    """Link che apre il bot con il codice del cliente. Il codice si crea la prima volta e vale finché non si usa."""
    if not customer.telegram_code:
        # 22 caratteri tra lettere, cifre, _ e -: quelli ammessi da Telegram nel parametro start
        customer.telegram_code = secrets.token_urlsafe(16)
        customer.save(update_fields=["telegram_code"])
    return f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={customer.telegram_code}"


def unlink(chat_id):
    """Scollega la chat dal cliente: non riceverà più avvisi. Restituisce True se era collegata."""
    return Customer.objects.filter(telegram_chat_id=chat_id).update(telegram_chat_id=None) > 0


def link(chat_id, code):
    """Collega la chat al titolare con il codice del link; restituisce la risposta del bot."""
    customer = Customer.objects.select_related("community__owner").filter(telegram_code=code).first()
    if customer is None:
        return "Codice non valido o già usato: apri di nuovo il link dalla pagina <b>Notifiche Telegram</b> del sito."
    if not customer.is_owner:
        return NOT_OWNER
    # Un account Telegram riceve gli avvisi di un solo titolare: se era collegato a un altro, lo si scollega
    Customer.objects.filter(telegram_chat_id=chat_id).exclude(pk=customer.pk).update(telegram_chat_id=None)
    customer.telegram_chat_id, customer.telegram_code = chat_id, None
    customer.save(update_fields=["telegram_chat_id", "telegram_code"])
    return (
        f"Ciao {escape(customer.name)}, Telegram è collegato a <b>{escape(customer.community.name)}</b>.\n\n"
        "Ti avviserò quando un impianto ha un guasto.\n\n" + commands_text()
    )


def commands_text():
    return "Comandi:\n" + "\n".join(f"/{name} - {text}" for name, text in COMMANDS.items())


def linked_customer(chat_id):
    return Customer.objects.select_related("community__owner").filter(telegram_chat_id=chat_id).first()


def answer(chat_id, text):
    """Risposta del bot a un messaggio ricevuto in chat privata."""
    command, _, argument = text.strip().partition(" ")
    # Nei menu Telegram può aggiungere il nome del bot al comando: /stato@SolarFamilyBot
    command = command.split("@")[0].lower()
    argument = argument.strip()
    if command == "/start" and argument:
        return Reply(link(chat_id, argument))

    customer = linked_customer(chat_id)
    if customer is None:
        return Reply(NOT_LINKED)
    if command == "/stop":
        unlink(chat_id)
        cache.delete(pending_key(chat_id))
        return Reply("Non riceverai più avvisi. Per riceverli di nuovo apri il link della pagina "
                     "<b>Notifiche Telegram</b> del sito.")
    if not customer.is_owner:
        return Reply(NOT_OWNER)
    if not command.startswith("/"):
        # Un testo dopo aver scelto il giorno dell'intervento è la nota per il tecnico
        pending = cache.get(pending_key(chat_id))
        if pending:
            return send_with_note(customer, chat_id, pending, text.strip())
    else:
        # Un comando annulla la nota attesa: un testo scritto più tardi non deve diventare una richiesta
        cache.delete(pending_key(chat_id))
    if command == "/intervento":
        return intervention_menu(customer)
    if command == "/stato":
        return Reply(status_text(customer.community))
    if command == "/report":
        day = parse_day(argument, timezone.now().date())
        return report_reply(customer.community, day) if day else Reply(BAD_DATE)
    if command == "/lettura":
        return reading_menu(customer.community)
    return Reply(commands_text())


def community_system(customer, value):
    """Impianto con id `value` della community del cliente, o None. Si controlla a ogni bottone, anche per quelli
    dei messaggi vecchi: nel frattempo l'impianto può essere stato spostato o eliminato."""
    return customer.community.photovoltaic_systems.filter(pk=int(value)).first() if value.isdigit() else None


def parse_iso_day(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def answer_button(chat_id, data, message_id=None):
    """Risposta del bot a un bottone premuto sotto il messaggio `message_id`."""
    customer = linked_customer(chat_id)
    if customer is None:
        return Reply(NOT_LINKED)
    if not customer.is_owner:
        return Reply(NOT_OWNER)
    kind, _, value = data.partition(":")
    if kind == REPORT_BUTTON:
        today = timezone.now().date()
        return report_reply(customer.community, min(parse_iso_day(value) or today, today))
    if kind == CANCEL_BUTTON:
        cache.delete(pending_key(chat_id))
        return Reply("Richiesta di intervento annullata.")
    if kind in PROPOSAL_BUTTONS:
        cache.delete(pending_key(chat_id))
        return answer_proposal_button(customer, kind, value)

    system_id, _, day = value.partition(":")
    system = community_system(customer, system_id)
    if kind == READING_BUTTON:
        return reading_reply(system) if system else Reply("Impianto non trovato: chiedi di nuovo con /lettura.")
    if kind not in (INTERVENTION_BUTTON, DAY_BUTTON, SEND_BUTTON):
        return Reply(commands_text())

    # Richiesta di intervento: a ogni passo si ricontrollano le regole, come fa il sito al momento dell'invio. Con
    # «Invia senza note» la richiesta in attesa della nota resta finché la richiesta non è inviata: se l'invio non
    # riesce per un errore, il titolare può riprovare o scrivere la nota
    if kind != SEND_BUTTON:
        cache.delete(pending_key(chat_id))
    if system is None:
        return Reply("Impianto non trovato: chiedi di nuovo con /intervento.")
    if owner_contacts_missing(customer.community, customer.user):
        return contacts_missing_reply()
    unavailable = request_unavailable(system)
    if unavailable:
        cache.delete(pending_key(chat_id))
        return already_requested_reply(system) or Reply(unavailable)
    day = parse_iso_day(day)
    if kind == INTERVENTION_BUTTON:
        return choose_day_reply(system, day)
    if day is None:
        return Reply("Giorno non valido: chiedi di nuovo con /intervento.")
    # Un bottone di un messaggio di ieri può avere un giorno non più ammesso
    form = request_form(day, "")
    if not form.is_valid():
        return form_errors(form)
    if kind == DAY_BUTTON:
        cache.set(pending_key(chat_id), {"system": system.id, "day": day.isoformat(), "message": message_id},
                  NOTE_TIMEOUT)
        return confirm_reply(system, day)
    reply = submit_request(customer, system, day, "")
    cache.delete(pending_key(chat_id))
    return reply


def handle_update(update):
    """Gestisce un aggiornamento di getUpdates: un messaggio al bot, un bottone premuto o il bot bloccato."""
    member = update.get("my_chat_member")
    if member:
        # L'utente ha bloccato il bot o cancellato la chat: non gli si può più scrivere
        if member["new_chat_member"]["status"] == "kicked":
            unlink(member["chat"]["id"])
        return
    query = update.get("callback_query")
    if query:
        handle_button(query)
        return
    message = update.get("message")
    # Solo le chat private: il bot non lavora nei gruppi
    if not message or message["chat"]["type"] != "private" or "text" not in message:
        return
    chat_id = message["chat"]["id"]
    with error_reply(chat_id):
        reply = answer(chat_id, message["text"])
    send_message(chat_id, reply.text, reply.buttons)


@contextmanager
def error_reply(chat_id):
    """Se la risposta non riesce per un errore, il titolare lo sa invece di non ricevere niente; l'errore prosegue
    fino al comando telegram_bot, che lo scrive nel log."""
    try:
        yield
    except Exception:
        send_message(chat_id, SERVER_ERROR)
        raise


def handle_button(query):
    """Bottone premuto sotto un messaggio del bot: il messaggio diventa il report o la lettura scelti."""
    try:
        # Conferma a Telegram che il bottone è stato ricevuto: sul telefono smette di mostrare l'attesa
        call("answerCallbackQuery", callback_query_id=query["id"])
    except TelegramError as error:
        logger.warning("Telegram button %s not confirmed: %s", query["id"], error)
    message = query.get("message")
    # Senza il messaggio (troppo vecchio) non c'è niente da aggiornare
    if not message or message["chat"]["type"] != "private":
        return
    chat_id = message["chat"]["id"]
    # Il messaggio con i bottoni resta com'era, così il titolare può riprovare
    with error_reply(chat_id):
        reply = answer_button(chat_id, query.get("data", ""), message["message_id"])
    edit_message(chat_id, message["message_id"], reply.text, reply.buttons)


def number(value, digits=1):
    """Numero all'italiana: 1.234,5."""
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def percent(share):
    return f"{share * 100:.0f}%"


def local_time(moment):
    return f"{timezone.localtime(moment):%H:%M}"


def parse_day(argument, today):
    """Giorno chiesto con /report: oggi, "ieri", "15/11" o "15/11/2026". None se non è una data o è nel futuro."""
    argument = argument.strip().lower()
    if argument in ("", "oggi"):
        return today
    if argument == "ieri":
        return today - timedelta(days=1)
    try:
        numbers = [int(part) for part in argument.replace("-", "/").replace(".", "/").split("/")]
        if len(numbers) == 2:
            day = date(today.year, numbers[1], numbers[0])
            # Senza anno è il giorno più recente con quella data: a gennaio "31/12" è dell'anno prima
            if day > today:
                day = date(today.year - 1, numbers[1], numbers[0])
        elif len(numbers) == 3:
            year = numbers[2] + 2000 if numbers[2] < 100 else numbers[2]
            day = date(year, numbers[1], numbers[0])
        else:
            return None
    except ValueError:
        return None
    return day if day <= today else None


def report_reply(community, day):
    """Report del giorno, con i bottoni per il giorno prima e (se non è oggi) quello dopo."""
    report = day_report(community, day)
    previous, following = day - timedelta(days=1), day + timedelta(days=1)
    buttons = [button(f"◀ {previous:%d/%m}", REPORT_BUTTON, previous.isoformat())]
    if following <= timezone.now().date():
        buttons.append(button(f"{following:%d/%m} ▶", REPORT_BUTTON, following.isoformat()))
    return Reply(report_text(report), [buttons])


def report_text(report):
    """Testo del report: energia, resa, picco, temperatura e CO₂ della community, poi impianto per impianto."""
    title = dateformat.format(report.day, "l j F Y")
    if not report.complete:
        title += f", fino alle {local_time(report.end)}"
    lines = [f"📊 <b>Report di {escape(report.community.name)}</b>", title, ""]
    if not any(item.readings for item in report.systems):
        lines.append("Nessuna misura in questo giorno.")
    else:
        energy = f"⚡ Energia prodotta: <b>{number(report.energy)} kWh</b>"
        if report.change is not None:
            compared = "a ieri alla stessa ora" if not report.complete else "al giorno prima"
            energy += f" ({report.change * 100:+.0f}% rispetto {compared})"
        lines.append(energy)
        if report.specific_yield is not None:
            lines.append(f"📈 Resa: {number(report.specific_yield, 2)} kWh per kW installato")
        if report.peak:
            lines.append(f"🔝 Picco: {number(report.peak)} kW alle {local_time(report.peak_at)}")
        if report.temperature_min is not None:
            lines.append(f"🌡 Temperatura: da {number(report.temperature_min)} a {number(report.temperature_max)} °C")
        lines.append(f"🌱 CO₂ evitata: {number(report.co2_kg)} kg")
        if report.coverage < MIN_REPORT_COVERAGE:
            lines.append(f"⚠️ Misure ricevute nel {percent(report.coverage)} dei minuti: l'energia prodotta è "
                         "probabilmente di più.")
        lines += ["", "<b>Impianti</b>"]
        for item in report.systems:
            line = f"• {escape(item.system.name)}: "
            if item.readings:
                line += f"<b>{number(item.energy)} kWh</b>"
                if item.specific_yield is not None:
                    line += f" ({number(item.specific_yield, 2)} kWh/kWp)"
                line += f", picco {number(item.peak, 2)} kW alle {local_time(item.peak_at)}"
            else:
                line += "nessuna misura"
            # Lo stato è quello di adesso, non del giorno del report: si segnala solo se c'è un problema
            if item.system.status != Status.OK:
                line += f" · {STATUS_ICONS.get(item.system.status, '')} {item.system.get_status_display()}"
            lines.append(line)
    lines += ["", f"Produzione e riepiloghi: {escape(site_url(reverse('SP:solar_community')))}"]
    return "\n".join(lines)


def system_buttons(systems, current=None):
    """Un bottone per impianto, due per riga; quello già mostrato serve ad aggiornare la lettura."""
    buttons = [button(f"🔄 {system.name}" if system == current else system.name, READING_BUTTON, system.id)
               for system in systems]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def reading_menu(community):
    """Risposta a /lettura: con più impianti si sceglie quale con i bottoni."""
    systems = list(community.photovoltaic_systems.order_by("name"))
    if not systems:
        return Reply("La community non ha ancora impianti.")
    if len(systems) == 1:
        return reading_reply(systems[0])
    return Reply("Di quale impianto vuoi l'ultima lettura?", system_buttons(systems))


def reading_reply(system):
    systems = list(system.community.photovoltaic_systems.order_by("name"))
    return Reply(reading_text(latest_reading(system)), system_buttons(systems, current=system))


def reading_text(latest):
    """Testo dell'ultima lettura: quando è arrivata, potenza, temperatura, luce, energia di oggi e stato."""
    system, reading = latest.system, latest.reading
    now = timezone.now()
    lines = [f"🔆 <b>{escape(system.name)}</b> ({escape(system.community.name)})"]
    if reading is None:
        lines.append("L'impianto non ha ancora inviato misure.")
    else:
        age = now - reading.time_stamp
        ago = "meno di un minuto fa" if age < timedelta(minutes=1) else f"{timesince(reading.time_stamp, now)} fa"
        lines.append(f"Ultima lettura: {timezone.localtime(reading.time_stamp):%d/%m alle %H:%M} ({ago})")
        if age > STALE_READING:
            lines.append("⚠️ Non arrivano letture più recenti: il dispositivo potrebbe essere spento o senza rete.")
        power = f"⚡ Potenza: <b>{number(reading.power, 2)} kW</b>"
        if latest.power_share is not None:
            power += f" ({percent(latest.power_share)} dei {number(system.max_power)} kWp installati)"
        lines += [
            "",
            power,
            f"🌡 Temperatura: {number(reading.temperature)} °C",
            f"💡 Luce: {number(reading.lightness, 0)} lux",
            f"📅 Energia prodotta oggi: {number(latest.today_energy)} kWh",
        ]
    lines.append(f"{STATUS_ICONS.get(system.status, '•')} Stato: {system.get_status_display()}")
    if latest.device is not None:
        if latest.device.online:
            lines.append("📡 Dispositivo collegato")
        elif latest.device.last_seen:
            lines.append(f"📡 Dispositivo scollegato, ultimo messaggio il "
                         f"{timezone.localtime(latest.device.last_seen):%d/%m alle %H:%M}")
        else:
            lines.append("📡 Dispositivo mai collegato")
    return "\n".join(lines)


def pending_key(chat_id):
    """Chiave in cache della richiesta di intervento che aspetta la nota del titolare (impianto, giorno, messaggio)."""
    return f"telegram:intervention:{chat_id}"


def system_label(system):
    return f"{STATUS_ICONS.get(system.status, '•')} {system.name}"


def open_interventions(community):
    """Richieste di intervento della community non ancora eseguite né annullate, dal giorno più vicino."""
    return (Intervention.objects.filter(system__community=community, status__in=Intervention.OPEN_STATUSES)
            .select_related("system").order_by("scheduled_date", "requested_at"))


def open_requests_text(interventions):
    """Le richieste aperte, come le vede il cliente nella pagina dell'impianto."""
    lines = []
    for intervention in interventions:
        line = f"• {escape(intervention.system.name)}: {intervention.get_status_display()}"
        if intervention.is_proposed:
            line += (f", il {intervention.proposed_date:%d/%m/%Y} invece del "
                     f"{intervention.scheduled_date:%d/%m/%Y}: aspetta la tua risposta")
        else:
            line += f" per il {intervention.scheduled_date:%d/%m/%Y}"
        lines.append(line)
    return "\n".join(["📋 <b>Richieste aperte</b>", *lines]) if lines else ""


def contacts_missing_text():
    """Il titolare di prima che telefono ed email fossero obbligatori li aggiunge dal profilo del sito."""
    return f"{escape(OWNER_WITHOUT_CONTACTS)}\n\nAggiungili dal profilo: {escape(site_url(reverse('SP:profile')))}"


def contacts_missing_reply():
    return Reply(contacts_missing_text())


def intervention_menu(customer):
    """Risposta a /intervento: gli impianti per cui si può chiedere un intervento, poi le richieste aperte con i
    bottoni per rispondere ai giorni proposti dai tecnici.

    Con un solo impianto da scegliere e nessuna proposta in attesa si passa subito alla scelta del giorno. Il
    titolare senza telefono ed email (di prima che fossero obbligatori) li aggiunge prima dal profilo del sito.
    """
    community = customer.community
    interventions = list(open_interventions(community))
    proposals = [intervention for intervention in interventions if intervention.is_proposed]
    if owner_contacts_missing(community, customer.user):
        systems = []
        lines = [contacts_missing_text()]
    else:
        systems = [system for system in community.photovoltaic_systems.order_by("name")
                   if request_unavailable(system) is None]
        if len(systems) == 1 and not proposals:
            return choose_day_reply(systems[0])
        if systems:
            lines = ["Per quale impianto vuoi chiedere l'intervento di un tecnico?"]
        else:
            lines = ["Nessun impianto ha un guasto per cui chiedere un intervento: lo puoi chiedere quando il "
                     "controllo automatico trova un probabile guasto o un guasto, e se non c'è già un intervento "
                     "per quel guasto."]
    buttons = [[button(system_label(system), INTERVENTION_BUTTON, system.id)] for system in systems]
    buttons += [[button(f"📅 Rispondi per {intervention.system.name}: {intervention.proposed_date:%d/%m}",
                        PROPOSAL_BUTTON, proposal_value(intervention))] for intervention in proposals]
    requests_text = open_requests_text(interventions)
    if requests_text:
        lines += ["", requests_text]
    return Reply("\n".join(lines), buttons or None)


def choose_day_reply(system, start=None):
    """Scelta del giorno dell'intervento, una settimana per volta, negli stessi giorni del calendario del sito."""
    first, last = intervention_date_range()
    start = min(max(start or first, first), last)
    days = [start + timedelta(days=offset) for offset in range(DAYS_PER_PAGE) if start + timedelta(days=offset) <= last]
    day_buttons = [button(f"{dateformat.format(day, 'D')} {day:%d/%m}", DAY_BUTTON, f"{system.id}:{day.isoformat()}")
                   for day in days]
    buttons = [row for row in (day_buttons[:4], day_buttons[4:]) if row]
    navigation = []
    if start > first:
        previous = max(start - timedelta(days=DAYS_PER_PAGE), first)
        navigation.append(button("◀ Settimana prima", INTERVENTION_BUTTON, f"{system.id}:{previous.isoformat()}"))
    following = start + timedelta(days=DAYS_PER_PAGE)
    if following <= last:
        navigation.append(button("Settimana dopo ▶", INTERVENTION_BUTTON, f"{system.id}:{following.isoformat()}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([button("Annulla", CANCEL_BUTTON, "")])
    text = (f"🛠 <b>Intervento per {escape(system.name)}</b> "
            f"({STATUS_ICONS.get(system.status, '')} {system.get_status_display()})\n\n"
            f"Scegli il giorno in cui un tecnico può intervenire, dal {first:%d/%m/%Y} al {last:%d/%m/%Y}.")
    return Reply(text, buttons)


def confirm_reply(system, day):
    """Giorno scelto: il titolare può scrivere una nota per il tecnico o inviare subito la richiesta."""
    first, _ = intervention_date_range()
    # "Cambia giorno" torna alla settimana in cui si era scelto il giorno
    week = first + timedelta(days=(day - first).days // DAYS_PER_PAGE * DAYS_PER_PAGE)
    text = (f"🛠 <b>Intervento per {escape(system.name)}</b> il {dateformat.format(day, 'l j F Y')}\n\n"
            "Se vuoi, scrivi ora una nota per il tecnico, per esempio gli orari in cui può accedere al tetto o i "
            "problemi notati sull'inverter: la richiesta partirà con la nota. Altrimenti premi «Invia senza note».")
    buttons = [
        [button("✅ Invia senza note", SEND_BUTTON, f"{system.id}:{day.isoformat()}")],
        [button("◀ Cambia giorno", INTERVENTION_BUTTON, f"{system.id}:{week.isoformat()}"),
         button("Annulla", CANCEL_BUTTON, "")],
    ]
    return Reply(text, buttons)


def request_form(day, notes):
    """Lo stesso form della pagina del sito: controlla il giorno (da domani a 60 giorni) e le note."""
    return InterventionRequestForm({"preferred_date": day.isoformat(), "customer_notes": notes})


def form_errors(form):
    errors = " ".join(error for field_errors in form.errors.values() for error in field_errors)
    return Reply(f"{escape(errors)} Chiedi di nuovo con /intervento.")


def already_requested_reply(system):
    """La richiesta aperta dell'impianto, o None: per esempio dopo aver premuto due volte «Invia senza note», o un
    bottone di un messaggio vecchio, il titolare la ritrova invece di un errore."""
    intervention = system.interventions.filter(status__in=Intervention.OPEN_STATUSES).order_by("-requested_at").first()
    if intervention is None:
        return None
    url = site_url(reverse("SP:photovoltaic_system", args=[system.id]))
    return Reply(
        f"La richiesta di intervento per <b>{escape(system.name)}</b> è già stata inoltrata: "
        f"{intervention.get_status_display()} per il {intervention.scheduled_date:%d/%m/%Y}.\n\n"
        f"Lo stato della richiesta è in /intervento e nella pagina dell'impianto: {escape(url)}"
    )


def submit_request(customer, system, day, notes):
    """Invia la richiesta con le stesse regole e lo stesso form del sito (SP/interventions.py)."""
    if owner_contacts_missing(customer.community, customer.user):
        return contacts_missing_reply()
    unavailable = request_unavailable(system)
    if unavailable:
        return already_requested_reply(system) or Reply(unavailable)
    form = request_form(day, notes)
    if not form.is_valid():
        return form_errors(form)
    intervention = form.save_request(system, customer.user)
    lines = [
        f"✅ Richiesta di intervento per <b>{escape(system.name)}</b> inoltrata per il "
        f"{intervention.preferred_date:%d/%m/%Y}: la vedrai accettata quando lo staff la prenderà in carico. Se il "
        "tecnico non è disponibile quel giorno, ti contatterà per accordarsi su un altro.",
    ]
    if intervention.customer_notes:
        lines += ["", f"Note per il tecnico: {escape(intervention.customer_notes)}"]
    url = site_url(reverse("SP:photovoltaic_system", args=[system.id]))
    lines += ["", f"Lo stato della richiesta è in /intervento e nella pagina dell'impianto: {escape(url)}"]
    return Reply("\n".join(lines))


def send_with_note(customer, chat_id, pending, notes):
    """Il titolare ha scritto la nota dopo aver scelto il giorno: si invia la richiesta con la nota.

    La richiesta in attesa della nota e i bottoni del messaggio precedente restano finché la richiesta non è
    inviata: se l'invio non riesce per un errore, il titolare può riprovare.
    """
    system = community_system(customer, str(pending["system"]))
    if system is None:
        cache.delete(pending_key(chat_id))
        return Reply("Impianto non trovato: chiedi di nuovo con /intervento.")
    day = date.fromisoformat(pending["day"])
    reply = submit_request(customer, system, day, notes)
    cache.delete(pending_key(chat_id))
    if pending.get("message"):
        # I bottoni del messaggio precedente non servono più: la risposta arriva come messaggio nuovo
        edit_message(chat_id, pending["message"],
                     f"🛠 <b>Intervento per {escape(system.name)}</b> il {dateformat.format(day, 'l j F Y')}: "
                     "nota ricevuta.")
    return reply


def proposal_value(intervention, day=None):
    return f"{intervention.pk}:{(day or intervention.proposed_date).isoformat()}"


def day_text(day):
    return dateformat.format(day, "l j F Y")


def proposal_text(intervention):
    """La proposta del tecnico: nuovo giorno, giorno che sostituisce, conseguenze di approvazione e rifiuto."""
    system = intervention.system
    staff = intervention.staff.get_full_name() or intervention.staff.username
    replaced = "il giorno richiesto" if not intervention.rescheduled else "il giorno concordato"
    lines = [
        "📅 <b>Nuova data per l'intervento</b>",
        "",
        f"{escape(staff)}, il tecnico che ha in carico la richiesta per <b>{escape(system.name)}</b> "
        f"({escape(system.community.name)}), propone di intervenire <b>{day_text(intervention.proposed_date)}</b> "
        f"invece di {day_text(intervention.scheduled_date)}, {replaced}.",
    ]
    if intervention.code:
        lines.append(f"Tipo di intervento: {intervention.get_code_display()}")
    url = site_url(reverse("SP:intervention_proposal", args=[intervention.pk]))
    lines += [
        "",
        "Se approvi, l'intervento resta con la nuova data. Se rifiuti, la richiesta di intervento viene annullata e "
        "per avere un tecnico dovrai aprirne una nuova.",
        "",
        f"Puoi rispondere anche dal sito: {escape(url)}",
    ]
    return "\n".join(lines)


def proposal_reply(intervention):
    value = proposal_value(intervention)
    return Reply(proposal_text(intervention), [[
        button(f"✅ Approva il {intervention.proposed_date:%d/%m}", APPROVE_BUTTON, value),
        button("❌ Rifiuta", REJECT_BUTTON, value),
    ]])


def proposal_state_text(intervention):
    """Lo stato di un intervento la cui proposta non aspetta più risposta: approvata, rifiutata o superata."""
    system = escape(intervention.system.name)
    if intervention.is_cancelled:
        return (f"La richiesta di intervento per <b>{system}</b> è stata annullata: hai rifiutato il giorno proposto. "
                "Se serve ancora un tecnico, aprine una nuova con /intervento.")
    if intervention.is_done:
        return f"L'intervento su <b>{system}</b> è stato eseguito il {intervention.executed_on:%d/%m/%Y}."
    return (f"Questa proposta non aspetta più la tua risposta: l'intervento su <b>{system}</b> è in programma "
            f"{day_text(intervention.scheduled_date)}.")


def answer_proposal_button(customer, kind, value):
    """Bottoni della proposta del tecnico: mostra, approva, rifiuta (con conferma) il nuovo giorno."""
    intervention_id, _, day = value.partition(":")
    intervention = (Intervention.objects.select_related("system__community", "staff")
                    .filter(pk=int(intervention_id), system__community=customer.community).first()
                    if intervention_id.isdigit() else None)
    day = parse_iso_day(day)
    if intervention is None or day is None:
        return Reply("Intervento non trovato: le richieste aperte sono in /intervento.")
    if not intervention.is_proposed:
        return Reply(proposal_state_text(intervention))
    if day != intervention.proposed_date:
        # Bottone di una proposta vecchia: il tecnico ne ha fatta un'altra, si mostra quella
        reply = proposal_reply(intervention)
        return Reply("⚠️ Il tecnico ha proposto un altro giorno.\n\n" + reply.text, reply.buttons)
    system = escape(intervention.system.name)
    if kind == PROPOSAL_BUTTON:
        return proposal_reply(intervention)
    if kind == REJECT_BUTTON:
        value = proposal_value(intervention)
        return Reply(
            f"Vuoi rifiutare il {day:%d/%m/%Y}? La richiesta di intervento per <b>{system}</b> sarà annullata e, "
            "se serve ancora un tecnico, dovrai aprirne una nuova.",
            [[button("Sì, rifiuta e annulla la richiesta", CONFIRM_REJECT_BUTTON, value)],
             [button("◀ Torna alla proposta", PROPOSAL_BUTTON, value)]],
        )
    approved = kind == APPROVE_BUTTON
    if not Intervention.answer_proposal(intervention.pk, day, approved=approved):
        # Nel frattempo ha risposto dal sito
        intervention.refresh_from_db()
        return Reply(proposal_state_text(intervention))
    if approved:
        return Reply(f"✅ Nuovo giorno approvato: l'intervento su <b>{system}</b> è {day_text(day)}.")
    reply = (f"Nuovo giorno rifiutato: la richiesta di intervento per <b>{system}</b> è annullata.\n\n"
             "Se serve ancora un tecnico, apri una nuova richiesta.")
    buttons = None
    if request_unavailable(intervention.system) is None:
        buttons = [[button("🛠 Nuova richiesta", INTERVENTION_BUTTON, intervention.system_id)]]
    return Reply(reply, buttons)


def notify_proposal(intervention_id):
    """Invia al titolare della community il nuovo giorno proposto dal tecnico, con i bottoni per rispondere.

    Riceve l'id e rilegge l'intervento, come notify_accepted. Restituisce True se il messaggio è stato inviato.
    """
    if not enabled():
        return False
    intervention = (Intervention.objects.select_related("system__community__owner", "staff")
                    .filter(pk=intervention_id, status=Intervention.Status.PROPOSED).first())
    if intervention is None or intervention.staff is None:
        return False
    owner = intervention.system.community.owner
    if owner is None or not owner.telegram_chat_id:
        return False
    reply = proposal_reply(intervention)
    return send_message(owner.telegram_chat_id, reply.text, reply.buttons)


def status_text(community):
    """Stato di ogni impianto della community, con la data dell'ultimo controllo automatico."""
    lines = [f"☀️ <b>Impianti di {escape(community.name)}</b>", ""]
    systems = list(community.photovoltaic_systems.order_by("name"))
    for system in systems:
        checked = (f"controllo del {timezone.localtime(system.last_check):%d/%m alle %H:%M}"
                   if system.last_check else "non ancora controllato")
        lines.append(f"{STATUS_ICONS.get(system.status, '•')} <b>{escape(system.name)}</b>: "
                     f"{system.get_status_display()} ({checked})")
    if not systems:
        lines.append("La community non ha ancora impianti.")
    lines += ["", f"Produzione e dettagli: {escape(site_url(reverse('SP:solar_community')))}"]
    return "\n".join(lines)


def broken_text(system):
    url = site_url(reverse("SP:photovoltaic_system", args=[system.id]))
    return (
        f"🔴 <b>Guasto all'impianto {escape(system.name)}</b> ({escape(system.community.name)})\n\n"
        "Negli ultimi giorni l'impianto ha prodotto meno del previsto e, anche dopo il lavaggio dei pannelli, "
        "produce meno degli impianti vicini: probabilmente c'è un guasto.\n\n"
        f"Puoi chiedere l'intervento di un tecnico dalla pagina dell'impianto: {escape(url)}"
    )


def notify_broken(systems):
    """Avvisa il titolare di ogni impianto tra `systems` che è un guasto confermato, se ha collegato Telegram.

    Restituisce il numero di messaggi inviati. Se Telegram non risponde l'avviso si perde, ma il controllo
    degli impianti continua: il guasto resta comunque visibile sul sito.
    """
    if not enabled():
        return 0
    broken = (PhotovoltaicSystem.objects
              .filter(pk__in=[system.pk for system in systems], status=Status.BROKEN)
              .select_related("community__owner").order_by("community__name", "name"))
    sent = 0
    for system in broken:
        owner = system.community.owner
        if owner and owner.telegram_chat_id and send_message(owner.telegram_chat_id, broken_text(system)):
            sent += 1
    return sent


def accepted_text(intervention):
    system = intervention.system
    staff = intervention.staff.get_full_name() or intervention.staff.username
    lines = [
        "🛠 <b>Richiesta di intervento accettata</b>",
        "",
        f"La richiesta per <b>{escape(system.name)}</b> ({escape(system.community.name)}) è stata presa in carico "
        f"da {escape(staff)}, per il giorno richiesto: {dateformat.format(intervention.preferred_date, 'l j F Y')}.",
    ]
    if intervention.code:
        lines.append(f"Tipo di intervento: {intervention.get_code_display()}")
    url = site_url(reverse("SP:photovoltaic_system", args=[system.id]))
    lines += ["", f"Lo stato della richiesta è in /intervento e nella pagina dell'impianto: {escape(url)}"]
    return "\n".join(lines)


def notify_accepted(intervention_id):
    """Avvisa il titolare della community che il tecnico ha accettato la richiesta di intervento.

    Riceve l'id e rilegge l'intervento: chi accetta lo aggiorna con una query (vedi SP/interventions.py), quindi
    l'oggetto in memoria non ha il tecnico e il tipo. L'avviso arriva al titolare anche se la richiesta l'ha
    fatta un altro utente della community dal sito. Restituisce True se il messaggio è stato inviato.
    """
    if not enabled():
        return False
    intervention = (Intervention.objects.select_related("system__community__owner", "staff")
                    .filter(pk=intervention_id, status=Intervention.Status.ACCEPTED).first())
    if intervention is None or intervention.staff is None:
        return False
    owner = intervention.system.community.owner
    if owner is None or not owner.telegram_chat_id:
        return False
    return send_message(owner.telegram_chat_id, accepted_text(intervention))


class TelegramView(LoginRequiredMixin, View):
    """Pagina "Notifiche Telegram" del titolare: collega o scollega il suo account Telegram."""

    template_name = "SP/telegram.html"

    def owner(self, request):
        customer = Customer.objects.select_related("community__owner").filter(user=request.user).first()
        if customer is None or customer.community is None:
            raise AccessError(NOT_A_CUSTOMER, status=403)
        if not customer.is_owner:
            raise AccessError(f"Gli avvisi su Telegram arrivano solo al titolare della community, "
                              f"{customer.community.owner}.", status=403)
        return customer

    def get(self, request):
        try:
            customer = self.owner(request)
        except AccessError as error:
            return render(request, self.template_name, {"error": error.message}, status=error.status)
        context = {
            "customer": customer,
            "enabled": enabled(),
            "bot_username": settings.TELEGRAM_BOT_USERNAME,
            "commands": COMMANDS,
            # Il codice serve solo finché l'account non è collegato
            "link_url": link_url(customer) if enabled() and not customer.telegram_chat_id else None,
        }
        return render(request, self.template_name, context)

    def post(self, request):
        try:
            customer = self.owner(request)
        except AccessError as error:
            return render(request, self.template_name, {"error": error.message}, status=error.status)
        chat_id = customer.telegram_chat_id
        if chat_id and unlink(chat_id):
            if enabled():
                send_message(chat_id, "Hai scollegato Telegram dal sito: non riceverai più avvisi.")
            messages.success(request, "Telegram scollegato: non riceverai più avvisi.")
        return redirect("SP:telegram")
