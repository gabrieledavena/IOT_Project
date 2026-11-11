// Nodo sul tetto: il sensore di un impianto, su una scheda Heltec WiFi LoRa 32 V3. Non ha internet: parla solo con il
// master della sua community, via LoRa (ProtocolloLora.h). Ogni 10 secondi gli manda una misura (temperatura, luce
// e ora del suo orologio) e ogni minuto un battito, a cui il master risponde con lo stato dell'impianto e l'ora.
//
// Sensori, tutti sul connettore J3 (pin 15-18), con 3V3 e GND dello stesso connettore:
//   GPIO4, GPIO5  SDA e SCL del modulo orologio DS1307 (Tiny RTC), su un bus I2C a 100 kHz separato da quello del
//                 display. Il DS1307 funziona solo a 5 V: si alimenta dal pin 5V di J2, che ha tensione solo con
//                 la scheda collegata a USB o al power bank. A batteria l'orologio non risponde e il nodo usa l'ora
//                 ricevuta dal master. Le resistenze di pull-up di SDA e SCL vanno verso 3V3 (10 kΩ), non a 5 V
//   GPIO6         DATA del DHT11 (temperatura)
//   GPIO7         fotoresistenza: partitore con la fotoresistenza verso 3V3 e una resistenza da 1 kΩ verso GND
// DHT11 e DS1307 si leggono senza librerie (le funzioni sono qui sotto).
//
// Il display della scheda mostra le misure, l'ora e lo stato. Al posto del LED blu della pompa, durante il lavaggio
// dei pannelli mostra un'animazione dell'acqua: 10 secondi quando l'impianto passa a "pannelli sporchi" (DRT) o a
// "probabile guasto" (FLT), una volta per cambiamento (lo stato già gestito resta nella flash).
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
// Pulsante PRG:
//   - premuto e rilasciato subito: un lavaggio di prova (l'animazione dell'acqua per 10 secondi);
//   - tenuto premuto 10 secondi, finché il LED resta acceso: al rilascio il nodo esce dalla rete e torna come nuovo.
//     Lo stesso succede quando il tecnico lo rimuove dal sito.
//
// Avvitare sempre l'antenna LoRa prima di accendere la scheda: trasmettere senza antenna può rovinare la radio.
//
// Arduino IDE: scheda "Heltec WiFi LoRa 32(V3)" del pacchetto esp32 di Espressif (3.x, la crittografia è inclusa);
// librerie RadioLib (8 o successiva) e U8g2 dal Gestore librerie. Il monitor seriale (115200 baud) mostra ogni passo.
#include <Preferences.h>
#include <RadioLib.h>
#include <SPI.h>
#include <U8g2lib.h>
#include <Wire.h>
#include <bootloader_random.h>
#include <math.h>
#include <sys/time.h>
#include <time.h>
#include "ProtocolloLora.h"

// --- PIN DELLA SCHEDA ---
const int pinLed = 35;          // LED bianco
const int pinVext = 36;         // LOW: accende l'uscita Vext (3,3 V), che alimenta il display
const int pinPulsante = 0;      // Pulsante PRG: LOW se premuto
const int pinOledSda = 17;
const int pinOledScl = 18;
const int pinOledReset = 21;
const int pinLoraNss = 8;
const int pinLoraSck = 9;
const int pinLoraMosi = 10;
const int pinLoraMiso = 11;
const int pinLoraReset = 12;
const int pinLoraBusy = 13;
const int pinLoraDio1 = 14;

// --- PIN DEI SENSORI (connettore J3) ---
const int pinOrologioSda = 4;   // J3 pin 15
const int pinOrologioScl = 5;   // J3 pin 16
const int pinDht = 6;           // J3 pin 17
const int pinLuce = 7;          // J3 pin 18, ADC1_CH6

// --- PARAMETRI ---
const unsigned long INTERVALLO_BATTITO = 60000;
const unsigned long VARIAZIONE = 5000;          // casuale, perché due nodi non trasmettano sempre insieme
const unsigned long INTERVALLO_MISURA = 10000;  // con il battito, circa lo 0,85% del tempo in trasmissione
const unsigned long VARIAZIONE_MISURA = 1000;
const unsigned long ADESIONE_VELOCE = 15000;
const unsigned long ADESIONE_LENTA = 120000;
const unsigned long DURATA_VELOCE = 600000;     // dopo l'accensione, o dopo essere tornato come nuovo
const unsigned long ATTESA_RISPOSTA = 3000;
const int BATTITI_SENZA_RISPOSTA = 3;
const unsigned long PRESSIONE_RESET = 10000;
const unsigned long PRESSIONE_PROVA = 1000;     // PRG rilasciato prima: lavaggio di prova
const unsigned long PRESSIONE_AVVISO = 2000;    // da qui il display spiega cosa succede al rilascio
const unsigned long DURATA_LAVAGGIO = 10000;
const unsigned long INTERVALLO_SENSORI = 2000;  // il DHT11 vuole almeno un secondo tra due letture
const unsigned long VALIDITA_TEMPERATURA = 10000;   // senza letture riuscite da tanto, la temperatura è assente
const unsigned long RIPROVA_OROLOGIO = 60000;   // orologio che non risponde: lo si cerca di nuovo dopo un minuto
const int64_t SCARTO_OROLOGIO = 2;              // secondi: oltre, l'ora del master corregge l'orologio
const unsigned long INTERVALLO_DISPLAY = 250;
const unsigned long INTERVALLO_ANIMAZIONE = 60;
const char FUSO_ORARIO[] = "CET-1CEST,M3.5.0,M10.5.0/3";   // ora italiana, con l'ora legale
const char PREFERENZE[] = "sf-nodo";
// Il contatore dei pacchetti si salva ogni 16: dopo un riavvio riparte 16 più avanti, così non ne riusa nessuno
const uint32_t SALTO_CONTATORE = 16;

// --- FOTORESISTENZA ---
// Partitore: 3V3 - fotoresistenza - GPIO7 - R_FISSA_LUCE - GND. Più luce, meno resistenza, più tensione sul pin.
// I valori sono quelli di una GL5528, la più comune nei kit: 10-20 kΩ a 10 lux, gamma 0,6. Per tararla, misurate
// la sua resistenza con il tester a una luce nota e correggete LDR_R10. Il lux è una stima, non una misura precisa
const float TENSIONE_PARTITORE_MV = 3300.0;
const float R_FISSA_LUCE = 1000.0;              // Ω
const float LDR_R10 = 15000.0;                  // Ω a 10 lux
const float LDR_GAMMA = 0.6;
const float LUX_MASSIMI = 100000.0;
const int CAMPIONI_LUCE = 16;

// --- OROLOGIO DS1307 ---
const uint8_t INDIRIZZO_DS1307 = 0x68;
TwoWire &busOrologio = Wire1;   // Wire è del display, che va a 400 kHz

U8G2_SSD1306_128X64_NONAME_F_HW_I2C schermo(U8G2_R0, pinOledReset, pinOledScl, pinOledSda);
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

// --- BATTITI E MISURE ---
unsigned long prossimoBattito = 0;
unsigned long ultimoBattito = 0;
bool attesaRisposta = false;
int senzaRisposta = 0;
char stato[lora::STATO + 1] = "---";
unsigned long prossimaMisura = 0;
unsigned long ultimaMisura = 0;        // millis() dell'ultima misura inviata
bool misuraInviata = false;

// --- SENSORI ---
int16_t temperatura = lora::TEMPERATURA_ASSENTE;   // decimi di °C, dell'ultima lettura riuscita
unsigned long temperaturaDa = 0;
float luce = 0;                                     // lux
unsigned long prossimaLettura = 0;
portMUX_TYPE sezioneDht = portMUX_INITIALIZER_UNLOCKED;

// --- OROLOGIO ---
enum StatoOrologio { OROLOGIO_ASSENTE, OROLOGIO_FERMO, OROLOGIO_OK };
StatoOrologio statoOrologio = OROLOGIO_ASSENTE;
unsigned long prossimoOrologio = 0;   // quando cercarlo di nuovo, se non risponde

// --- LAVAGGIO (la pompa, ora l'animazione sul display) ---
bool lavaggio = false;
unsigned long lavaggioDa = 0;
char statoLavato[lora::STATO + 1] = "";   // ultimo stato dell'impianto già gestito (nella flash)

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
  if (preferenze.isKey("lavato")) {
    preferenze.getString("lavato", statoLavato, sizeof(statoLavato));
  }
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

void salvaStatoLavato() {
  Preferences preferenze;
  preferenze.begin(PREFERENZE, false);
  preferenze.putString("lavato", statoLavato);
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
  statoLavato[0] = '\0';
  senzaRisposta = 0;
  attesaRisposta = false;
  misuraInviata = false;
  cercaDa = millis();
  prossimaAdesione = millis();
  Serial.println("Il nodo è tornato come nuovo: cerco un master");
}

// --- OROLOGIO DS1307 (I2C, indirizzo 0x68) ---
// I registri 0-6 sono secondi, minuti, ore, giorno della settimana, giorno, mese e anno (00-99), in BCD: ogni cifra
// decimale in 4 bit. Il bit 7 dei secondi (CH) ferma l'oscillatore: è acceso in un modulo mai regolato.
// L'orologio tiene l'ora UTC; il display la mostra nell'ora italiana.

uint8_t daBcd(uint8_t valore) {
  return (valore >> 4) * 10 + (valore & 0x0F);
}

uint8_t inBcd(uint8_t valore) {
  return ((valore / 10) << 4) | (valore % 10);
}

// Giorni dall'1/1/1970 (algoritmo days_from_civil di Howard Hinnant)
int32_t giorniDal1970(int anno, int mese, int giorno) {
  anno -= mese <= 2;
  int era = (anno >= 0 ? anno : anno - 399) / 400;
  int annoDellEra = anno - era * 400;
  int mesePrimavera = mese > 2 ? mese - 3 : mese + 9;   // l'anno comincia a marzo: febbraio è l'ultimo mese
  int giornoDellAnno = (153 * mesePrimavera + 2) / 5 + giorno - 1;
  int giornoDellEra = annoDellEra * 365 + annoDellEra / 4 - annoDellEra / 100 + giornoDellAnno;
  return era * 146097 + giornoDellEra - 719468;
}

// Legge l'ora: secondi Unix UTC, 0 se l'orologio è fermo o ha un'ora non valida
StatoOrologio leggiDs1307(uint32_t &secondi) {
  secondi = 0;
  uint8_t registri[7];
  busOrologio.beginTransmission(INDIRIZZO_DS1307);
  busOrologio.write(0);   // dal registro 0
  if (busOrologio.endTransmission() != 0 || busOrologio.requestFrom(INDIRIZZO_DS1307, (uint8_t)7) != 7) {
    return OROLOGIO_ASSENTE;
  }
  for (int i = 0; i < 7; i++) {
    registri[i] = busOrologio.read();
  }
  if (registri[0] & 0x80) {
    return OROLOGIO_FERMO;
  }
  int sec = daBcd(registri[0] & 0x7F);
  int min = daBcd(registri[1] & 0x7F);
  int ora = (registri[2] & 0x40) ? daBcd(registri[2] & 0x1F) % 12 + ((registri[2] & 0x20) ? 12 : 0)   // 12 ore
                                 : daBcd(registri[2] & 0x3F);
  int giorno = daBcd(registri[4] & 0x3F);
  int mese = daBcd(registri[5] & 0x1F);
  int anno = 2000 + daBcd(registri[6]);
  if (sec > 59 || min > 59 || ora > 23 || giorno < 1 || giorno > 31 || mese < 1 || mese > 12) {
    return OROLOGIO_FERMO;
  }
  uint32_t risultato = (uint32_t)giorniDal1970(anno, mese, giorno) * 86400UL + ora * 3600UL + min * 60UL + sec;
  if (risultato < lora::ORA_MINIMA) {
    return OROLOGIO_FERMO;
  }
  secondi = risultato;
  return OROLOGIO_OK;
}

// Scrive l'ora (UTC) e accende l'oscillatore
bool regolaDs1307(uint32_t secondi) {
  time_t istante = secondi;
  struct tm data;
  gmtime_r(&istante, &data);
  busOrologio.beginTransmission(INDIRIZZO_DS1307);
  busOrologio.write(0);
  busOrologio.write(inBcd(data.tm_sec));           // CH = 0: l'oscillatore parte
  busOrologio.write(inBcd(data.tm_min));
  busOrologio.write(inBcd(data.tm_hour));          // formato 24 ore
  busOrologio.write(data.tm_wday + 1);             // 1-7
  busOrologio.write(inBcd(data.tm_mday));
  busOrologio.write(inBcd(data.tm_mon + 1));
  busOrologio.write(inBcd(data.tm_year - 100));    // tm_year conta dal 1900
  return busOrologio.endTransmission() == 0;
}

// Ora dell'orologio, 0 se non risponde o non è regolato. Se non risponde lo si cerca di nuovo dopo un minuto, così
// il nodo non aspetta ogni volta la fine del tentativo sull'I2C
uint32_t oraOrologio() {
  if (statoOrologio == OROLOGIO_ASSENTE && !scaduto(prossimoOrologio)) {
    return 0;
  }
  uint32_t secondi = 0;
  StatoOrologio prima = statoOrologio;
  statoOrologio = leggiDs1307(secondi);
  if (statoOrologio == OROLOGIO_ASSENTE) {
    prossimoOrologio = millis() + RIPROVA_OROLOGIO;
    if (prima != OROLOGIO_ASSENTE) {
      Serial.println("Orologio DS1307: non risponde (il pin 5V ha tensione solo con USB o power bank)");
    }
  }
  return secondi;
}

bool oraNota() {
  return time(nullptr) >= (time_t)lora::ORA_MINIMA;
}

void impostaOra(uint32_t secondi) {
  struct timeval adesso = {(time_t)secondi, 0};
  settimeofday(&adesso, nullptr);
}

// Ora del nodo: quella dell'orologio, o quella ricevuta dal master se l'orologio non risponde; 0 se non la sa
uint32_t adesso() {
  uint32_t secondi = oraOrologio();
  if (secondi != 0) {
    return secondi;
  }
  return oraNota() ? (uint32_t)time(nullptr) : 0;
}

String oraLocale(uint32_t secondi) {
  if (secondi == 0) {
    return "--:--:--";
  }
  time_t istante = secondi;
  struct tm data;
  localtime_r(&istante, &data);
  char testo[12];
  strftime(testo, sizeof(testo), "%H:%M:%S", &data);
  return testo;
}

// Il master sa l'ora da internet (NTP): con quella il nodo regola l'ora di sistema e l'orologio, se è fermo o
// sbaglia di più di 2 secondi. Il pacchetto impiega meno di un decimo di secondo ad arrivare
void riceviOra(uint32_t secondi) {
  if (secondi < lora::ORA_MINIMA) {
    return;
  }
  int64_t scarto = (int64_t)time(nullptr) - secondi;
  if (!oraNota() || scarto > SCARTO_OROLOGIO || scarto < -SCARTO_OROLOGIO) {
    impostaOra(secondi);
  }
  uint32_t orologio = oraOrologio();
  if (statoOrologio == OROLOGIO_ASSENTE) {
    return;
  }
  scarto = (int64_t)orologio - secondi;
  if (orologio == 0 || scarto > SCARTO_OROLOGIO || scarto < -SCARTO_OROLOGIO) {
    if (regolaDs1307(secondi)) {
      statoOrologio = OROLOGIO_OK;
      Serial.printf("Orologio DS1307 regolato con l'ora del master: %s\n", oraLocale(secondi).c_str());
    }
  }
}

// --- SENSORI ---

// Microsecondi in cui il pin DATA del DHT11 resta al livello dato, -1 se ci resta più di massimo
long durataLivello(int livello, unsigned long massimo) {
  unsigned long inizio = micros();
  while (digitalRead(pinDht) == livello) {
    if (micros() - inizio > massimo) {
      return -1;
    }
  }
  return (long)(micros() - inizio);
}

// Lettura del DHT11. Il pin DATA è open drain: la scheda lo porta a LOW per 20 ms per chiedere una misura, poi lo
// lascia alla resistenza di pull-up. Il DHT11 risponde con un LOW e un HIGH di 80 µs, poi trasmette 40 bit: ognuno è
// un LOW di 50 µs seguito da un HIGH di 26-28 µs (0) o di 70 µs (1). I 5 byte sono umidità, decimi di umidità,
// temperatura, decimi di temperatura (bit 7: sotto zero) e la somma dei primi quattro, per controllo
bool leggiDht11(int16_t &decimi) {
  uint8_t dati[5] = {0, 0, 0, 0, 0};
  digitalWrite(pinDht, LOW);
  delay(20);
  bool riuscito = true;
  // Si misurano microsecondi: per i 4 ms della trasmissione niente interrupt su questo core
  portENTER_CRITICAL(&sezioneDht);
  digitalWrite(pinDht, HIGH);
  if (durataLivello(HIGH, 100) < 0 || durataLivello(LOW, 100) < 0 || durataLivello(HIGH, 100) < 0) {
    riuscito = false;
  }
  for (int i = 0; riuscito && i < 40; i++) {
    long basso = durataLivello(LOW, 100);
    long alto = durataLivello(HIGH, 100);
    if (basso < 0 || alto < 0) {
      riuscito = false;
    } else {
      dati[i / 8] = (dati[i / 8] << 1) | (alto > basso ? 1 : 0);
    }
  }
  portEXIT_CRITICAL(&sezioneDht);
  if (!riuscito || ((dati[0] + dati[1] + dati[2] + dati[3]) & 0xFF) != dati[4] || dati[0] == 0 || dati[2] > 60) {
    return false;
  }
  decimi = dati[2] * 10 + (dati[3] & 0x7F) % 10;
  if (dati[3] & 0x80) {
    decimi = -decimi;
  }
  return true;
}

// Luce in lux, dalla tensione del partitore (media di 16 letture). analogReadMilliVolts usa la taratura dell'ADC
// scritta in fabbrica nel chip: con l'attenuazione di default legge da 0 a circa 3,1 V
float leggiLuce() {
  uint32_t somma = 0;
  for (int i = 0; i < CAMPIONI_LUCE; i++) {
    somma += analogReadMilliVolts(pinLuce);
  }
  float millivolt = (float)somma / CAMPIONI_LUCE;
  if (millivolt < 1) {
    return 0;   // buio completo, o fotoresistenza scollegata
  }
  if (millivolt >= TENSIONE_PARTITORE_MV - 1) {
    return LUX_MASSIMI;
  }
  float resistenza = R_FISSA_LUCE * (TENSIONE_PARTITORE_MV - millivolt) / millivolt;
  return min(LUX_MASSIMI, 10.0f * powf(LDR_R10 / resistenza, 1.0f / LDR_GAMMA));
}

bool temperaturaValida() {
  return temperatura != lora::TEMPERATURA_ASSENTE && millis() - temperaturaDa < VALIDITA_TEMPERATURA;
}

void leggiSensori() {
  if (!scaduto(prossimaLettura)) {
    return;
  }
  prossimaLettura = millis() + INTERVALLO_SENSORI;
  static bool dhtFunziona = true;   // sul monitor seriale solo i cambiamenti
  int16_t decimi = 0;
  if (leggiDht11(decimi)) {
    temperatura = decimi;
    temperaturaDa = millis();
    if (!dhtFunziona) {
      Serial.println("DHT11: di nuovo ok");
    }
    dhtFunziona = true;
  } else if (!temperaturaValida()) {
    // Una lettura persa ogni tanto è normale: la temperatura sparisce solo dopo 10 secondi senza letture
    temperatura = lora::TEMPERATURA_ASSENTE;
    if (dhtFunziona) {
      Serial.println("DHT11: non risponde (controlla DATA su GPIO6, 3V3 e GND)");
    }
    dhtFunziona = false;
  }
  luce = leggiLuce();
}

String testoTemperatura(int16_t decimi) {
  char testo[16];
  snprintf(testo, sizeof(testo), "%s%d,%d °C", decimi < 0 ? "-" : "", abs(decimi) / 10, abs(decimi) % 10);
  return testo;
}

String testoLuce(float lux) {
  char testo[16];
  snprintf(testo, sizeof(testo), lux < 10 ? "%.1f lux" : "%.0f lux", lux);
  if (lux < 10) {
    char *punto = strchr(testo, '.');
    if (punto != nullptr) {
      *punto = ',';
    }
  }
  return testo;
}

// --- LAVAGGIO ---

bool daLavare(const char *codice) {
  return strcmp(codice, "DRT") == 0 || strcmp(codice, "FLT") == 0;
}

void avviaLavaggio(const char *motivo) {
  if (!lavaggio) {
    Serial.printf("Lavaggio dei pannelli (%s): pompa accesa per %lu secondi\n", motivo, DURATA_LAVAGGIO / 1000);
  }
  lavaggio = true;
  lavaggioDa = millis();
}

void fermaLavaggio() {
  if (lavaggio) {
    lavaggio = false;
    Serial.println("Lavaggio finito: pompa spenta");
  }
}

void controllaLavaggio() {
  if (lavaggio && millis() - lavaggioDa >= DURATA_LAVAGGIO) {
    fermaLavaggio();
  }
}

// Stato dell'impianto ricevuto dal master: con un nuovo DRT o FLT si lava, una volta sola anche dopo un riavvio
void gestisciStato() {
  if (!assegnato() || strcmp(stato, statoLavato) == 0) {
    return;
  }
  if (daLavare(stato)) {
    avviaLavaggio(stato);
  } else {
    fermaLavaggio();
  }
  strcpy(statoLavato, stato);
  salvaStatoLavato();
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
  prossimaMisura = millis() + ATTESA_RISPOSTA;
  Serial.printf("Entrato nella rete del master (community %lu)\n", (unsigned long)rete);
}

// --- BATTITI E MISURE ---

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

// Ogni 10 secondi l'ultima lettura dei sensori, senza risposta. Non mentre si aspetta la risposta a un battito:
// la radio non può trasmettere e ricevere insieme
void misure() {
  if (attesaRisposta || !scaduto(prossimaMisura)) {
    return;
  }
  lora::Misura misura;
  misura.ora = adesso();
  misura.temperatura = temperaturaValida() ? temperatura : lora::TEMPERATURA_ASSENTE;
  misura.luce = (uint32_t)lroundf(luce);
  misura.segnali = lavaggio ? lora::SEGNALE_LAVAGGIO : 0;
  uint8_t dati[lora::DATI_MISURA];
  lora::scriviMisura(dati, misura);
  inviaAlMaster(lora::MISURA, dati, sizeof(dati));
  ultimaMisura = millis();
  misuraInviata = true;
  prossimaMisura = millis() + INTERVALLO_MISURA + random(VARIAZIONE_MISURA);
  Serial.printf("Misura inviata: %s, %s, ora %s%s\n",
                misura.temperatura == lora::TEMPERATURA_ASSENTE ? "temperatura assente"
                                                                : testoTemperatura(misura.temperatura).c_str(),
                testoLuce(luce).c_str(), oraLocale(misura.ora).c_str(), lavaggio ? ", lavaggio in corso" : "");
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
  if (intestazione.tipo == lora::RISPOSTA &&
      (lunghezzaDati == lora::STATO || lunghezzaDati == lora::STATO + lora::ORA)) {
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
    if (lunghezzaDati == lora::STATO + lora::ORA) {
      riceviOra(lora::leggi32(dati + lora::STATO));
    }
    gestisciStato();
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
    unsigned long durata = millis() - pulsantePremutoDa;
    if (durata >= PRESSIONE_RESET) {
      if (abbinato) {
        inviaAlMaster(lora::USCITA, (const uint8_t *)"", 0);   // Il master lo toglie dal sito
      }
      dimentica();
    } else if (durata >= 30 && durata < PRESSIONE_PROVA) {   // sotto i 30 ms è un rimbalzo del contatto
      avviaLavaggio("prova con PRG");
    }
  }
}

unsigned long durataPressione() {
  return pulsantePremuto ? millis() - pulsantePremutoDa : 0;
}

// Lampeggi del LED (vedi l'inizio dello sketch)
bool ledAcceso() {
  unsigned long ora = millis();
  if (durataPressione() >= PRESSIONE_RESET) {
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

// --- DISPLAY ---

const char *descrizioneStato() {
  if (strcmp(stato, "OK") == 0) return "Impianto: OK";
  if (strcmp(stato, "DRT") == 0) return "Pannelli sporchi";
  if (strcmp(stato, "FLT") == 0) return "Probabile guasto";
  if (strcmp(stato, "BRK") == 0) return "Impianto guasto";
  return "Stato sconosciuto";
}

void mostraPulsante(unsigned long durata) {
  bool reset = durata >= PRESSIONE_RESET;
  schermo.setFont(u8g2_font_6x10_tf);
  schermo.drawStr(0, 0, reset ? "Rilascia ora:" : "Tieni premuto 10 s:");
  schermo.drawStr(0, 14, "il nodo esce dalla");
  schermo.drawStr(0, 26, "rete e torna nuovo");
  int pieno = min(128UL, (durata - PRESSIONE_AVVISO) * 128UL / (PRESSIONE_RESET - PRESSIONE_AVVISO));
  schermo.drawFrame(0, 54, 128, 8);
  schermo.drawBox(0, 54, pieno, 8);
}

// Pannello visto in prospettiva: un trapezio di 3 x 6 celle, dal bordo alto (y 44) a quello basso (y 63)
const int PANNELLO_ALTO = 44;
const int PANNELLO_BASSO = 63;

int bordoSinistro(int y) {
  return 14 - (12 * (y - PANNELLO_ALTO)) / (PANNELLO_BASSO - PANNELLO_ALTO);
}

int bordoDestro(int y) {
  return 113 + (14 * (y - PANNELLO_ALTO)) / (PANNELLO_BASSO - PANNELLO_ALTO);
}

void disegnaPannello() {
  const int righe[] = {PANNELLO_ALTO, 50, 56, PANNELLO_BASSO};
  for (int y : righe) {
    schermo.drawHLine(bordoSinistro(y), y, bordoDestro(y) - bordoSinistro(y) + 1);
  }
  for (int i = 0; i <= 6; i++) {
    int alto = bordoSinistro(PANNELLO_ALTO) + i * (bordoDestro(PANNELLO_ALTO) - bordoSinistro(PANNELLO_ALTO)) / 6;
    int basso = bordoSinistro(PANNELLO_BASSO) + i * (bordoDestro(PANNELLO_BASSO) - bordoSinistro(PANNELLO_BASSO)) / 6;
    schermo.drawLine(alto, PANNELLO_ALTO, basso, PANNELLO_BASSO);
  }
}

// Al posto del LED blu della pompa: un tubo con 8 ugelli spruzza l'acqua a ventaglio sul pannello, le gocce
// schizzano dove lo toccano e l'acqua scorre sulle celle
void mostraLavaggio() {
  const int UGELLI = 8;
  const int PARTENZA = 14;                        // le gocce escono dagli ugelli
  const int CADUTA = PANNELLO_ALTO - PARTENZA;    // pixel fino al pannello
  const int VELOCITA = 70;                        // pixel al secondo
  unsigned long t = millis() - lavaggioDa;
  long resto = ((long)DURATA_LAVAGGIO - (long)min(t, DURATA_LAVAGGIO) + 999) / 1000;
  schermo.setFont(u8g2_font_5x7_tf);
  schermo.drawStr(0, 0, "Lavaggio pannelli");
  char testo[8];
  snprintf(testo, sizeof(testo), "%ld s", resto);
  schermo.drawStr(128 - schermo.getStrWidth(testo), 0, testo);
  schermo.drawBox(0, 9, 128, 3);   // il tubo
  disegnaPannello();
  for (int n = 0; n < UGELLI; n++) {
    int ugello = 8 + n * 16;
    schermo.drawBox(ugello - 1, 12, 3, 2);
    for (int g = 0; g < 3; g++) {
      int percorso = (int)((t * VELOCITA / 1000 + g * CADUTA / 3 + n * 5) % CADUTA);
      int x = ugello + (percorso * (g - 1)) / 5;   // tre getti: a sinistra, dritto, a destra
      int y = PARTENZA + percorso;
      schermo.drawVLine(x, y, 3);
      if (percorso >= CADUTA - 4) {   // lo schizzo sul pannello
        schermo.drawPixel(x - 2, PANNELLO_ALTO - 2);
        schermo.drawPixel(x + 2, PANNELLO_ALTO - 2);
        schermo.drawPixel(x - 3, PANNELLO_ALTO - 3);
        schermo.drawPixel(x + 3, PANNELLO_ALTO - 3);
      }
    }
  }
  // L'acqua che scorre: un'onda in ogni fila di celle, in XOR per vederla anche sopra le righe del pannello
  schermo.setDrawColor(2);
  const int onde[] = {47, 53, 59};
  for (int f = 0; f < 3; f++) {
    for (int x = bordoSinistro(onde[f]) + 1; x < bordoDestro(onde[f]); x++) {
      float fase = (x - t * 0.03f) / 4.0f + f * 1.7f;
      schermo.drawPixel(x, onde[f] + (int)lroundf(1.5f * sinf(fase)));
    }
  }
  schermo.setDrawColor(1);
}

void mostraStato() {
  schermo.setFont(u8g2_font_5x7_tf);
  String titolo = "Tetto " + idLeggibile(idNodo);
  schermo.drawStr(0, 0, titolo.c_str());
  String ora = oraLocale(adesso());
  schermo.drawStr(128 - schermo.getStrWidth(ora.c_str()), 0, ora.c_str());
  schermo.drawHLine(0, 9, 128);

  schermo.drawStr(0, 15, "Temperatura");
  schermo.drawStr(0, 28, "Luce");
  schermo.setFont(u8g2_font_6x10_tf);
  String valore = temperaturaValida() ? testoTemperatura(temperatura) : String("DHT11 ?");
  schermo.drawUTF8(128 - schermo.getUTF8Width(valore.c_str()), 13, valore.c_str());
  valore = testoLuce(luce);
  schermo.drawUTF8(128 - schermo.getUTF8Width(valore.c_str()), 26, valore.c_str());

  schermo.setFont(u8g2_font_5x7_tf);
  const char *riga;
  if (identifica) {
    riga = (millis() / 250) % 2 ? "> IDENTIFICA <" : "";
  } else if (statoRadio != RADIOLIB_ERR_NONE) {
    riga = "Radio LoRa: errore";
  } else if (!abbinato) {
    riga = "Cerco un master...";
  } else if (senzaRisposta >= BATTITI_SENZA_RISPOSTA) {
    riga = "Il master non risponde";
  } else if (!assegnato()) {
    riga = "Da assegnare sul sito";
  } else {
    riga = descrizioneStato();
  }
  schermo.drawStr(0, 43, riga);

  char testo[32] = "";
  if (statoOrologio == OROLOGIO_ASSENTE) {
    strcpy(testo, "Orologio: non risponde");
  } else if (statoOrologio == OROLOGIO_FERMO) {
    strcpy(testo, "Orologio: da regolare");
  } else if (misuraInviata) {
    snprintf(testo, sizeof(testo), "Misura inviata %lu s fa", (millis() - ultimaMisura) / 1000);
  }
  schermo.drawStr(0, 54, testo);
}

void mostra() {
  static unsigned long ultimo = 0;
  if (millis() - ultimo < (lavaggio ? INTERVALLO_ANIMAZIONE : INTERVALLO_DISPLAY)) {
    return;
  }
  ultimo = millis();
  schermo.clearBuffer();
  if (durataPressione() >= PRESSIONE_AVVISO) {
    mostraPulsante(durataPressione());
  } else if (lavaggio) {
    mostraLavaggio();
  } else {
    mostraStato();
  }
  schermo.sendBuffer();
}

// ===================================================================================================================

void setup() {
  Serial.begin(115200);
  pinMode(pinLed, OUTPUT);
  pinMode(pinPulsante, INPUT_PULLUP);
  // Il display è alimentato da Vext: va acceso prima di inizializzarlo
  pinMode(pinVext, OUTPUT);
  digitalWrite(pinVext, LOW);
  delay(50);
  schermo.begin();
  schermo.setFontPosTop();
  setenv("TZ", FUSO_ORARIO, 1);
  tzset();

  // DATA del DHT11: open drain, a riposo HIGH grazie alla resistenza di pull-up
  pinMode(pinDht, OUTPUT_OPEN_DRAIN | PULLUP);
  digitalWrite(pinDht, HIGH);
  busOrologio.begin(pinOrologioSda, pinOrologioScl, 100000);
  uint32_t ora = oraOrologio();
  if (ora != 0) {
    impostaOra(ora);
    Serial.printf("Orologio DS1307: %s\n", oraLocale(ora).c_str());
  } else if (statoOrologio == OROLOGIO_FERMO) {
    Serial.println("Orologio DS1307: da regolare, lo regola il master con la prima risposta");
  } else {
    Serial.println("Orologio DS1307: non risponde (il pin 5V ha tensione solo con USB o power bank)");
  }

  idNodo = lora::idScheda();
  Serial.printf("Nodo sul tetto %s\n", idLeggibile(idNodo).c_str());
  carica();
  avviaRadio();
  cercaDa = millis();
  if (abbinato) {
    Serial.printf("Nella rete del master (community %lu)\n", (unsigned long)rete);
    prossimoBattito = millis() + random(VARIAZIONE);
    prossimaMisura = prossimoBattito + ATTESA_RISPOSTA;
  } else {
    Serial.println("Nodo nuovo: cerco un master (sul sito, \"Aggiungi nodi\" nella pagina della community)");
  }
}

void loop() {
  controllaPulsante();
  gestisciRadio();
  leggiSensori();
  controllaLavaggio();
  if (statoRadio == RADIOLIB_ERR_NONE) {
    if (abbinato) {
      battiti();
      misure();
    } else {
      cercaMaster();
    }
  }
  digitalWrite(pinLed, ledAcceso() ? HIGH : LOW);
  mostra();
}
