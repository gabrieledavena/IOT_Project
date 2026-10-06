// Nodo di un impianto fotovoltaico: legge i sensori e invia le misure al bridge sulla seriale (UART),
// riceve dal bridge la potenza massima dell'impianto e il suo stato, e con DRT (pannelli sporchi)
// aziona la pompa dell'acqua per il lavaggio. Il protocollo è descritto in ArduinoBridge/protocol.py.
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <DHT.h>

// --- IDENTITÀ ---
// Il bridge usa la porta seriale su cui arriva ID|SolarNode|<versione del protocollo>: lo sketch lo invia
// appena acceso e in risposta al comando ID
const char TIPO_DISPOSITIVO[] = "SolarNode";
const int VERSIONE_PROTOCOLLO = 1;

// --- PIN DI INPUT ---
#define DHTPIN 4           // Pin digitale collegato a 'out' del DHT11 (es. D4 o A4)
#define DHTTYPE DHT11      // Modello del sensore
const int pinSensoreLuce = A5;

// --- PIN DI OUTPUT ---
const int pinPompa = 7;        // Relè della pompa dell'acqua per il lavaggio dei pannelli
const int pinLedOk = 8;        // Verde: impianto OK
const int pinLedSporco = 9;    // Giallo: pannelli sporchi (DRT)
const int pinLedGuasto = 10;   // Rosso: probabile guasto (FLT)

// Inizializzazione sensore DHT
DHT dht(DHTPIN, DHTTYPE);

// --- PARAMETRI COMUNI ---
const float VCC = 5.0; // Tensione di alimentazione (5V)

// --- PARAMETRI CALCOLO POTENZA PANNELLI ---
const float TEMP_RIFERIMENTO_C = 25.0; // 25°C in Celsius
const float K = -0.004;                // Fattore correttivo di temperatura (-0.4%/°C)

// --- PARAMETRI SENSORE LUCE (A5) ---
const float R_FISSA_LUCE = 100.0;
const float LDR_GAMMA = 0.8;
const float LDR_R1 = 127410.0;   // Resistenza LDR a 1 lux (Ohm)
const float LUX_MASSIMI = 130000.0;

// --- TEMPI (ms) ---
const unsigned long INTERVALLO_LETTURA = 1500;  // Il DHT11 richiede almeno 1 secondo tra due letture
const unsigned long INTERVALLO_READY = 2000;
const unsigned long DURATA_LAVAGGIO = 10000;

// Potenza massima dell'impianto (W): la invia il bridge con CFG, secondo l'impianto con cui è collegato.
// Finché non arriva, Arduino non invia misure e chiede la configurazione con READY.
float maxPanelPower = 0;
char stato[4] = "";           // OK, DRT o FLT, inviato dal bridge con STATUS
bool pompaAccesa = false;
unsigned long inizioLavaggio = 0;
unsigned long ultimaLettura = 0;
unsigned long ultimoReady = 0;

// Riga in arrivo dal bridge, letta un carattere alla volta senza bloccare il loop
char comando[32];
byte lunghezzaComando = 0;

void setup() {
  Serial.begin(9600);
  dht.begin();
  pinMode(pinPompa, OUTPUT);
  pinMode(pinLedOk, OUTPUT);
  pinMode(pinLedSporco, OUTPUT);
  pinMode(pinLedGuasto, OUTPUT);
  aggiornaUscite();

  // Appena acceso si presenta e chiede la configurazione. Aprire la porta USB riavvia la scheda: così il bridge
  // la riconosce senza scriverle mentre è ancora attivo il bootloader (quello di Arduino Mega, se riceve dati,
  // resta in attesa di essere programmato e lo sketch non parte)
  inviaIdentita();
  Serial.println("READY");
  ultimoReady = millis();
}

void loop() {
  leggiComandi();

  // Niente delay(): il loop deve continuare a leggere i comandi e spegnere la pompa al momento giusto
  unsigned long adesso = millis();
  if (pompaAccesa && adesso - inizioLavaggio >= DURATA_LAVAGGIO) {
    fermaPompa();
  }
  if (maxPanelPower <= 0) {
    if (adesso - ultimoReady >= INTERVALLO_READY) {
      ultimoReady = adesso;
      Serial.println("READY");
    }
  } else if (adesso - ultimaLettura >= INTERVALLO_LETTURA) {
    ultimaLettura = adesso;
    inviaMisura();
  }
}

void inviaMisura() {
  // === 1. LETTURA TEMPERATURA (DHT11) ===
  float tempC = dht.readTemperature();
  if (isnan(tempC)) {
    // Se la lettura fallisce si usa la temperatura di riferimento
    tempC = TEMP_RIFERIMENTO_C;
  }

  // === 2. LETTURA LUMINOSITÀ (A5) ===
  int valoreLuce = analogRead(pinSensoreLuce);
  float VoutLuce = (float)valoreLuce * (VCC / 1023.0);
  float R_LDR = R_FISSA_LUCE * (VCC / VoutLuce - 1.0);
  float lux = R_LDR > 0 ? pow(LDR_R1 / R_LDR, 1.0 / LDR_GAMMA) : LUX_MASSIMI;
  if (isnan(lux) || lux > LUX_MASSIMI) {
    lux = LUX_MASSIMI;
  }
  float lightPercent = (float)valoreLuce / 1023.0;

  // Calcolo potenza stimata con derating termico
  float panelPower = (lightPercent * maxPanelPower) * (1.0 + (tempC - TEMP_RIFERIMENTO_C) * K);

  // === 3. INVIO AL BRIDGE: D|temperatura|lux|potenza(W) ===
  Serial.print("D|");
  Serial.print(tempC);
  Serial.print("|");
  Serial.print(lux);
  Serial.print("|");
  Serial.println(panelPower);
}

void leggiComandi() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lunghezzaComando > 0) {
        comando[lunghezzaComando] = '\0';
        eseguiComando(comando);
        lunghezzaComando = 0;
      }
    } else if (lunghezzaComando < sizeof(comando) - 1) {
      comando[lunghezzaComando++] = c;
    }
  }
}

// ID            chi sei? Risposta: ID|SolarNode|1
// CFG|6000      potenza massima dell'impianto in W
// STATUS|DRT|1  stato dell'impianto; 1 se è un nuovo cambiamento (con DRT si avvia il lavaggio)
void eseguiComando(char *riga) {
  char *tipo = strtok(riga, "|");
  if (tipo == NULL) {
    return;
  }

  if (strcmp(tipo, "ID") == 0) {
    inviaIdentita();
  } else if (strcmp(tipo, "CFG") == 0) {
    char *watt = strtok(NULL, "|");
    if (watt != NULL && atol(watt) > 0) {
      maxPanelPower = atol(watt);
      Serial.print("ACK|CFG|");
      Serial.println(atol(watt));
    }
  } else if (strcmp(tipo, "STATUS") == 0) {
    char *nuovoStato = strtok(NULL, "|");
    char *cambiamento = strtok(NULL, "|");
    if (nuovoStato == NULL || cambiamento == NULL) {
      return;
    }
    if (strcmp(nuovoStato, "OK") != 0 && strcmp(nuovoStato, "DRT") != 0 && strcmp(nuovoStato, "FLT") != 0) {
      return;
    }
    strncpy(stato, nuovoStato, sizeof(stato) - 1);
    if (strcmp(stato, "DRT") == 0) {
      // Solo per un nuovo cambiamento: se il bridge si riavvia non si lava di nuovo
      if (strcmp(cambiamento, "1") == 0) {
        avviaPompa();
      }
    } else {
      fermaPompa();
    }
    aggiornaUscite();
  }
}

// ID|SolarNode|1: tipo di dispositivo e versione del protocollo
void inviaIdentita() {
  Serial.print("ID|");
  Serial.print(TIPO_DISPOSITIVO);
  Serial.print("|");
  Serial.println(VERSIONE_PROTOCOLLO);
}

void avviaPompa() {
  if (!pompaAccesa) {
    Serial.println("PUMP|ON");
  }
  pompaAccesa = true;
  inizioLavaggio = millis();
  aggiornaUscite();
}

void fermaPompa() {
  if (pompaAccesa) {
    pompaAccesa = false;
    Serial.println("PUMP|OFF");
    aggiornaUscite();
  }
}

void aggiornaUscite() {
  digitalWrite(pinPompa, pompaAccesa ? HIGH : LOW);
  digitalWrite(pinLedOk, strcmp(stato, "OK") == 0 ? HIGH : LOW);
  digitalWrite(pinLedSporco, strcmp(stato, "DRT") == 0 ? HIGH : LOW);
  digitalWrite(pinLedGuasto, strcmp(stato, "FLT") == 0 ? HIGH : LOW);
}
