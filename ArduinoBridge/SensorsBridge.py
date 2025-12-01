import serial
import threading
import time
from datetime import datetime
import statistics
import requests

# --- CONFIGURAZIONE ---
# Assicurati che sia la stessa porta usata dall'IDE di Arduino
SERIAL_PORT = 'COM2' 
BAUD_RATE = 9600
URL = "http://127.0.0.1:8000/sp/panel-data/"
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    print(f"Connesso alla porta seriale {SERIAL_PORT} a {BAUD_RATE} baud.")
except serial.SerialException as e:
    print(f"ERRORE: Impossibile aprire la porta seriale {SERIAL_PORT}.")
    print(f"Dettagli: {e}")
    exit()

# Funzione di invio dati al server
def invia_dati_al_server(payload):
    # stampiamo il payload per debug
    print(f"Invio dati al server: {payload}")
    # Qui andrà il codice MQTT o HTTP reale
    try:
        response = requests.post(URL, json=payload)
        if response.status_code == 201:
            print(f"Dati inviati con successo: {payload}")
        else:
            print(f"Errore invio dati: {response.status_code} - {response.text}")
    except requests.RequestException as e:
        print(f"Errore durante l'invio dei dati: {e}")

def serial_reader():
    """
    Legge dalla seriale, accumula dati per 1 minuto, 
    calcola la media e invia al server.
    """
    buffer_temp = []
    buffer_lux = []
    buffer_power = []
    
    ultimo_invio = time.time()
    INTERVALLO = 60 

    while True:
        try:
            if ser.in_waiting:
                # Legge e pulisce la linea
                raw_line = ser.readline().decode('utf-8', errors='ignore').strip()
                if raw_line:
                    try:
                        dati = raw_line.split('|')
                        if len(dati) == 3:
                            # Convertiamo e aggiungiamo ai buffer
                            t_val = float(dati[0])
                            l_val = float(dati[1])
                            p_val = float(dati[2])
                            
                            buffer_temp.append(t_val)
                            buffer_lux.append(l_val)
                            buffer_power.append(p_val)
                        else:
                            print(f"Formato dati non valido: {raw_line}")

                    except ValueError:
                        print(f"Errore conversione numeri: {raw_line}")

            # Controllo se è passato un minuto
            ora_corrente = time.time()
            if (ora_corrente - ultimo_invio) >= INTERVALLO:
                
                # Verifichiamo di aver raccolto dati prima di fare la media
                if buffer_temp:
                    # Calcolo Medie
                    avg_temp = statistics.mean(buffer_temp)
                    avg_lux = statistics.mean(buffer_lux)
                    avg_power = statistics.mean(buffer_power)
                    
                    # Timestamp precisione al minuto (YYYY-MM-DD HH:MM)
                    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                    
                    # Creazione Payload
                    payload = {
                        "time_stamp": timestamp_str,
                        "temperature": avg_temp,
                        "lightness": avg_lux,
                        "power": avg_power,
                        "system": 1,
                    }
                    
                    # Invio
                    invia_dati_al_server(payload)
                
                else:
                    print("Nessun dato raccolto nell'ultimo minuto.")

                # Reset dei buffer e del timer
                buffer_temp.clear()
                buffer_lux.clear()
                buffer_power.clear()
                ultimo_invio = time.time()
                
            # Piccola pausa per non saturare la CPU
            time.sleep(0.01) 

        except serial.SerialException:
            print("Errore seriale critico. Il thread si ferma.")
            break
        except Exception as e:
            print(f"Errore generico nel thread: {e}")
            time.sleep(1)

# --- AVVIO DEL PROGRAMMA ---

if __name__ == "__main__":
    print("Avvio thread di lettura seriale...")
    
    # Avvia il thread come daemon
    t = threading.Thread(target=serial_reader, daemon=True)
    t.start()

    print("Script in esecuzione. Premi Ctrl+C per terminare.")

    # --- CICLO PRINCIPALE CHE TIENE VIVO LO SCRIPT ---
    try:
        while True:
            # Il main thread non fa nulla, dorme solo per risparmiare CPU
            # mentre il thread 't' lavora in background.
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nChiusura script richiesta dall'utente.")
        if ser.is_open:
            ser.close()
            print("Porta seriale chiusa.")
        print("Arrivederci.")