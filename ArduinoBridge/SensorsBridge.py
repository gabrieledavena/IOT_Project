import serial
import threading
import time
from datetime import datetime
import statistics
import requests

# --- CONFIGURATION ---
SERIAL_PORT = 'COM2'    # Port connected to Arduino
BAUD_RATE = 9600        # Baud rate of the serial communication
URL = "http://127.0.0.1:8000/sp/panel-data/"
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    print(f"Connected to serial port {SERIAL_PORT} at {BAUD_RATE} baud.")
except serial.SerialException as e:
    print(f"ERROR: Unable to open serial port {SERIAL_PORT}.")
    print(f"Details: {e}")
    exit()

# Function to send data to the HTTP server (MQTT in the future)
def send_data_to_server(payload):
    # Send data to the server via POST
    try:
        response = requests.post(URL, json=payload)
        if response.status_code == 201:
            print(f"Data sent successfully: {payload}")
        else:
            print(f"Error sending data: {response.status_code} - {response.text}")
    except requests.RequestException as e:
        print(f"Error sending data: {e}")
def serial_reader():
    """
    Reads from the serial port, accumulates data for 1 minute, 
    calculates the average, and sends it to the server.
    """
    buffer_temp = []
    buffer_lux = []
    buffer_power = []
    
    last_send = time.time()
    INTERVAL = 60 

    while True:
        try:
            if ser.in_waiting:
                # Reads and cleans the line
                raw_line = ser.readline().decode('utf-8', errors='ignore').strip()
                if raw_line:
                    try:
                        data = raw_line.split('|')
                        if len(data) == 3:
                            # Converts and adds to buffers
                            t_val = float(data[0])
                            l_val = float(data[1])
                            p_val = float(data[2])
                            
                            buffer_temp.append(t_val)
                            buffer_lux.append(l_val)
                            buffer_power.append(p_val)
                        else:
                            print(f"Invalid data format: {raw_line}")

                    except ValueError:
                        print(f"Number conversion error: {raw_line}")

            # Checks if a minute has passed
            current_time = time.time()
            if (current_time - last_send) >= INTERVAL:
                
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
                    
                    # Send
                    send_data_to_server(payload)
                
                else:
                    print("No data collected in the last minute.")

                # Reset buffers and timer
                buffer_temp.clear()
                buffer_lux.clear()
                buffer_power.clear()
                last_send = time.time()
                
            # Small pause to avoid CPU saturation
            time.sleep(0.01) 

        except serial.SerialException:
            print("Critical serial error. The thread stops.")
            break
        except Exception as e:
            print(f"Generic error in the thread: {e}")
            time.sleep(1)

# --- START OF THE PROGRAM ---

if __name__ == "__main__":
    print("Starting serial reading thread...")
    
    # Start the thread as a daemon
    t = threading.Thread(target=serial_reader, daemon=True)
    t.start()

    print("Script running. Press Ctrl+C to terminate.")
    # --- MAIN LOOP TO KEEP THE SCRIPT ALIVE ---
    try:
        while True:
            # The main thread does nothing, just sleeps to save CPU
            # while the thread 't' works in the background.
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nUser requested script termination.")
        if ser.is_open:
            ser.close()
            print("Serial port closed.")
        print("Goodbye.")