from datetime import date, timedelta
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from SP import telegram
from SP.forms import NO_FAULT
from SP.models import Customer, Intervention, PhotovoltaicSystem
from SP.tests.helpers import (
    add_readings, create_community, create_customer, create_system, create_technician, install_device, utc,
)
from SP.tests.test_monitoring import MonitoringTestCase, washed

Status = PhotovoltaicSystem.Status

TOKEN = "123456:test-token"
BOT = override_settings(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_BOT_USERNAME="SolarFamilyTestBot",
                        SITE_URL="http://localhost:8000")
CHAT = 555001


class FakeBot:
    """Bot API finta: registra i messaggi inviati (sent) e tutte le chiamate (calls) invece di chiamare Telegram."""

    def __init__(self, testcase):
        self.sent = []
        self.calls = []
        patcher = mock.patch("SP.telegram.call", side_effect=self.call)
        patcher.start()
        testcase.addCleanup(patcher.stop)

    def call(self, method, wait=None, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.sent.append((params["chat_id"], params["text"]))
        return {}


def ask(chat_id, text):
    """Testo della risposta del bot al messaggio."""
    return telegram.answer(chat_id, text).text


def create_superuser():
    return User.objects.create_superuser("admin", password=None)


def customer_of(user):
    return Customer.objects.select_related("community__owner").get(user=user)


@BOT
class TelegramTestCase(TestCase):
    def setUp(self):
        self.bot = FakeBot(self)
        self.community = create_community("Rossi Trasporti")
        # Il primo utente della community ne diventa il titolare
        self.owner_user = create_customer(self.community, "titolare")
        self.member_user = create_customer(self.community, "dipendente")
        self.owner = customer_of(self.owner_user)
        self.member = customer_of(self.member_user)

    def link_owner(self, chat_id=CHAT):
        self.owner.telegram_chat_id = chat_id
        self.owner.save()


class LinkTests(TelegramTestCase):
    def test_the_owner_links_telegram_with_the_code_of_the_link(self):
        url = telegram.link_url(self.owner)
        code = url.rsplit("=", 1)[1]
        self.assertEqual(url, f"https://t.me/SolarFamilyTestBot?start={code}")
        # Lo stesso link finché non si usa
        self.assertEqual(telegram.link_url(customer_of(self.owner_user)), url)

        reply = ask(CHAT, f"/start {code}")

        self.owner.refresh_from_db()
        self.assertEqual((self.owner.telegram_chat_id, self.owner.telegram_code), (CHAT, None))
        self.assertIn("collegato a <b>Rossi Trasporti</b>", reply)
        self.assertIn("/stato", reply)

    def test_a_code_works_once(self):
        code = telegram.link_url(self.owner).rsplit("=", 1)[1]
        ask(CHAT, f"/start {code}")

        reply = ask(CHAT + 1, f"/start {code}")

        self.assertIn("Codice non valido o già usato", reply)
        self.assertFalse(Customer.objects.filter(telegram_chat_id=CHAT + 1).exists())

    def test_only_the_owner_can_link(self):
        code = telegram.link_url(self.member).rsplit("=", 1)[1]

        self.assertEqual(ask(CHAT, f"/start {code}"), telegram.NOT_OWNER)
        self.member.refresh_from_db()
        self.assertIsNone(self.member.telegram_chat_id)

    def test_an_account_receives_the_alerts_of_one_owner(self):
        other = customer_of(create_customer(create_community("Bianchi"), "bianchi"))
        other.telegram_chat_id = CHAT
        other.save()

        ask(CHAT, f"/start {telegram.link_url(self.owner).rsplit('=', 1)[1]}")

        other.refresh_from_db()
        self.owner.refresh_from_db()
        self.assertIsNone(other.telegram_chat_id)
        self.assertEqual(self.owner.telegram_chat_id, CHAT)


class CommandTests(TelegramTestCase):
    def test_status_lists_the_systems_of_the_community(self):
        self.link_owner()
        sede = create_system(self.community, "Sede")
        sede.set_status(Status.OK, utc(2026, 11, 17, 9, 30))
        magazzino = create_system(self.community, "Magazzino")
        magazzino.set_status(Status.BROKEN, utc(2026, 11, 16, 15, 0))
        create_system(self.community, "Negozio")
        create_system(create_community("Altra community"), "Non sua")

        reply = ask(CHAT, "/stato")

        self.assertIn("Impianti di Rossi Trasporti", reply)
        self.assertIn("🔴 <b>Magazzino</b>: Guasto (controllo del 16/11 alle 15:00)", reply)
        self.assertIn("✅ <b>Sede</b>: OK (controllo del 17/11 alle 09:30)", reply)
        self.assertIn("<b>Negozio</b>: OK (non ancora controllato)", reply)
        self.assertNotIn("Non sua", reply)
        self.assertLess(reply.index("Magazzino"), reply.index("Negozio"))
        self.assertIn("http://localhost:8000/sp/community/", reply)

    def test_commands_with_the_name_of_the_bot_from_the_menu(self):
        self.link_owner()
        self.assertIn("Impianti di Rossi Trasporti", ask(CHAT, "/stato@SolarFamilyTestBot"))

    def test_without_a_linked_account_the_bot_explains_how_to_link_it(self):
        self.assertEqual(ask(CHAT, "/stato"), telegram.NOT_LINKED)
        self.assertEqual(ask(CHAT, "/start"), telegram.NOT_LINKED)

    def test_a_former_owner_does_not_see_the_status(self):
        self.link_owner()
        self.community.owner = self.member
        self.community.save()

        self.assertEqual(ask(CHAT, "/stato"), telegram.NOT_OWNER)

    def test_stop_unlinks_the_account(self):
        self.link_owner()

        reply = ask(CHAT, "/stop")

        self.assertIn("Non riceverai più avvisi", reply)
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.telegram_chat_id)

    def test_other_messages_get_the_list_of_commands(self):
        self.link_owner()
        reply = ask(CHAT, "ciao")
        for command in ("/stato", "/stop", "/aiuto"):
            self.assertIn(command, reply)

    def test_private_messages_get_an_answer(self):
        self.link_owner()

        telegram.handle_update({"update_id": 1, "message": {"chat": {"id": CHAT, "type": "private"}, "text": "/stato"}})
        # Gruppi e messaggi senza testo (foto, adesivi) si ignorano
        telegram.handle_update({"update_id": 2, "message": {"chat": {"id": -10, "type": "group"}, "text": "/stato"}})
        telegram.handle_update({"update_id": 3, "message": {"chat": {"id": CHAT, "type": "private"}, "sticker": {}}})

        [(chat_id, text)] = self.bot.sent
        self.assertEqual(chat_id, CHAT)
        self.assertIn("Impianti di Rossi Trasporti", text)

    def test_blocking_the_bot_unlinks_the_account(self):
        self.link_owner()

        telegram.handle_update({"update_id": 1, "my_chat_member": {
            "chat": {"id": CHAT, "type": "private"}, "new_chat_member": {"status": "kicked"},
        }})

        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.telegram_chat_id)


@BOT
class BotApiTests(TestCase):
    def test_send_message_uses_html_without_link_previews(self):
        with mock.patch("SP.telegram.requests.post") as post:
            post.return_value.json.return_value = {"ok": True, "result": {}}
            self.assertTrue(telegram.send_message(CHAT, "<b>Ciao</b>"))

        self.assertEqual(post.call_args.args[0], f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(post.call_args.kwargs["json"], {
            "chat_id": CHAT, "text": "<b>Ciao</b>", "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
        })

    def test_errors_do_not_show_the_token(self):
        error = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/getUpdates")
        with mock.patch("SP.telegram.requests.post", side_effect=error):
            with self.assertRaises(telegram.TelegramError) as raised:
                telegram.get_updates()
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_a_blocked_bot_unlinks_the_account(self):
        customer = customer_of(create_customer(create_community(), "titolare"))
        customer.telegram_chat_id = CHAT
        customer.save()
        with mock.patch("SP.telegram.requests.post") as post:
            post.return_value.json.return_value = {
                "ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user",
            }
            self.assertFalse(telegram.send_message(CHAT, "Ciao"))

        customer.refresh_from_db()
        self.assertIsNone(customer.telegram_chat_id)


class FaultAlertTests(TelegramTestCase):
    def test_only_the_owner_gets_the_alert_of_a_confirmed_fault(self):
        self.link_owner()
        self.member.telegram_chat_id = CHAT + 1
        self.member.save()
        broken = create_system(self.community, "Magazzino")
        broken.set_status(Status.BROKEN, timezone.now())
        fault = create_system(self.community, "Negozio")
        fault.set_status(Status.FAULT, timezone.now())

        self.assertEqual(telegram.notify_broken([broken, fault]), 1)

        [(chat_id, text)] = self.bot.sent
        self.assertEqual(chat_id, CHAT)
        self.assertIn("Guasto all'impianto Magazzino</b> (Rossi Trasporti)", text)
        self.assertIn("visita la pagina dedicata all'impianto sul sito.", text)
        self.assertNotIn("http", text)

    def test_no_alert_without_a_linked_account(self):
        broken = create_system(self.community, "Magazzino")
        broken.set_status(Status.BROKEN, timezone.now())

        self.assertEqual(telegram.notify_broken([broken]), 0)
        self.assertEqual(self.bot.sent, [])

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_no_alert_without_the_bot(self):
        self.link_owner()
        broken = create_system(self.community, "Magazzino")
        broken.set_status(Status.BROKEN, timezone.now())

        self.assertEqual(telegram.notify_broken([broken]), 0)
        self.assertEqual(self.bot.sent, [])


class ManualStatusAlertTests(TelegramTestCase):
    """Anche lo stato cambiato a mano (admin, pagina dell'installazione) avvisa il titolare di un nuovo guasto."""

    def setUp(self):
        super().setUp()
        self.link_owner()
        self.system = create_system(self.community, "Magazzino")
        self.admin = create_superuser()

    def change_in_admin(self, status):
        self.client.force_login(self.admin)
        data = {"name": self.system.name, "max_power": self.system.max_power, "brand": self.system.brand,
                "community": self.community.id, "status": status, "last_check_0": "", "last_check_1": ""}
        with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/admin/SP/photovoltaicsystem/{self.system.id}/change/", data)
        self.assertEqual(response.status_code, 302)

    def test_a_fault_set_in_the_admin_alerts_the_owner(self):
        self.change_in_admin("BRK")

        [(chat_id, text)] = self.bot.sent
        self.assertEqual(chat_id, CHAT)
        self.assertIn("Guasto all'impianto Magazzino", text)

    def test_other_statuses_and_other_changes_in_the_admin_do_not(self):
        self.change_in_admin("FLT")
        self.system.status = Status.BROKEN
        self.system.save()
        self.system.max_power = 9.5
        self.change_in_admin("BRK")  # stato non cambiato: solo la potenza

        self.assertEqual(self.bot.sent, [])

    def test_a_fault_forced_by_a_technician_alerts_the_owner_each_time(self):
        self.client.force_login(create_technician())
        url = reverse("SP:installation_detail", args=[self.system.id])
        for status in ("BRK", "BRK", "OK"):
            with mock.patch("SP.mqtt.publish"), self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, {"action": "force_status", "status": status})

        self.assertEqual([chat_id for chat_id, _ in self.bot.sent], [CHAT, CHAT])


@BOT
class CheckSystemsAlertTests(MonitoringTestCase):
    def test_check_systems_alerts_the_owner_when_the_washing_confirms_the_fault(self):
        bot = FakeBot(self)
        owner = customer_of(create_customer(self.community, "titolare"))
        owner.telegram_chat_id = CHAT
        owner.save()
        cleaned_at = timezone.now().replace(second=0, microsecond=0) - timedelta(hours=3)
        sede = create_system(self.community, "Sede", max_power=1.0)
        add_readings(sede, cleaned_at, [0.5] * 120)
        sede.last_check = timezone.now()
        sede.save()
        system = create_system(self.community, "Capannone", max_power=1.0)
        add_readings(system, cleaned_at, [0.2] * 120)
        washed(system, cleaned_at)

        out = StringIO()
        with mock.patch("SP.mqtt.publish", return_value=True):
            call_command("check_systems", stdout=out)

        [(chat_id, text)] = bot.sent
        self.assertEqual(chat_id, CHAT)
        self.assertIn("Guasto all'impianto Capannone", text)
        self.assertIn("Fault alert sent on Telegram to the owners of 1 systems.", out.getvalue())

        # Ai controlli successivi resta un guasto, ma l'avviso non si ripete
        with mock.patch("SP.mqtt.publish", return_value=True):
            call_command("check_systems", "--all", stdout=StringIO())
        self.assertEqual(len(bot.sent), 1)


NOW = utc(2026, 11, 17, 15, 0)


class BotDataTestCase(TelegramTestCase):
    """Titolare collegato, due impianti e l'ora ferma alle 15 del 17/11/2026."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("django.utils.timezone.now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.link_owner()
        self.sede = create_system(self.community, "Sede", max_power=4.0)
        self.magazzino = create_system(self.community, "Magazzino", max_power=2.0)

    def press(self, data, chat_id=CHAT):
        """Preme un bottone sotto il messaggio 9 del bot; restituisce i parametri del messaggio aggiornato."""
        telegram.handle_update({"update_id": 1, "callback_query": {
            "id": "query-1", "data": data, "message": {"message_id": 9, "chat": {"id": chat_id, "type": "private"}},
        }})
        self.assertEqual(self.bot.calls[0], ("answerCallbackQuery", {"callback_query_id": "query-1"}))
        [(method, params)] = self.bot.calls[1:]
        self.assertEqual((method, params["chat_id"], params["message_id"]), ("editMessageText", chat_id, 9))
        return params


def button_data(buttons):
    return [[(button["text"], button["callback_data"]) for button in row] for row in buttons]


class ReportCommandTests(BotDataTestCase):
    def test_parse_day(self):
        today = date(2026, 1, 10)
        cases = {
            "": today, "oggi": today, "Ieri": date(2026, 1, 9), "5/1": date(2026, 1, 5),
            # Senza anno è il giorno più recente con quella data
            "31/12": date(2025, 12, 31), "11/1": date(2025, 1, 11),
            "15/11/2025": date(2025, 11, 15), "15-11-25": date(2025, 11, 15), "15.11.2025": date(2025, 11, 15),
            "11/1/2026": None, "31/02": None, "domani": None, "1/2/3/4": None,
        }
        for argument, day in cases.items():
            with self.subTest(argument=argument):
                self.assertEqual(telegram.parse_day(argument, today), day)

    def test_report_of_today(self):
        add_readings(self.sede, utc(2026, 11, 17, 14, 0), [3.0] * 60)

        reply = telegram.answer(CHAT, "/report")

        for text in ("📊 <b>Report di Rossi Trasporti</b>", "Martedì 17 Novembre 2026, fino alle 15:00",
                     "⚡ Energia prodotta: <b>3,0 kWh</b>\n", "📈 Resa: 0,50 kWh per kW installato",
                     "🔝 Picco: 3,0 kW alle 14:00", "🌡 Temperatura: da 20,0 a 20,0 °C", "CO₂ evitata: 0,8 kg",
                     "⚠️ Misure ricevute nel 3% dei minuti", "• Magazzino: nessuna misura\n",
                     "• Sede: <b>3,0 kWh</b> (0,75 kWh/kWp), picco 3,00 kW alle 14:00",
                     "http://localhost:8000/sp/community/"):
            self.assertIn(text, reply.text)
        # Oggi non c'è il giorno dopo
        self.assertEqual(button_data(reply.buttons), [[("◀ 16/11", "report:2026-11-16")]])

    def test_report_of_a_past_day_compared_with_the_day_before(self):
        add_readings(self.sede, utc(2026, 11, 16, 10, 0), [3.0] * 60)
        add_readings(self.sede, utc(2026, 11, 15, 10, 0), [2.0] * 60)
        self.magazzino.set_status(Status.BROKEN, NOW)

        reply = telegram.answer(CHAT, "/report ieri")

        self.assertIn("Lunedì 16 Novembre 2026\n", reply.text)
        self.assertIn("<b>3,0 kWh</b> (+50% rispetto al giorno prima)", reply.text)
        # Lo stato di adesso si segnala solo se c'è un problema
        self.assertIn("• Magazzino: nessuna misura · 🔴 Guasto\n", reply.text)
        self.assertNotIn("OK", reply.text)
        self.assertEqual(button_data(reply.buttons),
                         [[("◀ 15/11", "report:2026-11-15"), ("17/11 ▶", "report:2026-11-17")]])

    def test_a_day_without_readings_and_a_wrong_date(self):
        self.assertIn("Nessuna misura in questo giorno.", ask(CHAT, "/report 1/11"))
        self.assertEqual(ask(CHAT, "/report 18/11/2026"), telegram.BAD_DATE)

    def test_the_buttons_move_the_report_to_another_day(self):
        add_readings(self.sede, utc(2026, 11, 15, 10, 0), [3.0] * 60)

        params = self.press("report:2026-11-15")

        self.assertIn("Domenica 15 Novembre 2026", params["text"])
        self.assertEqual(params["parse_mode"], "HTML")
        self.assertEqual(button_data(params["reply_markup"]["inline_keyboard"]),
                         [[("◀ 14/11", "report:2026-11-14"), ("16/11 ▶", "report:2026-11-16")]])

    def test_a_future_or_wrong_day_in_a_button_is_today(self):
        self.assertIn("Martedì 17 Novembre 2026", self.press("report:2026-12-25")["text"])
        self.bot.calls.clear()
        self.assertIn("Martedì 17 Novembre 2026", self.press("report:xyz")["text"])

    def test_numbers_in_italian(self):
        self.assertEqual(telegram.number(1234.56), "1.234,6")
        self.assertEqual(telegram.number(0.5, 2), "0,50")


class ReadingCommandTests(BotDataTestCase):
    def test_with_several_systems_the_owner_chooses_one(self):
        reply = telegram.answer(CHAT, "/lettura")

        self.assertEqual(reply.text, "Di quale impianto vuoi l'ultima lettura?")
        self.assertEqual(button_data(reply.buttons),
                         [[("Magazzino", f"lettura:{self.magazzino.id}"), ("Sede", f"lettura:{self.sede.id}")]])

    def test_the_last_reading_of_the_chosen_system(self):
        add_readings(self.sede, utc(2026, 11, 17, 14, 0), [3.0] * 60, temperature=21.5, lightness=640.0)

        params = self.press(f"lettura:{self.sede.id}")

        # timesince separa numero e unità con uno spazio non separabile (\xa0)
        for text in ("🔆 <b>Sede</b> (Rossi Trasporti)", "Ultima lettura: 17/11 alle 14:59 (1\xa0minuto fa)",
                     "⚡ Potenza: <b>3,00 kW</b> (75% dei 4,0 kWp installati)", "🌡 Temperatura: 21,5 °C",
                     "💡 Luce: 640 lux", "📅 Energia prodotta oggi: 3,0 kWh", "✅ Stato: OK"):
            self.assertIn(text, params["text"])
        self.assertNotIn("⚠️", params["text"])
        self.assertNotIn("Dispositivo", params["text"])  # nessun dispositivo installato
        # Il bottone dell'impianto mostrato aggiorna la lettura, l'altro passa all'altro impianto
        self.assertEqual(button_data(params["reply_markup"]["inline_keyboard"]),
                         [[("Magazzino", f"lettura:{self.magazzino.id}"), ("🔄 Sede", f"lettura:{self.sede.id}")]])

    def test_an_old_reading_and_a_disconnected_device(self):
        add_readings(self.magazzino, utc(2026, 11, 17, 12, 0), [1.0])
        device, _ = install_device(self.magazzino)
        device.last_seen = utc(2026, 11, 17, 12, 1)
        device.save()

        text = self.press(f"lettura:{self.magazzino.id}")["text"]

        self.assertIn("(3\xa0ore fa)", text)
        self.assertIn("⚠️ Non arrivano letture più recenti", text)
        self.assertIn("📡 Dispositivo scollegato, ultimo messaggio il 17/11 alle 12:01", text)

    def test_with_one_system_the_reading_comes_at_once(self):
        self.magazzino.delete()

        reply = telegram.answer(CHAT, "/lettura")

        self.assertIn("🔆 <b>Sede</b>", reply.text)
        self.assertIn("L'impianto non ha ancora inviato misure.", reply.text)
        self.assertEqual(button_data(reply.buttons), [[("🔄 Sede", f"lettura:{self.sede.id}")]])

    def test_only_the_systems_of_the_community(self):
        other = create_system(create_community("Bianchi"), "Non suo")

        text = self.press(f"lettura:{other.id}")["text"]

        self.assertEqual(text, "Impianto non trovato: chiedi di nuovo con /lettura.")

    def test_the_buttons_need_the_owner(self):
        self.assertEqual(self.press(f"lettura:{self.sede.id}", chat_id=CHAT + 1)["text"], telegram.NOT_LINKED)

    def test_a_message_already_up_to_date_is_not_an_error(self):
        error = telegram.TelegramError("editMessageText: Bad Request: message is not modified", 400)
        with mock.patch("SP.telegram.call", side_effect=error), self.assertNoLogs("SP.telegram", "WARNING"):
            telegram.edit_message(CHAT, 9, "Testo")


class InterventionRequestTests(BotDataTestCase):
    """Richiesta di intervento dal bot: stesse regole della pagina del sito (da domani a 60 giorni, note facoltative)."""

    def setUp(self):
        super().setUp()
        # La nota attesa dopo la scelta del giorno sta in cache: ogni test parte senza
        cache.clear()
        self.addCleanup(cache.clear)
        self.sede.set_status(Status.FAULT, NOW)

    def choose_day(self, day="2026-11-20", system=None):
        self.bot.calls.clear()
        return self.press(f"giorno:{(system or self.sede).id}:{day}")

    def test_with_one_fault_the_owner_chooses_the_day_at_once(self):
        reply = telegram.answer(CHAT, "/intervento")

        self.assertIn("🛠 <b>Intervento per Sede</b> (⚠️ Probabile guasto)", reply.text)
        self.assertIn("dal 18/11/2026 al 16/01/2027", reply.text)
        self.assertEqual(button_data(reply.buttons), [
            [("Mer 18/11", f"giorno:{self.sede.id}:2026-11-18"), ("Gio 19/11", f"giorno:{self.sede.id}:2026-11-19"),
             ("Ven 20/11", f"giorno:{self.sede.id}:2026-11-20"), ("Sab 21/11", f"giorno:{self.sede.id}:2026-11-21")],
            [("Dom 22/11", f"giorno:{self.sede.id}:2026-11-22"), ("Lun 23/11", f"giorno:{self.sede.id}:2026-11-23"),
             ("Mar 24/11", f"giorno:{self.sede.id}:2026-11-24")],
            [("Settimana dopo ▶", f"intervento:{self.sede.id}:2026-11-25")],
            [("Annulla", "annulla:")],
        ])

    def test_with_several_faults_the_owner_chooses_the_system(self):
        self.magazzino.set_status(Status.BROKEN, NOW)

        reply = telegram.answer(CHAT, "/intervento")

        self.assertEqual(reply.text, "Per quale impianto vuoi chiedere l'intervento di un tecnico?")
        self.assertEqual(button_data(reply.buttons), [[("🔴 Magazzino", f"intervento:{self.magazzino.id}")],
                                                      [("⚠️ Sede", f"intervento:{self.sede.id}")]])
        self.assertIn("Intervento per Magazzino", self.press(f"intervento:{self.magazzino.id}")["text"])

    def test_weeks_of_the_calendar(self):
        def days_and_navigation(start):
            self.bot.calls.clear()
            rows = button_data(self.press(f"intervento:{self.sede.id}:{start}")["reply_markup"]["inline_keyboard"])
            return [text for row in rows[:-2] for text, _ in row], rows[-2]

        days, navigation = days_and_navigation("2026-11-25")
        self.assertEqual((days[0], days[-1]), ("Mer 25/11", "Mar 01/12"))
        self.assertEqual(navigation, [("◀ Settimana prima", f"intervento:{self.sede.id}:2026-11-18"),
                                      ("Settimana dopo ▶", f"intervento:{self.sede.id}:2026-12-02")])
        # L'ultima settimana si ferma al sessantesimo giorno
        days, navigation = days_and_navigation("2027-01-13")
        self.assertEqual(days, ["Mer 13/01", "Gio 14/01", "Ven 15/01", "Sab 16/01"])
        self.assertEqual(navigation, [("◀ Settimana prima", f"intervento:{self.sede.id}:2027-01-06")])

    def test_request_without_notes(self):
        params = self.choose_day()
        self.assertIn("<b>Intervento per Sede</b> il Venerdì 20 Novembre 2026", params["text"])
        self.assertEqual(button_data(params["reply_markup"]["inline_keyboard"]), [
            [("✅ Invia senza note", f"invia:{self.sede.id}:2026-11-20")],
            [("◀ Cambia giorno", f"intervento:{self.sede.id}:2026-11-18"), ("Annulla", "annulla:")],
        ])

        self.bot.calls.clear()
        params = self.press(f"invia:{self.sede.id}:2026-11-20")

        self.assertIn("✅ Richiesta di intervento per <b>Sede</b> inoltrata per il 20/11/2026", params["text"])
        self.assertIn(f"http://localhost:8000/sp/system/{self.sede.id}/", params["text"])
        self.assertNotIn("reply_markup", params)
        [intervention] = Intervention.objects.all()
        self.assertEqual((intervention.system, intervention.requested_by, intervention.status),
                         (self.sede, self.owner_user, Intervention.Status.REQUESTED))
        self.assertEqual((intervention.preferred_date, intervention.customer_notes), (date(2026, 11, 20), ""))
        # Ora c'è una richiesta aperta: non se ne può chiedere un'altra, e /intervento la mostra
        reply = ask(CHAT, "/intervento")
        self.assertIn("Nessun impianto ha un guasto per cui chiedere un intervento", reply)
        self.assertIn("📋 <b>Richieste aperte</b>\n• Sede: Richiesta inoltrata per il 20/11/2026", reply)

    def test_request_with_the_note_written_after_choosing_the_day(self):
        self.choose_day()
        self.bot.calls.clear()

        reply = ask(CHAT, "Accesso al tetto dalle 9 alle 12 <citofono>")

        self.assertIn("inoltrata per il 20/11/2026", reply)
        self.assertIn("Note per il tecnico: Accesso al tetto dalle 9 alle 12 &lt;citofono&gt;", reply)
        self.assertEqual(Intervention.objects.get().customer_notes, "Accesso al tetto dalle 9 alle 12 <citofono>")
        # Il messaggio con i bottoni perde i bottoni
        [(method, params)] = self.bot.calls
        self.assertEqual((method, params["message_id"]), ("editMessageText", 9))
        self.assertIn("nota ricevuta", params["text"])
        self.assertNotIn("reply_markup", params)
        # Un altro testo non è più una nota
        self.assertIn("Comandi:", ask(CHAT, "grazie"))
        self.assertEqual(Intervention.objects.count(), 1)

    def test_pressing_invia_senza_note_twice_shows_the_request_already_sent(self):
        self.choose_day()
        self.bot.calls.clear()
        self.press(f"invia:{self.sede.id}:2026-11-20")
        self.bot.calls.clear()

        params = self.press(f"invia:{self.sede.id}:2026-11-20")

        self.assertIn("La richiesta di intervento per <b>Sede</b> è già stata inoltrata", params["text"])
        self.assertEqual(Intervention.objects.count(), 1)

    def send_update(self, update):
        """Un aggiornamento che il bot non riesce a gestire: l'errore arriva al comando telegram_bot, che lo scrive
        nel log (qui, un errore del database come quello di un bot avviato prima di una migrazione)."""
        self.bot.calls.clear()
        with mock.patch.object(telegram.InterventionRequestForm, "save_request",
                               side_effect=IntegrityError("NOT NULL constraint failed")), \
                self.assertRaises(IntegrityError):
            telegram.handle_update(update)

    def test_an_error_while_sending_without_notes_is_not_silent_and_can_be_retried(self):
        self.choose_day()

        self.send_update({"update_id": 1, "callback_query": {
            "id": "query-1", "data": f"invia:{self.sede.id}:2026-11-20",
            "message": {"message_id": 9, "chat": {"id": CHAT, "type": "private"}},
        }})

        # Il titolare riceve l'errore, e il messaggio resta con i suoi bottoni
        self.assertEqual(self.bot.sent, [(CHAT, telegram.SERVER_ERROR)])
        self.assertNotIn("editMessageText", [method for method, _ in self.bot.calls])
        self.assertFalse(Intervention.objects.exists())
        # Può ancora scrivere la nota, o premere di nuovo «Invia senza note»
        self.assertIn("inoltrata per il 20/11/2026", ask(CHAT, "Accesso dal cortile"))
        self.assertEqual(Intervention.objects.get().customer_notes, "Accesso dal cortile")

    def test_an_error_while_sending_with_a_note_is_not_silent_and_can_be_retried(self):
        self.choose_day()

        self.send_update({"update_id": 1, "message": {"chat": {"id": CHAT, "type": "private"}, "text": "Citofono"}})

        # Nessun «nota ricevuta» prima che la richiesta sia salvata
        self.assertEqual(self.bot.sent, [(CHAT, telegram.SERVER_ERROR)])
        self.assertEqual([method for method, _ in self.bot.calls], ["sendMessage"])
        self.bot.calls.clear()
        self.assertIn("inoltrata per il 20/11/2026", self.press(f"invia:{self.sede.id}:2026-11-20")["text"])
        self.assertEqual(Intervention.objects.count(), 1)

    def test_a_command_or_annulla_cancel_the_request(self):
        self.choose_day()
        ask(CHAT, "/stato")
        self.assertIn("Comandi:", ask(CHAT, "Accesso al tetto dalle 9"))

        self.choose_day()
        self.bot.calls.clear()
        self.assertEqual(self.press("annulla:")["text"], "Richiesta di intervento annullata.")
        self.assertIn("Comandi:", ask(CHAT, "Accesso al tetto dalle 9"))
        self.assertFalse(Intervention.objects.exists())

    def test_same_rules_as_the_site(self):
        # Il giorno: da domani a 60 giorni, con il messaggio del form del sito
        for day in ("2026-11-17", "2027-01-17"):
            with self.subTest(day=day):
                self.assertEqual(self.choose_day(day)["text"], "Scegli un giorno tra il 18/11/2026 e il 16/01/2027. "
                                                              "Chiedi di nuovo con /intervento.")
        # Solo con un guasto non ancora affidato a un intervento
        self.assertEqual(self.choose_day(system=self.magazzino)["text"], NO_FAULT)
        self.bot.calls.clear()
        self.press(f"invia:{self.sede.id}:2026-11-20")
        self.bot.calls.clear()
        self.assertIn("La richiesta di intervento per <b>Sede</b> è già stata inoltrata: Richiesta inoltrata per il "
                      "20/11/2026.", self.press(f"invia:{self.sede.id}:2026-11-21")["text"])
        self.assertEqual(Intervention.objects.count(), 1)
        # Solo gli impianti della sua community
        other = create_system(create_community("Bianchi"), "Non suo")
        other.set_status(Status.BROKEN, NOW)
        self.assertEqual(self.choose_day(system=other)["text"], "Impianto non trovato: chiedi di nuovo con /intervento.")


class AcceptedRequestAlertTests(TelegramTestCase):
    """Quando un tecnico accetta la richiesta di intervento, il titolare lo sa su Telegram."""

    def setUp(self):
        super().setUp()
        self.link_owner()
        self.system = create_system(self.community, "Magazzino")
        self.system.set_status(Status.FAULT, timezone.now())
        self.technician = create_technician("tecnico", first_name="Paolo", last_name="Neri")
        # Richiesta fatta dal sito da un altro utente della community: l'avviso arriva comunque al titolare
        self.intervention = Intervention.objects.create(
            system=self.system, requested_by=self.member_user, preferred_date=date(2026, 11, 26),
        )
        self.url = f"/sp/interventions/{self.intervention.pk}/"

    def post_as(self, user, url, data):
        self.client.force_login(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data)

    def test_the_technician_accepts_the_request(self):
        self.post_as(self.technician, self.url, {"code": "INV"})

        [(chat_id, text)] = self.bot.sent
        self.assertEqual(chat_id, CHAT)
        for expected in ("🛠 <b>Richiesta di intervento accettata</b>",
                         "La richiesta per <b>Magazzino</b> (Rossi Trasporti) è stata presa in carico da Paolo Neri, "
                         "per il giorno richiesto: Giovedì 26 Novembre 2026.",
                         "Tipo di intervento: Intervento su Inverter",
                         f"http://localhost:8000/sp/system/{self.system.id}/"):
            self.assertIn(expected, text)

    def test_only_when_the_request_is_accepted(self):
        self.post_as(self.technician, self.url, {"code": "INV"})
        # Né un secondo tecnico (la richiesta è già accettata) né l'esecuzione mandano altri messaggi
        self.post_as(create_technician("tecnico2"), self.url, {"code": "CLN"})
        self.post_as(self.technician, self.url, {"code": "INV", "executed_on": timezone.localdate().isoformat(),
                                                 "notes": "Sostituito l'inverter", "cost": "850.00"})

        self.intervention.refresh_from_db()
        self.assertEqual(self.intervention.status, Intervention.Status.DONE)
        self.assertEqual(len(self.bot.sent), 1)

    def test_accepted_in_the_admin(self):
        admin_user = create_superuser()
        url = f"/admin/SP/intervention/{self.intervention.pk}/change/"

        def save(status, **fields):
            data = {"system": self.system.id, "status": status, "requested_by": self.member_user.id,
                    "requested_at_0": "2026-11-17", "requested_at_1": "10:00:00", "preferred_date": "2026-11-26",
                    "customer_notes": "", "staff": self.technician.id, "code": "CLN", "executed_on": "",
                    "notes": "", "cost": "", **fields}
            self.assertEqual(self.post_as(admin_user, url, data).status_code, 302)

        save("ACC")
        save("ACC", customer_notes="Altre modifiche")
        save("DON", executed_on="2026-11-26")

        [(chat_id, text)] = self.bot.sent
        self.assertIn("presa in carico da Paolo Neri", text)
        self.assertIn("Tipo di intervento: Pulizia Pannelli", text)

    def test_no_alert_without_a_linked_account(self):
        self.owner.telegram_chat_id = None
        self.owner.save()

        self.post_as(self.technician, self.url, {"code": "INV"})

        self.assertEqual(self.bot.sent, [])
        self.assertFalse(telegram.notify_accepted(self.intervention.pk))

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_no_alert_without_the_bot(self):
        self.post_as(self.technician, self.url, {"code": "INV"})

        self.assertEqual(self.bot.sent, [])


class DateProposalTests(BotDataTestCase):
    """Il tecnico propone un altro giorno: il titolare lo approva o lo rifiuta (con conferma) dai bottoni."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.sede.set_status(Status.FAULT, NOW)
        self.technician = create_technician("tecnico", first_name="Paolo", last_name="Neri")
        self.intervention = Intervention.objects.create(
            system=self.sede, requested_by=self.owner_user, preferred_date=date(2026, 11, 20),
            status=Intervention.Status.ACCEPTED, staff=self.technician, code="INV",
        )
        self.url = f"/sp/interventions/{self.intervention.pk}/"
        self.value = f"{self.intervention.pk}:2026-11-24"

    def propose(self, day="2026-11-24"):
        """Il tecnico invia il nuovo giorno dalla sua pagina; restituisce i parametri del messaggio al titolare."""
        self.client.force_login(self.technician)
        self.bot.calls.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {"action": "propose", "day": day})
        [(method, params)] = self.bot.calls
        self.assertEqual((method, params["chat_id"]), ("sendMessage", CHAT))
        return params

    def press_button(self, data):
        self.bot.calls.clear()
        return self.press(data)

    def status(self):
        self.intervention.refresh_from_db()
        return self.intervention.status

    def test_the_owner_receives_the_new_day_with_the_buttons(self):
        params = self.propose()

        for expected in ("📅 <b>Nuova data per l'intervento</b>",
                         "Paolo Neri, il tecnico che ha in carico la richiesta per <b>Sede</b> (Rossi Trasporti), "
                         "propone di intervenire <b>Martedì 24 Novembre 2026</b> invece di Venerdì 20 Novembre "
                         "2026, il giorno richiesto.",
                         "Tipo di intervento: Intervento su Inverter",
                         "Se rifiuti, la richiesta di intervento viene annullata",
                         f"http://localhost:8000/sp/interventions/{self.intervention.pk}/proposal/"):
            self.assertIn(expected, params["text"])
        self.assertEqual(button_data(params["reply_markup"]["inline_keyboard"]),
                         [[("✅ Approva il 24/11", f"approva:{self.value}"), ("❌ Rifiuta", f"rifiuta:{self.value}")]])

    def test_accepting_with_another_day_sends_the_proposal_instead_of_the_acceptance(self):
        request = Intervention.objects.create(system=self.magazzino, requested_by=self.member_user,
                                              preferred_date=date(2026, 11, 20))
        self.client.force_login(self.technician)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"/sp/interventions/{request.pk}/", {"code": "CLN", "day": "2026-11-26"})

        [(chat_id, text)] = self.bot.sent
        self.assertIn("propone di intervenire <b>Giovedì 26 Novembre 2026</b>", text)
        self.assertNotIn("Richiesta di intervento accettata", text)

    def test_approved_the_intervention_stays_with_the_new_day(self):
        self.propose()

        params = self.press_button(f"approva:{self.value}")

        self.assertEqual(params["text"], "✅ Nuovo giorno approvato: l'intervento su <b>Sede</b> è Martedì 24 "
                                         "Novembre 2026.")
        self.assertNotIn("reply_markup", params)
        self.assertEqual(self.status(), Intervention.Status.ACCEPTED)
        self.assertEqual(self.intervention.scheduled_date, date(2026, 11, 24))

    def test_rejecting_asks_to_confirm_and_cancels_the_request(self):
        self.propose()

        params = self.press_button(f"rifiuta:{self.value}")
        self.assertIn("Vuoi rifiutare il 24/11/2026? La richiesta di intervento per <b>Sede</b> sarà annullata",
                      params["text"])
        self.assertEqual(button_data(params["reply_markup"]["inline_keyboard"]),
                         [[("Sì, rifiuta e annulla la richiesta", f"rifiuto:{self.value}")],
                          [("◀ Torna alla proposta", f"proposta:{self.value}")]])
        self.assertEqual(self.status(), Intervention.Status.PROPOSED)
        self.assertIn("Nuova data per l'intervento", self.press_button(f"proposta:{self.value}")["text"])

        params = self.press_button(f"rifiuto:{self.value}")

        self.assertIn("Nuovo giorno rifiutato: la richiesta di intervento per <b>Sede</b> è annullata",
                      params["text"])
        self.assertEqual(self.status(), Intervention.Status.CANCELLED)
        # Per avere un tecnico apre una nuova richiesta, anche subito dal bottone
        self.assertEqual(button_data(params["reply_markup"]["inline_keyboard"]),
                         [[("🛠 Nuova richiesta", f"intervento:{self.sede.id}")]])
        self.assertIn("Intervento per Sede", self.press_button(f"intervento:{self.sede.id}")["text"])

    def test_a_button_of_an_old_proposal_shows_the_new_one(self):
        self.propose()
        self.propose("2026-11-25")

        params = self.press_button(f"approva:{self.value}")

        self.assertIn("⚠️ Il tecnico ha proposto un altro giorno.", params["text"])
        self.assertIn("<b>Mercoledì 25 Novembre 2026</b>", params["text"])
        self.assertEqual(params["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         f"approva:{self.intervention.pk}:2026-11-25")
        self.assertEqual(self.status(), Intervention.Status.PROPOSED)
        self.assertEqual(self.intervention.proposed_date, date(2026, 11, 25))

    def test_answered_on_the_site_the_buttons_say_so(self):
        self.propose()
        self.client.force_login(self.owner_user)
        self.client.post(f"{self.url}proposal/", {"answer": "approve", "day": "2026-11-24"})

        params = self.press_button(f"rifiuto:{self.value}")

        self.assertEqual(params["text"], "Questa proposta non aspetta più la tua risposta: l'intervento su "
                                         "<b>Sede</b> è in programma Martedì 24 Novembre 2026.")
        self.assertEqual(self.status(), Intervention.Status.ACCEPTED)

    def test_only_the_interventions_of_the_community_of_the_owner(self):
        other = create_system(create_community("Bianchi"), "Non suo")
        theirs = Intervention.objects.create(system=other, preferred_date=date(2026, 11, 20), staff=self.technician,
                                             code="INV", status=Intervention.Status.PROPOSED,
                                             proposed_date=date(2026, 11, 24))

        params = self.press_button(f"approva:{theirs.pk}:2026-11-24")

        self.assertEqual(params["text"], "Intervento non trovato: le richieste aperte sono in /intervento.")
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, Intervention.Status.PROPOSED)

    def test_intervento_lists_the_proposals_to_answer(self):
        self.propose()
        self.magazzino.set_status(Status.BROKEN, NOW)

        reply = telegram.answer(CHAT, "/intervento")

        self.assertIn("• Sede: Nuova data proposta, il 24/11/2026 invece del 20/11/2026: aspetta la tua risposta",
                      reply.text)
        self.assertEqual(button_data(reply.buttons), [[("🔴 Magazzino", f"intervento:{self.magazzino.id}")],
                                                      [("📅 Rispondi per Sede: 24/11", f"proposta:{self.value}")]])

    def test_no_message_without_a_linked_account(self):
        self.owner.telegram_chat_id = None
        self.owner.save()
        Intervention.propose(self.intervention.pk, self.technician, date(2026, 11, 24))

        self.assertFalse(telegram.notify_proposal(self.intervention.pk))
        self.assertEqual(self.bot.sent, [])


class OwnerWithoutContactsTests(BotDataTestCase):
    """Un titolare di prima che telefono ed email fossero obbligatori li aggiunge dal sito prima di chiedere."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.sede.set_status(Status.FAULT, NOW)
        Customer.objects.filter(pk=self.owner.pk).update(phone="")

    def test_the_request_waits_for_the_contacts(self):
        reply = telegram.answer(CHAT, "/intervento")

        self.assertIn("Prima di richiedere un intervento aggiungi telefono ed email nel tuo profilo", reply.text)
        self.assertIn("http://localhost:8000/sp/profile/", reply.text)
        self.assertIsNone(reply.buttons)
        self.assertIn("aggiungi telefono ed email", self.press(f"invia:{self.sede.id}:2026-11-20")["text"])
        self.assertFalse(Intervention.objects.exists())


@BOT
class TelegramPageTests(TelegramTestCase):
    url = reverse("SP:telegram")

    def test_the_owner_gets_the_link_to_the_bot(self):
        self.client.force_login(self.owner_user)

        response = self.client.get(self.url)

        self.assertContains(response, "Non collegato")
        self.assertContains(response, f'href="https://t.me/SolarFamilyTestBot?start={customer_of(self.owner_user).telegram_code}"')
        self.assertContains(response, "/stato")

    def test_the_profile_of_the_owner_leads_to_the_page_and_the_menu_does_not(self):
        profile = reverse("SP:profile")
        self.client.force_login(self.owner_user)

        response = self.client.get(profile)

        self.assertContains(response, "Notifiche Telegram")
        self.assertContains(response, "Non collegato")
        self.assertContains(response, f'href="{self.url}"')
        self.assertContains(response, "Collega Telegram")
        # Nel menu non c'è più: si arriva dal profilo
        self.assertNotContains(self.client.get(reverse("SP:solar_community")), f'href="{self.url}"')
        # E la pagina riporta al profilo
        self.assertContains(self.client.get(self.url), f'href="{profile}"')

        self.link_owner()
        response = self.client.get(profile)
        self.assertContains(response, "Collegato")
        self.assertContains(response, "Gestisci le notifiche")

    def test_other_users_have_no_telegram_in_the_profile(self):
        for user in (self.member_user, create_technician()):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("SP:profile"))
                self.assertNotContains(response, "Notifiche Telegram")
                self.assertNotContains(response, f'href="{self.url}"')

    def test_other_users_of_the_community_cannot_link(self):
        self.client.force_login(self.member_user)

        response = self.client.get(self.url)

        self.assertContains(response, "arrivano solo al titolare della community, Mario Rossi", status_code=403)
        self.assertNotContains(response, "t.me", status_code=403)

    def test_staff_and_anonymous_users(self):
        self.client.force_login(create_technician())
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        self.assertRedirects(self.client.get(self.url), f"{reverse('SP:login')}?next={self.url}")

    def test_the_owner_unlinks_telegram(self):
        self.link_owner()
        self.client.force_login(self.owner_user)
        self.assertContains(self.client.get(self.url), "Collegato")

        response = self.client.post(self.url, follow=True)

        self.assertContains(response, "Telegram scollegato")
        self.assertIsNone(customer_of(self.owner_user).telegram_chat_id)
        [(chat_id, text)] = self.bot.sent
        self.assertEqual(chat_id, CHAT)
        self.assertIn("non riceverai più avvisi", text)

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_without_the_bot_the_page_says_so(self):
        self.client.force_login(self.owner_user)

        response = self.client.get(self.url)

        self.assertContains(response, "non è ancora configurato")
        self.assertNotContains(response, "t.me")
        self.assertIsNone(customer_of(self.owner_user).telegram_code)
        self.assertContains(self.client.get(reverse("SP:profile")), "Bot non disponibile")


class TelegramBotCommandTests(TestCase):
    def test_without_the_token_the_bot_is_off(self):
        out = StringIO()
        # Senza token il comando aspetta per sempre: qui la prima attesa lo interrompe
        with mock.patch("SP.management.commands.telegram_bot.time.sleep", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            call_command("telegram_bot", stdout=out)
        self.assertIn("TELEGRAM_BOT_TOKEN is not set", out.getvalue())

    @BOT
    def test_answers_the_messages_and_confirms_them(self):
        updates = [
            [{"update_id": 7, "message": {"chat": {"id": CHAT, "type": "private"}, "text": "ciao"}}],
            KeyboardInterrupt(),
        ]
        with mock.patch("SP.telegram.set_commands") as set_commands, \
                mock.patch("SP.telegram.get_updates", side_effect=updates) as get_updates, \
                mock.patch("SP.telegram.send_message") as send_message:
            call_command("telegram_bot", stdout=StringIO())

        set_commands.assert_called_once()
        self.assertEqual([c.args for c in get_updates.call_args_list], [(None,), (8,)])
        send_message.assert_called_once_with(CHAT, telegram.NOT_LINKED, None)
