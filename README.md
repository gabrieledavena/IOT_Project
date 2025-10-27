# SOLAR FAMILY
Solar Family aims to create an intelligent management system for residential photovoltaic communities.
The system optimizes self-consumption, monitors performance, and use collective intelligence to detect anomalies and improve efficiency.
## System Architecture
1. **IoT Layer (Arduino Node)**
	Each house is equipped with an Arduino-based monitoring node connected to:
	- Current Transformer sensors. One on the solar inverter output to measure production power. One on the main grid line to measure total house consumption.
	- Actuators. Smart relays to turn on/off appliances. We also have actuators that are part of an automated system for cleaning the photovoltaic panels.
2. **MQTT Communication Layer**
	MQTT acts as the messaging backbone between all the components.
	Each Arduino publishes its measurements to its own topic
	The Python Bridge and Django server subscribe to the topics
	The server can publish alerts or commands back to each node
3. **Python Bridge**
	The bridge acts as a go-between for the microcontroller and the server. It serves both as MQTT broker and HTTP client (building packets and sending them to the server and pull down commands).
4. **Django Backend**
	The **Django server** is the intelligence center of the system.
	It exposes REST APIs to receive and serve data, stores everything in a  database, and provides an **interactive web UI** for visualization.
5. **Web Dashboard (Django UI)**
	Data can be visualized through charts, tables, and alerts, giving both individual users and administrators a clear understanding of the system status.
## Key Features 

1. **Self-Consumption Optimization**
	The Arduino sensors measures both house consumption and PV production in real time.
	When the surplus energy (Production - Consumption) exceeds a configurable threshold for several minutes, the server sends an MQTT command to smart relays to turn on non-essential loads, such as water heater, electric car charging and softener regeneration. This allows us to maximize the local energy use, which is more profitable than selling excess energy to the grid.

2. **Environmental Impact Calculation**
	The system converts the community's total energy production into $CO_2$ savings, showing how much pollution has been avoided thanks to renewable energy

3. **Short-Term Production Forecasting**
	Using historical data and weather forecasting APIs, the system can estimate next-day energy production in $KWh$

4. **Dynamic Community Benchmark (Collective Intelligence)** 
	Each home periodically reports its normalized yield (current power / peak installed power) via MQTT.
	The server aggregates all reports to calculate the real-time community benchmark - an objective reference for comparison.
	If a single system performs significantly below the community average, an alert is triggered (possible panel malfunction).
	If the whole community yield is inconsistent with the expected solar conditions from the weather API, the system triggers the actuators for a collective panel cleaning
	
5. **Energy Community Simulation**
	Each home reports its energy balance (surplus or deficit) via MQTT.
	The server computes the amount of energy that can be shared locally before importing from or exporting to the public grid, simulating the community's ability to be self-sustaining

# Possibili implementazioni
## 4. **Dynamic Community Benchmark (Collective Intelligence)**
###  Su Arduino (per ogni casa)
- **Misurazione:** Leggere la potenza istantanea ($W$) prodotta.
- **Invio Dati (MQTT Publish):** Pubblicare su un topic MQTT (es. `comunita/casa_A/produzione`) un payload JSON : `{ "potenza_w": 1500}`.
- **Ricezione Alert (MQTT Subscribe):** Sottoscriversi a un topic di alert (es. `comunita/casa_A/alert`) per ricevere messaggi dal server (es. "Pulisci pannelli").
###  Su Python Bridge (MQTT <-> HTTP)
- **Ascolto (Subscribe):** Sottoscriversi al topic "wildcard" di produzione: `comunita/+/produzione`.
- **Inoltro (HTTP POST):** Quando riceve un messaggio, estrarre l'ID della casa (es. `casa_A`) dal topic e inoltrare il payload JSON all'endpoint API di Django (es. `POST /api/produzione/`).
###  Su Server Django (Backend)
- **Modelli (Database):**
    - `Impianto`: Per salvare i dati statici (es. `nome="casa_A"`, `potenza_picco_wp=3000`).
    - `DatoProduzione`: Per salvare i dati in ingresso (Timestamp, `potenza_w`, `impianto [FK]`).
- **API (Ricezione):** Creare l'endpoint `POST /api/produzione/` che riceve i dati dal Bridge e li salva nel database.
- **Logica di Business (Task periodica, es. ogni 5 min):**
    1. Recuperare gli ultimi dati validi (es. ultimo minuto) di _tutti_ gli impianti.
    2. Calcolare il rendimento (`potenza_w / potenza_picco_wp`)di ogni casa
    3. Calcolare il **`rendimento_medio_comunita
    4. Ciclare su ogni impianto: se `rendimento_singolo < (rendimento_medio_comunita * 0.85)` (cioè performa il 15% in meno della media), marcare l'impianto come "anomalo".
    5. **Azione:** Se un impianto è "anomalo", pubblicare un messaggio sul suo topic MQTT (es. `comunita/casa_A/alert` con payload `"performance_bassa"`) e salvarlo nel DB.
- **API (Esposizione):** Creare un endpoint `GET /api/dashboard/` che mostri lo stato di ogni impianto e il benchmark della comunità.
## 5. Simulazione di una Comunità Energetica
### Su Arduino (per ogni casa)
- **Misurazione:** Leggere la potenza istantanea prodotta e i consumi totali della casa
- **Invio Dati (MQTT Publish):** Modificare il payload MQTT (es. sul topic `comunita/casa_A/bilancio`) per includere i consumi: `{ "produzione_w": 1500, "consumo_w": 400 }`.
### Su Python Bridge
- **Ascolto (Subscribe):** Sottoscriversi al nuovo topic `comunita/+/bilancio`.
- **Inoltro (HTTP POST):** Inoltrare il nuovo payload (produzione + consumo) a un endpoint API dedicato (es. `POST /api/bilancio/`).
### Su Server Django
- **Modelli (Database):** Aggiornare il modello `DatoProduzione` per includere il campo `consumo_w`.
- **API (Ricezione):** Creare l'endpoint `POST /api/bilancio/` per salvare i dati.
- **Logica di Business (Endpoint di calcolo):** Creare un endpoint `GET /api/comunita/stato/` che, quando chiamato:
    1. Recupera gli ultimi dati di bilancio di tutte le case.
    2. Calcola il `bilancio_casa` per ognuna (es. `produzione - consumo`).
    3. Calcola il `bilancio_totale_comunità` (somma di tutti i bilanci).
    4. **Logica di Simulazione:** Calcola quanta energia è "autoconsumata" dalla comunità (l'energia prodotta dai surplus che copre i deficit) e quanta è prelevata dalla rete esterna.
- **Dashboard:** Creare una pagina web che chiami questa API e mostri graficamente i flussi energetici (es. Casa A dà 500W a Casa C; la comunità preleva 1200W dalla rete).
