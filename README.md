
Feature: 
- Ottimizzazione dell'Autoconsumo :
	Aggiungendo una seconda pinza sul contatore generale posso calcolare in tempo reale il surplus di produzione ($Surplus = Produzione\ Pannelli - Consumo\ Casa$).
	Quando il $Surplus$ supera una certa soglia (es. 1000 W) per più di 5 minuti, Django (tramite MQTT) può inviare un comando a un relè smart ( attuatore ) per **accendere carichi non essenziali** (es. boiler dell'acqua calda, lavaggio dell'addolcificatore,  ricarica dell'auto elettrica). In questo modo si massimizza l'autoconsumo, che è economicamente molto più vantaggioso della vendita.
- **Calcolo dell'Impatto Ambientale:** Converti i $kWh$ totali prodotti (dall'intera "comunità" di vicini) in **$kg$ di $CO_2$ risparmiati**. 
- **Previsione di Produzione a Breve Termine:** Usando lo storico dei dati (come produce l'impianto con sole, nuvole, ecc.) e le API meteo (previsione irraggiamento oraria), puoi provare a implementare un semplice modello che **preveda la produzione $kWh$ del giorno successivo**.
- **Benchmark Dinamico di Comunità (Intelligenza Collettiva)**:
	Sfruttando l'intelligenza collettiva della rete IoT, creiamo un **Rendimento di Riferimento della Comunità** in tempo reale. Ogni dispositivo invia al server Django il proprio rendimento normalizzato (es. $2.5\ kW$ prodotti / $4\ kWp$ installati = $0.625$). Il server calcola la media istantanea di tutti i vicini, stabilendo la "verità oggettiva" della produzione in quel quartiere. Se il rendimento di un singolo vicino è incongruente con i dati  dell'intelligenza collettiva, il server invia un alert per controllare i pannelli per controllare un eventuale malfunzionamento; Se invece la media dell'intelligenza collettiva non è congrua con i dati dell' API del meteo vuol dire che c'è bisogno della pulizia di tutti i pannelli del vicinato .


# Possibili implementazioni
## 1. Benchmark Dinamico di Comunità

_Obiettivo: Creare un "rendimento di riferimento" della comunità per identificare anomalie (es. pannelli sporchi) in una singola casa._

###  Su Arduino (per ogni casa)

- **Misurazione:** Leggere la potenza istantanea ($W$) prodotta.
    
- **Configurazione:** Salvare in memoria (EEPROM o hardcoded) la potenza di picco dell'impianto (es. `3000` $Wp$).
    
- **Calcolo Locale:** Calcolare il **rendimento istantaneo** (es. `produzione_attuale_W / potenza_picco_Wp`). Esempio: $1500W / 3000Wp = 0.5$.
    
- **Invio Dati (MQTT Publish):** Pubblicare su un topic MQTT (es. `comunita/casa_A/produzione`) un payload JSON con entrambi i valori: `{ "potenza_w": 1500, "rendimento": 0.5 }`.
    
- **Ricezione Alert (MQTT Subscribe):** Sottoscriversi a un topic di alert (es. `comunita/casa_A/alert`) per ricevere messaggi dal server (es. "Pulisci pannelli").
    

###  Su Python Bridge (MQTT <-> HTTP)

- **Ascolto (Subscribe):** Sottoscriversi al topic "wildcard" di produzione: `comunita/+/produzione`.
    
- **Inoltro (HTTP POST):** Quando riceve un messaggio, estrarre l'ID della casa (es. `casa_A`) dal topic e inoltrare il payload JSON all'endpoint API di Django (es. `POST /api/produzione/`).
    

###  Su Server Django (Backend)

- **Modelli (Database):**
    
    - `Impianto`: Per salvare i dati statici (es. `nome="casa_A"`, `potenza_picco_wp=3000`).
        
    - `DatoProduzione`: Per salvare i dati in ingresso (Timestamp, `potenza_w`, `rendimento`, `impianto [FK]`).
        
- **API (Ricezione):** Creare l'endpoint `POST /api/produzione/` che riceve i dati dal Bridge e li salva nel database.
    
- **Logica di Business (Task periodica, es. ogni 5 min):**
    
    1. Recuperare gli ultimi dati validi (es. ultimo minuto) di _tutti_ gli impianti.
        
    2. Calcolare il **`rendimento_medio_comunita`** (es. facendo la mediana o la media dei rendimenti).
        
    3. Ciclare su ogni impianto: se `rendimento_singolo < (rendimento_medio_comunita * 0.85)` (cioè performa il 15% in meno della media), marcare l'impianto come "anomalo".
        
    4. **Azione:** Se un impianto è "anomalo", pubblicare un messaggio sul suo topic MQTT (es. `comunita/casa_A/alert` con payload `"performance_bassa"`) e salvarlo nel DB.
        
- **API (Esposizione):** Creare un endpoint `GET /api/dashboard/` che mostri lo stato di ogni impianto e il benchmark della comunità.
## 2. Simulazione di una Comunità Energetica

_Obiettivo: Calcolare il bilancio (surplus/deficit) di ogni casa e simulare lo scambio di energia all'interno della comunità._

### Su Arduino (per ogni casa)

- **Misurazione:** Aggiungere una **seconda pinza amperometrica** (e sensore di tensione) sul **contatore generale** per misurare i **consumi totali** della casa ($W$).
    
- **Invio Dati (MQTT Publish):** Modificare il payload MQTT (es. sul topic `comunita/casa_A/bilancio`) per includere i consumi: `{ "produzione_w": 1500, "consumo_w": 400 }`.
    

### Su Python Bridge

- **Ascolto (Subscribe):** Sottoscriversi al nuovo topic `comunita/+/bilancio`.
    
- **Inoltro (HTTP POST):** Inoltrare il nuovo payload (produzione + consumo) a un endpoint API dedicato (es. `POST /api/bilancio/`).
    

### Su Server Django

- **Modelli (Database):** Aggiornare il modello `DatoProduzione` (o crearne uno nuovo `DatoBilancio`) per includere il campo `consumo_w`.
    
- **API (Ricezione):** Creare l'endpoint `POST /api/bilancio/` per salvare i dati.
    
- **Logica di Business (Endpoint di calcolo):** Creare un endpoint `GET /api/comunita/stato/` che, quando chiamato:
    
    1. Recupera gli ultimi dati di bilancio di tutte le case.
        
    2. Calcola il `bilancio_casa` per ognuna (es. `produzione - consumo`).
        
    3. Calcola il `bilancio_totale_comunità` (somma di tutti i bilanci).
        
    4. **Logica di Simulazione:** Calcola quanta energia è "autoconsumata" dalla comunità (l'energia prodotta dai surplus che copre i deficit) e quanta è immessa/prelevata dalla rete esterna.
        
- **Dashboard:** Creare una pagina web che chiami questa API e mostri graficamente i flussi energetici (es. Casa A dà 500W a Casa C; la comunità immette 1200W in rete).
