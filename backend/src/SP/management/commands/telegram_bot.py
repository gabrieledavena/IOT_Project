import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from SP import telegram

# Attesa prima di riprovare quando Telegram non risponde (rete assente, token sbagliato...)
RETRY_DELAY = 30


class Command(BaseCommand):
    help = (
        "Runs the Telegram bot: reads the messages sent to the bot with long polling (getUpdates) and answers "
        "them (/start with the code of the site links the owner of a community, /stato, /report, /lettura, "
        "/intervento, /stop, /aiuto) and the buttons pressed under its messages. "
        "The fault alerts are sent by check_systems. Runs forever: the telegram-bot service of docker compose "
        "starts it. Without TELEGRAM_BOT_TOKEN the bot is off and the command just waits."
    )

    def handle(self, *args, **options):
        if not telegram.enabled():
            # Il servizio docker si riavvia sempre: invece di uscire si aspetta, senza consumare risorse
            self.stdout.write(self.style.WARNING("TELEGRAM_BOT_TOKEN is not set: the Telegram bot is off."))
            while True:
                time.sleep(3600)

        offset = None
        commands_set = False
        self.stdout.write("Telegram bot started, waiting for messages...")
        while True:
            try:
                if not commands_set:
                    telegram.set_commands()
                    commands_set = True
                updates = telegram.get_updates(offset)
            except telegram.TelegramError as error:
                self.stderr.write(f"Telegram is not reachable ({error}): retrying in {RETRY_DELAY} s.")
                time.sleep(RETRY_DELAY)
                continue
            except KeyboardInterrupt:
                return

            # Processo lungo: le connessioni al database non si chiudono da sole come alla fine di una richiesta
            close_old_connections()
            for update in updates:
                # Il prossimo getUpdates conferma questo messaggio anche se la risposta non riesce: un messaggio
                # che fa fallire il bot non va ripetuto all'infinito
                offset = update["update_id"] + 1
                try:
                    telegram.handle_update(update)
                except Exception as error:  # noqa: BLE001 - un messaggio sbagliato non deve fermare il bot
                    self.stderr.write(f"Telegram update {update['update_id']} not handled: {error!r}")
