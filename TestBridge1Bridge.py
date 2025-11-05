import paho.mqtt.client as mqtt
import serial
import threading
import time

# --- CONFIGURAZIONE ---
# ATTENZIONE: Assicurati che sia la stessa porta usata dall'IDE di Arduino
# Esempio: 'COM3' su Windows, '/dev/ttyUSB0' su Linux
SERIAL_PORT = 'COM2' 
BAUD_RATE = 9600

BROKER = 'localhost' # Il tuo broker MQTT (Mosquitto)

# Topic per inviare comandi all'Arduino (es. "PING")
TOPIC_SUB_COMMANDS = 'panel/command'
# Topic per pubblicare i dati ricevuti dall'Arduino
TOPIC_PUB_DATA = 'panel/data'
# --------------------

# Prova a connettersi alla porta seriale
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    print(f"Connesso alla porta seriale {SERIAL_PORT} a {BAUD_RATE} baud.")
except serial.SerialException as e:
    print(f"ERRORE: Impossibile aprire la porta seriale {SERIAL_PORT}.")
    print(f"Dettagli: {e}")
    print("Controlla che la porta sia corretta e non usata da altri programmi (es. Monitor Seriale Arduino).")
    exit()

def on_connect(client, userdata, flags, rc):
    """Chiamato quando ci si connette al broker MQTT."""
    print(f"Connesso al broker MQTT con codice {rc}")
    # Si sottoscrive al topic dei comandi
    client.subscribe(TOPIC_SUB_COMMANDS)
    print(f"Sottoscritto al topic dei comandi: {TOPIC_SUB_COMMANDS}")

def on_message(client, userdata, msg):
    """Chiamato quando riceve un messaggio MQTT sul topic sottoscritto."""
    command = msg.payload.decode()
    print(f"Messaggio ricevuto su {msg.topic}: {command}")
    
    # Invia il comando ricevuto all'Arduino via seriale
    try:
        ser.write((command + '\n').encode())
        print(f"Comando '{command}' inviato ad Arduino.")
    except Exception as e:
        print(f"Errore durante l'invio seriale: {e}")

def serial_reader():
    """
    Thread separato che legge continuamente dalla seriale
    e pubblica ogni linea ricevuta sul topic MQTT.
    """
    while True:
        try:
            if ser.in_waiting:
                # Legge una linea dalla seriale
                line = ser.readline().decode('utf-8').strip()
                
                if line:
                    print(f"Dati da Arduino: {line}")
                    # Pubblica la linea intera sul topic dei dati
                    client.publish(TOPIC_PUB_DATA, line)
        
        except serial.SerialException:
            print("Errore seriale. Il thread si ferma.")
            break
        except Exception as e:
            print(f"Errore nel thread di lettura: {e}")
            time.sleep(1) # Evita il looping rapido in caso di errore

# Configurazione del client MQTT
client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message

# Connessione al broker
try:
    client.connect(BROKER, 1883, 60)
except Exception as e:
    print(f"ERRORE: Impossibile connettersi al broker MQTT '{BROKER}'.")
    print(f"Dettagli: {e}")
    print("Assicurati che il broker (es. Mosquitto) sia in esecuzione.")
    exit()

# Avvia il thread per la lettura della seriale in background
# 'daemon=True' assicura che il thread termini quando lo script principale termina
t = threading.Thread(target=serial_reader, daemon=True)
t.start()

print("MQTT Bridge per ProgettoFinale avviato...")
print(f"Ascolta comandi su: {TOPIC_SUB_COMMANDS}")
print(f"Pubblica dati su:   {TOPIC_PUB_DATA}")
print("Premi CTRL+C per fermare.")

# Mantiene lo script in esecuzione per ascoltare i messaggi
client.loop_forever()