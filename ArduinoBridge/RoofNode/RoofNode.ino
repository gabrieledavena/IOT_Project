// Nodo sul tetto: il sensore di un impianto, su una scheda Heltec WiFi LoRa 32 V3. Non ha display (quello della
// scheda resta spento) né internet: parla solo con il master della sua community, via LoRa (ProtocolloLora.h).
// Per questa prova non ha sensori collegati: manda al master solo un battito al minuto.
//
// Entra nella rete del master come un dispositivo Zigbee in quella della sua centralina, e il LED dice a che punto è:
//   1. un nodo nuovo chiede di entrare ogni 15 secondi per i primi 10 minuti, poi ogni 2 minuti.
//      LED: un lampo al secondo;
//   2. sul sito il tecnico preme "Aggiungi nodi" nella pagina della community: il master lo accetta e insieme
//      concordano una chiave, che il nodo salva nella flash. LED: due lampi ogni 3 secondi, finché il tecnico non
//      lo assegna al suo impianto;
//   3. assegnato a un impianto, a ogni battito il master risponde con lo stato dell'impianto. LED: un lampo a ogni
//      risposta.
// Altri lampeggi: veloce per 10 secondi con "Identifica" sul sito; tre lampi ogni 5 secondi se il master non
// risponde da tre battiti.
//
// Pulsante PRG tenuto premuto 10 secondi, finché il LED resta acceso: al rilascio il nodo esce dalla rete e torna
// come nuovo. Lo stesso succede quando il tecnico lo rimuove dal sito.
//
// Avvitare sempre l'antenna LoRa prima di accendere la scheda: trasmettere senza antenna può rovinare la radio.
//
// Arduino IDE: scheda "Heltec WiFi LoRa 32(V3)" del pacchetto esp32 di Espressif (3.x, la crittografia è inclusa);
// libreria RadioLib (8 o successiva) dal Gestore librerie. Il monitor seriale (115200 baud) mostra ogni passo.
#include <Preferences.h>
#include <RadioLib.h>
#include <SPI.h>
#include <bootloader_random.h>
#include "ProtocolloLora.h"

// --- PIN DELLA SCHEDA ---
const int pinLed = 35;          // LED bianco
const int pinVext = 36;         // HIGH: spegne l'uscita Vext, che alimenta il display (non usato)
const int pinPulsante = 0;      // Pulsante PRG: LOW se premuto
const int pinLoraNss = 8;
const int pinLoraSck = 9;
const int pinLoraMosi = 10;
const int pinLoraMiso = 11;
const int pinLoraReset = 12;
const int pinLoraBusy = 13;
const int pinLoraDio1 = 14;

// --- PARAMETRI ---
const unsigned long INTERVALLO_BATTITO = 60000;
const unsigned long VARIAZIONE = 5000;          // casuale, perché due nodi non trasmettano sempre insieme
const unsigned long ADESIONE_VELOCE = 15000;
const unsigned long ADESIONE_LENTA = 120000;
const unsigned long DURATA_VELOCE = 600000;     // dopo l'accensione, o dopo essere tornato come nuovo
const unsigned long ATTESA_RISPOSTA = 3000;
const int BATTITI_SENZA_RISPOSTA = 3;
const unsigned long PRESSIONE_RESET = 10000;
const char PREFERENZE[] = "sf-nodo";
// Il contatore dei pacchetti si salva ogni 16: dopo un riavvio riparte 16 più avanti, così non ne riusa nessuno
const uint32_t SALTO_CONTATORE = 16;

SX1262 radio = new Module(pinLoraNss, pinLoraDio1, pinLoraReset, pinLoraBusy);
volatile bool radioPronta = false;     // la radio ha ricevuto un pacchetto (interrupt)
int16_t statoRadio = RADIOLIB_ERR_UNKNOWN;

uint32_t idNodo = 0;

// --- RETE DEL MASTER (nella flash) ---
bool abbinato = false;
uint32_t rete = 0;                     // id della community del master
uint8_t chiave[lora::CHIAVE];
uint32_t inviato = 0;                  // contatore dell'ultimo pacchetto verso il master
uint32_t ricevuto = 0;                 // contatore dell'ultimo pacchetto del master

// --- ADESIONE ---
lora::CoppiaChiavi coppia;
bool coppiaPronta = false;
unsigned long cercaDa = 0;
unsigned long prossimaAdesione = 0;

// --- BATTITI ---
unsigned long prossimoBattito = 0;
unsigned long ultimoBattito = 0;
bool attesaRisposta = false;
int senzaRisposta = 0;
char stato[lora::STATO + 1] = "---";

// --- LED E PULSANTE ---
bool identifica = false;
unsigned long identificaFino = 0;
bool lampo = false;
unsigned long lampoFino = 0;
bool pulsantePremuto = false;
unsigned long pulsantePremutoDa = 0;

// ===================================================================================================================

bool scaduto(unsigned long istante) {
  return (long)(millis() - istante) >= 0;
}

String idLeggibile(uint32_t id) {
  char testo[9];
  snprintf(testo, sizeof(testo), "%08lX", (unsigned long)id);
  return testo;
}

bool assegnato() {
  return strcmp(stato, lora::NON_ASSEGNATO) != 0;
}

// --- FLASH ---

void carica() {
  Preferences preferenze;
  preferenze.begin(PREFERENZE, false);
  abbinato = preferenze.getBool("abbinato", false) &&
             preferenze.getBytes("chiave", chiave, lora::CHIAVE) == lora::CHIAVE;
  rete = preferenze.getUInt("rete", 0);
  inviato = preferenze.getUInt("inviato", 0) + SALTO_CONTATORE;
  ricevuto = preferenze.getUInt("ricevuto", 0);
  if (abbinato) {
    preferenze.putUInt("inviato", inviato);
  }
  preferenze.end();
}

void salvaAbbinamento() {
  Preferences preferenze;
  preferenze.begin(PREFERENZE, false);
  preferenze.putBool("abbinato", true);
  preferenze.putUInt("rete", rete);
  preferenze.putBytes("chiave", chiave, lora::CHIAVE);
  preferenze.putUInt("inviato", inviato);
  preferenze.putUInt("ricevuto", ricevuto);
  preferenze.end();
}

void salvaContatore(const char *nome, uint32_t valore) {
  Preferences preferenze;
  preferenze.begin(PREFERENZE, false);
  preferenze.putUInt(nome, valore);
  preferenze.end();
}

// Il nodo torna come nuovo: dimentica rete e chiave e ricomincia a cercare un master
void dimentica() {
  Preferences preferenze;
  preferenze.begin(PREFERENZE, false);
  preferenze.clear();
  preferenze.end();
  abbinato = false;
  coppiaPronta = false;
  rete = inviato = ricevuto = 0;
  memset(chiave, 0, sizeof(chiave));
  strcpy(stato, lora::NON_ASSEGNATO);
  senzaRisposta = 0;
  attesaRisposta = false;
  cercaDa = millis();
  prossimaAdesione = millis();
  Serial.println("Il nodo è tornato come nuovo: cerco un master");
}

// --- RADIO ---

void IRAM_ATTR suRadio() {
  radioPronta = true;
}

void avviaRadio() {
  SPI.begin(pinLoraSck, pinLoraMiso, pinLoraMosi, pinLoraNss);
  // La radio della Heltec V3 ha un oscillatore TCXO alimentato a 1,8 V
  radio.tcxoVoltage = RadioLibTCXOVoltage_t::Voltage1V8;
  ConfigLoRa_t configurazione;
  configurazione.frequency = RADIOLIB_UNIT_KILO(lora::FREQUENZA_KHZ);
  configurazione.spreadingFactor = lora::SPREADING_FACTOR;
  configurazione.codingRate = lora::CODING_RATE;
  configurazione.power = lora::POTENZA_DBM;
  statoRadio = radio.begin(configurazione);
  if (statoRadio != RADIOLIB_ERR_NONE) {
    Serial.printf("Radio LoRa: errore %d\n", statoRadio);
    return;
  }
  radio.setIrqAction(suRadio);
  radio.startReceive();   // Sempre in ascolto: "Identifica" e lo stato arrivano subito
}

// Trasmissione bloccante (pochi decimi di secondo), poi di nuovo in ascolto
void trasmetti(const uint8_t *pacchetto, size_t lunghezza) {
  if (statoRadio != RADIOLIB_ERR_NONE || lunghezza == 0) {
    return;
  }
  int16_t esito = radio.transmit(pacchetto, lunghezza);
  if (esito != RADIOLIB_ERR_NONE) {
    Serial.printf("Radio LoRa: trasmissione non riuscita (%d)\n", esito);
  }
  radioPronta = false;   // L'interrupt della fine della trasmissione
  radio.startReceive();
}

void inviaAlMaster(uint8_t tipo, const uint8_t *dati, size_t lunghezza) {
  uint8_t pacchetto[lora::MASSIMO];
  size_t chiaro = lora::scriviIntestazione(pacchetto, tipo, rete, idNodo);
  inviato++;
  if (inviato % SALTO_CONTATORE == 0) {
    salvaContatore("inviato", inviato);
  }
  trasmetti(pacchetto, lora::proteggi(pacchetto, chiaro, inviato, lora::VERSO_MASTER, chiave, dati, lunghezza));
}

// --- ADESIONE ---

// La chiave privata deve essere imprevedibile: senza Wi-Fi serve la sorgente di rumore del bootloader
void generaChiavi() {
  bootloader_random_enable();
  coppiaPronta = lora::generaCoppia(coppia);
  bootloader_random_disable();
  if (!coppiaPronta) {
    Serial.println("Chiavi non generate");
  }
}

void cercaMaster() {
  if (!coppiaPronta) {
    generaChiavi();
  }
  if (!coppiaPronta || !scaduto(prossimaAdesione)) {
    return;
  }
  uint8_t pacchetto[lora::MASSIMO];
  size_t lunghezza = lora::scriviIntestazione(pacchetto, lora::ADESIONE, 0, idNodo);
  memcpy(pacchetto + lunghezza, coppia.pubblica, lora::CHIAVE_PUBBLICA);
  lunghezza += lora::CHIAVE_PUBBLICA;
  pacchetto[lunghezza++] = lora::NODO_TETTO;
  trasmetti(pacchetto, lunghezza);
  Serial.println("Chiedo di entrare nella rete di un master");
  unsigned long intervallo = millis() - cercaDa < DURATA_VELOCE ? ADESIONE_VELOCE : ADESIONE_LENTA;
  prossimaAdesione = millis() + intervallo + random(VARIAZIONE);
}

// Il master ha accettato: il nodo verifica che abbia calcolato la sua stessa chiave
void riceviAccettazione(const uint8_t *pacchetto, size_t lunghezza, const lora::Intestazione &intestazione) {
  const size_t chiaro = lora::INTESTAZIONE + lora::CHIAVE_PUBBLICA;
  if (!coppiaPronta || intestazione.rete == 0 || lunghezza != chiaro + lora::CONTATORE + lora::CODICE) {
    return;
  }
  const uint8_t *pubblicaMaster = pacchetto + lora::INTESTAZIONE;
  uint8_t nuova[lora::CHIAVE];
  uint8_t dati[lora::MASSIMO];
  size_t lunghezzaDati = 0;
  uint32_t contatore = 0;
  if (!lora::calcolaChiave(coppia.privata, pubblicaMaster, intestazione.rete, idNodo, coppia.pubblica, pubblicaMaster,
                           nuova) ||
      !lora::apri(pacchetto, lunghezza, chiaro, lora::VERSO_NODO, nuova, contatore, dati, lunghezzaDati) ||
      contatore != 0) {
    Serial.println("Accettazione non valida: ignorata");
    return;
  }
  memcpy(chiave, nuova, lora::CHIAVE);
  memset(coppia.privata, 0, sizeof(coppia.privata));
  coppiaPronta = false;
  rete = intestazione.rete;
  inviato = 0;
  ricevuto = 0;
  abbinato = true;
  strcpy(stato, lora::NON_ASSEGNATO);
  salvaAbbinamento();
  prossimoBattito = millis();   // Il primo battito conferma al master che il nodo ha la chiave
  Serial.printf("Entrato nella rete del master (community %lu)\n", (unsigned long)rete);
}

// --- BATTITI ---

void battiti() {
  if (attesaRisposta && millis() - ultimoBattito > ATTESA_RISPOSTA) {
    attesaRisposta = false;
    senzaRisposta++;
    Serial.printf("Il master non ha risposto (%d)\n", senzaRisposta);
  }
  if (!scaduto(prossimoBattito)) {
    return;
  }
  uint8_t dati[4];
  lora::scrivi32(dati, millis() / 1000);
  inviaAlMaster(lora::BATTITO, dati, sizeof(dati));
  ultimoBattito = millis();
  attesaRisposta = true;
  prossimoBattito = millis() + INTERVALLO_BATTITO + random(VARIAZIONE);
}

void riceviDalMaster(const uint8_t *pacchetto, size_t lunghezza, const lora::Intestazione &intestazione) {
  uint8_t dati[lora::MASSIMO];
  size_t lunghezzaDati = 0;
  uint32_t contatore = 0;
  if (intestazione.rete != rete ||
      !lora::apri(pacchetto, lunghezza, lora::INTESTAZIONE, lora::VERSO_NODO, chiave, contatore, dati, lunghezzaDati)) {
    return;
  }
  if (contatore <= ricevuto) {
    Serial.println("Pacchetto ripetuto scartato");
    return;
  }
  ricevuto = contatore;
  salvaContatore("ricevuto", ricevuto);
  if (intestazione.tipo == lora::RISPOSTA && lunghezzaDati == lora::STATO) {
    bool cambiato = memcmp(stato, dati, lora::STATO) != 0;
    memcpy(stato, dati, lora::STATO);
    attesaRisposta = false;
    senzaRisposta = 0;
    lampo = true;
    lampoFino = millis() + 60;
    if (cambiato && assegnato()) {
      Serial.printf("Stato dell'impianto: %s\n", stato);
    } else if (cambiato) {
      Serial.println("Nella rete, da assegnare a un impianto");
    }
  } else if (intestazione.tipo == lora::IDENTIFICA && lunghezzaDati == 1) {
    identifica = true;
    identificaFino = millis() + dati[0] * 1000UL;
    Serial.println("Identifica: il LED lampeggia");
  } else if (intestazione.tipo == lora::RIMOZIONE) {
    Serial.println("Rimosso dalla rete dal master");
    dimentica();
  }
}

void gestisciRadio() {
  if (statoRadio != RADIOLIB_ERR_NONE || !radioPronta) {
    return;
  }
  radioPronta = false;
  uint8_t pacchetto[lora::MASSIMO];
  size_t lunghezza = radio.getPacketLength();
  bool letto = lunghezza > 0 && lunghezza <= lora::MASSIMO && radio.readData(pacchetto, lunghezza) == RADIOLIB_ERR_NONE;
  radio.startReceive();
  lora::Intestazione intestazione;
  if (!letto || !lora::leggiIntestazione(pacchetto, lunghezza, intestazione) || intestazione.nodo != idNodo) {
    return;   // Rovinato, di un'altra rete LoRa, o per un altro nodo
  }
  if (!abbinato) {
    if (intestazione.tipo == lora::ACCETTAZIONE) {
      riceviAccettazione(pacchetto, lunghezza, intestazione);
    }
  } else {
    riceviDalMaster(pacchetto, lunghezza, intestazione);
  }
}

// --- PULSANTE E LED ---

void controllaPulsante() {
  bool premuto = digitalRead(pinPulsante) == LOW;
  if (premuto && !pulsantePremuto) {
    pulsantePremuto = true;
    pulsantePremutoDa = millis();
  } else if (!premuto && pulsantePremuto) {
    pulsantePremuto = false;
    if (millis() - pulsantePremutoDa >= PRESSIONE_RESET) {
      if (abbinato) {
        inviaAlMaster(lora::USCITA, (const uint8_t *)"", 0);   // Il master lo toglie dal sito
      }
      dimentica();
    }
  }
}

// Lampeggi del LED (vedi l'inizio dello sketch)
bool ledAcceso() {
  unsigned long ora = millis();
  if (pulsantePremuto && ora - pulsantePremutoDa >= PRESSIONE_RESET) {
    return true;
  }
  if (identifica) {
    if (scaduto(identificaFino)) {
      identifica = false;
    } else {
      return (ora / 100) % 2 == 0;
    }
  }
  if (!abbinato) {
    return ora % 1000 < 80;
  }
  if (senzaRisposta >= BATTITI_SENZA_RISPOSTA) {
    unsigned long fase = ora % 5000;
    return fase < 600 && fase % 200 < 80;
  }
  if (!assegnato()) {
    unsigned long fase = ora % 3000;
    return fase < 400 && fase % 200 < 80;
  }
  if (lampo && scaduto(lampoFino)) {
    lampo = false;
  }
  return lampo;
}

// ===================================================================================================================

void setup() {
  Serial.begin(115200);
  pinMode(pinLed, OUTPUT);
  pinMode(pinPulsante, INPUT_PULLUP);
  pinMode(pinVext, OUTPUT);
  digitalWrite(pinVext, HIGH);
  idNodo = lora::idScheda();
  Serial.printf("Nodo sul tetto %s\n", idLeggibile(idNodo).c_str());
  carica();
  avviaRadio();
  cercaDa = millis();
  if (abbinato) {
    Serial.printf("Nella rete del master (community %lu)\n", (unsigned long)rete);
    prossimoBattito = millis() + random(VARIAZIONE);
  } else {
    Serial.println("Nodo nuovo: cerco un master (sul sito, \"Aggiungi nodi\" nella pagina della community)");
  }
}

void loop() {
  controllaPulsante();
  gestisciRadio();
  if (statoRadio == RADIOLIB_ERR_NONE) {
    if (abbinato) {
      battiti();
    } else {
      cercaMaster();
    }
  }
  digitalWrite(pinLed, ledAcceso() ? HIGH : LOW);
}
