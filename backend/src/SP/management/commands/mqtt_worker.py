from django.conf import settings
from django.core.management.base import BaseCommand
from paho.mqtt import client as mqtt_client

from SP import mqtt

# Client id fisso e sessione persistente: se il worker si ferma, il broker tiene da parte i messaggi
# QoS 1 dei dispositivi e li consegna quando il worker si ricollega
CLIENT_ID = "solarfamily-server"


class Command(BaseCommand):
    help = (
        "Receives the messages of the devices from the MQTT broker (readings, connection, pump events) and "
        "publishes the configuration and status of every installed system when it connects. Runs forever: "
        "the mqtt-worker service of docker compose starts it."
    )

    def handle(self, *args, **options):
        client = mqtt_client.Client(
            mqtt_client.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID, clean_session=False,
        )
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self.on_connect
        client.on_disconnect = self.on_disconnect
        client.on_message = self.on_message

        self.stdout.write(f"Connecting to the MQTT broker {settings.MQTT_HOST}:{settings.MQTT_PORT}...")
        client.connect_async(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=60)
        try:
            # Riprova da solo se il broker non è ancora pronto o cade
            client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            client.disconnect()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.stderr.write(f"Connection refused by the broker: {reason_code}")
            return
        self.stdout.write(self.style.SUCCESS("Connected to the MQTT broker."))
        # Da ogni impianto: + vale per un livello qualsiasi del topic, cioè l'id dell'impianto
        client.subscribe([
            (mqtt.topic("+", name), mqtt.QOS) for name in (mqtt.TELEMETRY, mqtt.CONNECTION, mqtt.EVENTS)
        ])
        # Allinea il broker al database: stato e configurazione di ogni impianto installato, retained
        messages = mqtt.state_messages(mqtt.installed_systems())
        for message in messages:
            client.publish(**message)
        self.stdout.write(f"Published the state of {len(messages) // 2} installed systems.")

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.stderr.write(f"Disconnected from the MQTT broker ({reason_code}): reconnecting...")

    def on_message(self, client, userdata, message):
        mqtt.handle_message(message.topic, message.payload)
