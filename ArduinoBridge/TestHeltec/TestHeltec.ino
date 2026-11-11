// Collaudo di una scheda Heltec WiFi LoRa 32 V3 dopo la saldatura dei pin: controlla microcontrollore, memoria
// flash, display, LED, batteria, Wi-Fi e radio LoRa, e cerca i difetti di saldatura dei pin liberi del connettore.
// I risultati compaiono sul monitor seriale (115200 baud) e sul display; il pulsante PRG cambia pagina:
//   1. Riepilogo   esito di ogni controllo
//   2. Saldature   i pin già toccati con un filo collegato a GND (G) o a 3V3 (3); ! = pin in corto
//   3. LoRa        ping scambiati con l'altra scheda, con la qualità del segnale
// RST ripete il collaudo.
//
// Avvitare sempre l'antenna LoRa prima di accendere la scheda: trasmettere senza antenna può rovinare la radio.
//
// Arduino IDE: scheda "Heltec WiFi LoRa 32(V3)" del pacchetto esp32 di Espressif; librerie RadioLib (8.0 o
// successiva: con la 7 la radio si configura in un altro modo) e U8g2, dal Gestore librerie.
#include <Preferences.h>
#include <RadioLib.h>
#include <SPI.h>
#include <U8g2lib.h>
#include <WiFi.h>
#include <stdarg.h>

// --- PIN COLLEGATI SULLA SCHEDA (funzionano anche se il pin del connettore non è saldato) ---
const int pinLed = 35;          // LED bianco
const int pinVext = 36;         // LOW: accende l'uscita Vext (3,3 V), che alimenta anche il display
const int pinPulsante = 0;      // Pulsante PRG: LOW se premuto
const int pinBatteria = 1;      // Tensione della batteria attraverso un partitore
const int pinPartitore = 37;    // Collega il partitore della batteria: LOW sulla V3.1, HIGH sulla V3.0
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

// --- PIN LIBERI DEL CONNETTORE ---
// Quelli saldati, nell'ordine del connettore J3 (da GPIO7 verso GND) e poi J2. Mancano 20, 21, 26, 33, 34, 35,
// 36, 47 e 48 (non saldati), 0, 1 e 37 (pulsante e batteria), 43 e 44 (seriale verso il PC)
const int PIN_LIBERI[] = {7, 6, 5, 4, 3, 2, 38, 39, 40, 41, 42, 45, 46, 19};
const int NUMERO_PIN = sizeof(PIN_LIBERI) / sizeof(PIN_LIBERI[0]);

// --- PARAMETRI ---
const float RAPPORTO_PARTITORE = 4.9;   // Partitore della batteria: 390 kΩ + 100 kΩ

// Ping LoRa a 868,1 MHz, SF7, 125 kHz, con la potenza minima che basta nella stessa stanza. Un ping dura circa
// 60 ms: uno ogni 10-12 secondi è lo 0,6% del tempo, sotto il limite dell'1% della banda 868,0-868,6 MHz
const uint32_t FREQUENZA_KHZ = 868100;
const uint8_t SPREADING_FACTOR = 7;
const int8_t POTENZA_DBM = 0;
const unsigned long INTERVALLO_PING = 10000;
const char PREFISSO_PING[] = "SFTEST ";

const unsigned long INTERVALLO_BATTERIA = 10000;
const unsigned long INTERVALLO_DISPLAY = 250;
const unsigned long DURATA_LAMPO_LED = 100;
const int NUMERO_PAGINE = 3;

// Contatti visti dalla prova dei collegamenti, per ogni pin libero
const uint8_t CONTATTO_GND = 1;
const uint8_t CONTATTO_3V3 = 2;

U8G2_SSD1306_128X64_NONAME_F_HW_I2C schermo(U8G2_R0, pinOledReset, pinOledScl, pinOledSda);
SX1262 radio = new Module(pinLoraNss, pinLoraDio1, pinLoraReset, pinLoraBusy);

// --- RISULTATI ---
int errori = 0;
bool chipOk = false;
bool flashOk = false;
unsigned long avvii = 0;
float batteria = 0;             // V, 0 se non collegata
int retiWifi = 0;
int rssiWifi = 0;               // dBm della rete più forte
int16_t statoLora = RADIOLIB_ERR_UNKNOWN;
int problemiPin = 0;
bool anomalia[NUMERO_PIN];      // Pin in corto con un altro, con GND o con 3V3: non si prova il collegamento
uint8_t contatti[NUMERO_PIN];   // CONTATTO_GND e/o CONTATTO_3V3

// --- STATO ---
// La radio avvisa con un interrupt sia quando ha finito di trasmettere sia quando ha ricevuto un pacchetto
volatile bool radioPronta = false;
bool staTrasmettendo = false;
char idScheda[5] = "";
unsigned long inviati = 0;
unsigned long ricevuti = 0;
unsigned long ultimoPing = 0;
unsigned long attesaPing = INTERVALLO_PING;
char mittente[5] = "";
float ultimoRssi = 0;
float ultimoSnr = 0;
unsigned long ultimaRicezione = 0;

int pagina = 0;
unsigned long ultimoDisegno = 0;
unsigned long ultimaBatteria = 0;
bool ledAcceso = false;
unsigned long ledAccesoDa = 0;

void IRAM_ATTR suRadioPronta() {
  radioPronta = true;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println("=== Collaudo Heltec WiFi LoRa 32 V3 ===");

  pinMode(pinLed, OUTPUT);
  pinMode(pinPulsante, INPUT_PULLUP);
  // Il display è alimentato da Vext: va acceso prima di inizializzarlo
  pinMode(pinVext, OUTPUT);
  digitalWrite(pinVext, LOW);
  delay(100);
  schermo.begin();
  schermo.setFont(u8g2_font_5x7_tf);
  schermo.setFontPosTop();
  rapporto("VEDI", "Display: deve mostrare il collaudo in corso");

  avanzamento("Chip e memoria flash");
  controllaChip();
  controllaFlash();
  avanzamento("LED: 3 lampeggi");
  lampeggiaLed();
  avanzamento("Batteria");
  batteria = leggiBatteria();
  if (batteria > 0) {
    rapporto("OK", "Batteria: %.2f V", batteria);
  } else {
    rapporto("ATTENZIONE", "Batteria: nessuna lettura (non è collegata?)");
  }
  avanzamento("Wi-Fi: ricerca delle reti");
  controllaWifi();
  avanzamento("Radio LoRa");
  avviaLora();
  avanzamento("Cortocircuiti tra i pin");
  cercaCortocircuiti();

  Serial.println();
  if (errori == 0) {
    Serial.println("Collaudo automatico finito: nessun errore.");
  } else {
    Serial.printf("Collaudo automatico finito: %d errori.\n", errori);
  }
  Serial.println("Ora toccate ogni pin libero con un filo collegato a GND o a 3V3 (pagina Saldature).");
  Serial.println("Mai collegare il pin 5V a un GPIO: l'ESP32 regge al massimo 3,3 V.");
}

void loop() {
  gestisciPulsante();
  gestisciRadio();
  provaContatti();

  unsigned long adesso = millis();
  if (adesso - ultimaBatteria >= INTERVALLO_BATTERIA) {
    ultimaBatteria = adesso;
    batteria = leggiBatteria();
  }
  if (ledAcceso && adesso - ledAccesoDa >= DURATA_LAMPO_LED) {
    digitalWrite(pinLed, LOW);
    ledAcceso = false;
  }
  if (adesso - ultimoDisegno >= INTERVALLO_DISPLAY) {
    ultimoDisegno = adesso;
    disegnaSchermo();
  }
}

// --- CONTROLLI ALL'AVVIO ---

void controllaChip() {
  chipOk = strcmp(ESP.getChipModel(), "ESP32-S3") == 0;
  rapporto(chipOk ? "OK" : "ERRORE", "Chip %s rev. %d, %d core a %lu MHz, flash %lu MB, RAM libera %lu KB, %.0f °C",
           ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores(), (unsigned long)ESP.getCpuFreqMHz(),
           (unsigned long)(ESP.getFlashChipSize() / (1024 * 1024)), (unsigned long)(ESP.getFreeHeap() / 1024),
           temperatureRead());
}

// Conta gli avvii nella memoria flash (NVS): così si vede che la flash si scrive e si rilegge
void controllaFlash() {
  Preferences preferenze;
  preferenze.begin("collaudo", false);
  avvii = preferenze.getULong("avvii", 0) + 1;
  flashOk = preferenze.putULong("avvii", avvii) > 0 && preferenze.getULong("avvii", 0) == avvii;
  preferenze.end();
  rapporto(flashOk ? "OK" : "ERRORE", "Flash: scritta e riletta (avvio n. %lu)", avvii);
}

void lampeggiaLed() {
  for (int i = 0; i < 3; i++) {
    digitalWrite(pinLed, HIGH);
    delay(200);
    digitalWrite(pinLed, LOW);
    delay(200);
  }
  rapporto("VEDI", "LED bianco: deve aver lampeggiato 3 volte");
}

// Tensione della batteria in V, 0 se non è collegata. Il partitore si collega con LOW sulla V3.1 e con HIGH sulla
// V3.0: si prova prima LOW, poi HIGH, e vale la prima lettura plausibile per una LiPo
float leggiBatteria() {
  const int livelli[] = {LOW, HIGH};
  for (int livello : livelli) {
    pinMode(pinPartitore, OUTPUT);
    digitalWrite(pinPartitore, livello);
    delay(10);
    uint32_t millivolt = 0;
    for (int i = 0; i < 16; i++) {
      millivolt += analogReadMilliVolts(pinBatteria);
    }
    float volt = millivolt / 16.0 * RAPPORTO_PARTITORE / 1000.0;
    if (volt > 2.5 && volt < 4.6) {
      pinMode(pinPartitore, INPUT);
      return volt;
    }
  }
  pinMode(pinPartitore, INPUT);
  return 0;
}

void controllaWifi() {
  WiFi.mode(WIFI_STA);
  retiWifi = WiFi.scanNetworks();
  if (retiWifi < 0) {
    rapporto("ERRORE", "Wi-Fi: la ricerca delle reti non è riuscita (%d)", retiWifi);
    retiWifi = 0;
  }
  for (int i = 0; i < retiWifi; i++) {
    if (i == 0 || WiFi.RSSI(i) > rssiWifi) {
      rssiWifi = WiFi.RSSI(i);
    }
  }
  WiFi.scanDelete();
  WiFi.mode(WIFI_OFF);
  if (retiWifi > 0) {
    rapporto("OK", "Wi-Fi: %d reti, la più forte a %d dBm", retiWifi, rssiWifi);
  } else {
    rapporto("ATTENZIONE", "Wi-Fi: nessuna rete trovata (va bene solo se non ce ne sono)");
  }
}

void avviaLora() {
  // Identificativo della scheda nei ping: le ultime cifre del suo indirizzo MAC
  uint64_t mac = ESP.getEfuseMac();
  snprintf(idScheda, sizeof(idScheda), "%04X", (unsigned int)((mac >> 32) & 0xFFFF));

  SPI.begin(pinLoraSck, pinLoraMiso, pinLoraMosi, pinLoraNss);
  // La radio della Heltec V3 ha un oscillatore TCXO alimentato a 1,8 V
  radio.tcxoVoltage = RadioLibTCXOVoltage_t::Voltage1V8;
  ConfigLoRa_t configurazione;
  configurazione.frequency = RADIOLIB_UNIT_KILO(FREQUENZA_KHZ);
  configurazione.spreadingFactor = SPREADING_FACTOR;
  configurazione.codingRate = 5;
  configurazione.power = POTENZA_DBM;
  statoLora = radio.begin(configurazione);
  if (statoLora != RADIOLIB_ERR_NONE) {
    rapporto("ERRORE", "Radio LoRa SX1262: errore %d (-2: la radio non risponde)", statoLora);
    return;
  }
  radio.setIrqAction(suRadioPronta);
  radio.startReceive();
  rapporto("OK", "Radio LoRa SX1262: risponde (questa scheda nei ping: %s)", idScheda);
}

// Cerca i cortocircuiti tra i pin liberi (per esempio una goccia di stagno tra due pin vicini) e verso GND o 3V3.
// A riposo ogni pin segue la sua resistenza di pull: con la pull-up un pin basso è in corto con GND, con la
// pull-down un pin alto è in corto con 3V3. Poi, con le pull-down, si porta alto un pin alla volta: se si alza
// anche un altro, i due sono in corto.
// GPIO1 e GPIO37 non sono liberi ma stanno accanto a GPIO2, GPIO38 e GPIO46: si leggono soltanto, GPIO1 con la
// pull-down (sulla scheda va già a GND attraverso il partitore) e GPIO37 con la pull-up.
void cercaCortocircuiti() {
  problemiPin = 0;
  for (int i = 0; i < NUMERO_PIN; i++) {
    anomalia[i] = false;
  }

  impostaPin(INPUT_PULLUP);
  delay(5);
  segnalaPinFermi(LOW, "GND");
  impostaPin(INPUT_PULLDOWN);
  delay(5);
  segnalaPinFermi(HIGH, "3V3 o 5V");

  pinMode(pinBatteria, INPUT_PULLDOWN);
  delay(5);
  bool batteriaARiposo = digitalRead(pinBatteria) == LOW;
  for (int i = 0; i < NUMERO_PIN; i++) {
    if (anomalia[i]) {
      continue;
    }
    pinMode(PIN_LIBERI[i], OUTPUT);
    digitalWrite(PIN_LIBERI[i], HIGH);
    delayMicroseconds(100);
    for (int j = i + 1; j < NUMERO_PIN; j++) {
      if (!anomalia[j] && digitalRead(PIN_LIBERI[j]) == HIGH) {
        segnalaCorto(PIN_LIBERI[i], PIN_LIBERI[j]);
        anomalia[i] = true;
        anomalia[j] = true;
      }
    }
    if (batteriaARiposo && digitalRead(pinBatteria) == HIGH) {
      segnalaCorto(PIN_LIBERI[i], pinBatteria);
      anomalia[i] = true;
    }
    pinMode(PIN_LIBERI[i], INPUT_PULLDOWN);
    delayMicroseconds(100);
  }

  pinMode(pinPartitore, INPUT_PULLUP);
  delay(5);
  bool partitoreARiposo = digitalRead(pinPartitore) == HIGH;
  for (int i = 0; i < NUMERO_PIN; i++) {
    if (anomalia[i]) {
      continue;
    }
    pinMode(PIN_LIBERI[i], OUTPUT);
    digitalWrite(PIN_LIBERI[i], LOW);
    delayMicroseconds(100);
    if (partitoreARiposo && digitalRead(pinPartitore) == LOW) {
      segnalaCorto(PIN_LIBERI[i], pinPartitore);
      anomalia[i] = true;
    }
    pinMode(PIN_LIBERI[i], INPUT_PULLDOWN);
    delayMicroseconds(100);
  }

  impostaPin(INPUT);
  pinMode(pinBatteria, INPUT);
  pinMode(pinPartitore, INPUT);
  if (problemiPin == 0) {
    rapporto("OK", "Pin liberi: nessun cortocircuito");
  }
}

void segnalaPinFermi(int livelloSbagliato, const char *verso) {
  for (int i = 0; i < NUMERO_PIN; i++) {
    if (!anomalia[i] && digitalRead(PIN_LIBERI[i]) == livelloSbagliato) {
      anomalia[i] = true;
      problemiPin++;
      rapporto("ERRORE", "GPIO%d resta %s da solo: è in corto verso %s?", PIN_LIBERI[i],
               livelloSbagliato == HIGH ? "alto" : "basso", verso);
    }
  }
}

void segnalaCorto(int pinA, int pinB) {
  problemiPin++;
  rapporto("ERRORE", "GPIO%d e GPIO%d sono in corto: controllate lo stagno tra i due pin", pinA, pinB);
}

void impostaPin(int modo) {
  for (int i = 0; i < NUMERO_PIN; i++) {
    pinMode(PIN_LIBERI[i], modo);
  }
}

// --- PROVE CONTINUE ---

// Prova dei collegamenti: un filo da GND porta basso il pin nonostante la pull-up, uno da 3V3 lo porta alto
// nonostante la pull-down. Se il microcontrollore lo sente, la saldatura conduce. I pin in corto non si provano.
void provaContatti() {
  impostaPin(INPUT_PULLUP);
  delayMicroseconds(200);
  for (int i = 0; i < NUMERO_PIN; i++) {
    if (!anomalia[i] && digitalRead(PIN_LIBERI[i]) == LOW) {
      registraContatto(i, CONTATTO_GND);
    }
  }
  impostaPin(INPUT_PULLDOWN);
  delayMicroseconds(200);
  for (int i = 0; i < NUMERO_PIN; i++) {
    if (!anomalia[i] && digitalRead(PIN_LIBERI[i]) == HIGH) {
      registraContatto(i, CONTATTO_3V3);
    }
  }
}

void registraContatto(int i, uint8_t contatto) {
  if (contatti[i] & contatto) {
    return;
  }
  contatti[i] |= contatto;
  Serial.printf("Saldature: GPIO%d collegato a %s (%d/%d pin provati)\n", PIN_LIBERI[i],
                contatto == CONTATTO_GND ? "GND" : "3V3", pinProvati(), NUMERO_PIN);
}

int pinProvati() {
  int provati = 0;
  for (int i = 0; i < NUMERO_PIN; i++) {
    if (contatti[i] != 0) {
      provati++;
    }
  }
  return provati;
}

// Ogni 10-12 secondi invia un ping; nel frattempo ascolta quelli dell'altra scheda
void gestisciRadio() {
  if (statoLora != RADIOLIB_ERR_NONE) {
    return;
  }
  if (radioPronta) {
    radioPronta = false;
    if (staTrasmettendo) {
      radio.finishTransmit();
      staTrasmettendo = false;
    } else {
      riceviPing();
    }
    radio.startReceive();
  }
  // L'intervallo un po' casuale evita che le due schede trasmettano sempre nello stesso momento
  if (!staTrasmettendo && !radioPronta && millis() - ultimoPing >= attesaPing) {
    ultimoPing = millis();
    attesaPing = INTERVALLO_PING + random(2000);
    inviati++;
    char messaggio[24];
    snprintf(messaggio, sizeof(messaggio), "%s%s %lu", PREFISSO_PING, idScheda, inviati);
    if (radio.startTransmit(messaggio) == RADIOLIB_ERR_NONE) {
      staTrasmettendo = true;
    } else {
      radio.startReceive();
    }
  }
}

// Ping dell'altra scheda: "SFTEST <id> <numero>". Gli altri pacchetti LoRa della zona si ignorano
void riceviPing() {
  String messaggio;
  if (radio.readData(messaggio) != RADIOLIB_ERR_NONE || !messaggio.startsWith(PREFISSO_PING)) {
    return;
  }
  ricevuti++;
  ultimoRssi = radio.getRSSI();
  ultimoSnr = radio.getSNR();
  ultimaRicezione = millis();
  int inizio = strlen(PREFISSO_PING);
  messaggio.substring(inizio, inizio + 4).toCharArray(mittente, sizeof(mittente));
  Serial.printf("LoRa: ricevuto \"%s\", RSSI %.0f dBm, SNR %.1f dB\n", messaggio.c_str(), ultimoRssi, ultimoSnr);
  digitalWrite(pinLed, HIGH);
  ledAcceso = true;
  ledAccesoDa = millis();
}

void gestisciPulsante() {
  static int ultimoStato = HIGH;
  static unsigned long ultimoCambio = 0;
  int stato = digitalRead(pinPulsante);
  if (stato != ultimoStato && millis() - ultimoCambio > 50) {
    ultimoCambio = millis();
    ultimoStato = stato;
    if (stato == LOW) {
      pagina = (pagina + 1) % NUMERO_PAGINE;
    }
  }
}

// --- DISPLAY: 8 righe da 25 caratteri ---

void disegnaSchermo() {
  schermo.clearBuffer();
  if (pagina == 0) {
    paginaRiepilogo();
  } else if (pagina == 1) {
    paginaSaldature();
  } else {
    paginaLora();
  }
  riga(7, "PRG: pagina %d/%d", pagina + 1, NUMERO_PAGINE);
  schermo.sendBuffer();
}

void paginaRiepilogo() {
  riga(0, "COLLAUDO HELTEC V3  #%lu", avvii);
  riga(1, "Chip %s %s", ESP.getChipModel(), chipOk ? "OK" : "ERRORE");
  riga(2, "Flash %s", flashOk ? "OK" : "ERRORE");
  if (batteria > 0) {
    riga(3, "Batteria %.2f V", batteria);
  } else {
    riga(3, "Batteria non collegata");
  }
  if (retiWifi > 0) {
    riga(4, "Wi-Fi %d reti, %d dBm", retiWifi, rssiWifi);
  } else {
    riga(4, "Wi-Fi nessuna rete");
  }
  riga(5, "LoRa %s", statoLora == RADIOLIB_ERR_NONE ? "OK" : "ERRORE");
  if (problemiPin == 0) {
    riga(6, "Pin: nessun corto");
  } else {
    riga(6, "Pin: %d CORTI (seriale)", problemiPin);
  }
}

void paginaSaldature() {
  riga(0, "SALDATURE    G=GND 3=3V3");
  for (int r = 0; r * 4 < NUMERO_PIN; r++) {
    char testo[26] = "";
    for (int i = r * 4; i < r * 4 + 4 && i < NUMERO_PIN; i++) {
      char cella[7];
      snprintf(cella, sizeof(cella), "%2d:%-3s", PIN_LIBERI[i], statoContatto(i));
      strcat(testo, cella);
    }
    schermo.drawStr(0, (r + 1) * 8, testo);
  }
  riga(5, "Provati %d/%d   !=corto", pinProvati(), NUMERO_PIN);
}

const char *statoContatto(int i) {
  if (anomalia[i]) {
    return "!";
  }
  const char *stati[] = {"--", "G", "3", "G3"};
  return stati[contatti[i]];
}

void paginaLora() {
  riga(0, "LORA %.1f MHz SF%d %d dBm", FREQUENZA_KHZ / 1000.0, SPREADING_FACTOR, POTENZA_DBM);
  if (statoLora != RADIOLIB_ERR_NONE) {
    riga(2, "Radio: errore %d", statoLora);
    return;
  }
  riga(1, "Questa scheda: %s", idScheda);
  riga(2, "Ping inviati: %lu", inviati);
  riga(3, "Ping ricevuti: %lu", ricevuti);
  if (ricevuti == 0) {
    riga(5, "Accendete l'altra scheda");
    return;
  }
  riga(4, "Ultimo da %s, %lu s fa", mittente, (millis() - ultimaRicezione) / 1000);
  riga(5, "RSSI %.0f dBm SNR %.1f dB", ultimoRssi, ultimoSnr);
}

// Durante il collaudo mostra il controllo in corso
void avanzamento(const char *passo) {
  schermo.clearBuffer();
  riga(0, "COLLAUDO HELTEC V3");
  riga(3, "%s...", passo);
  schermo.sendBuffer();
}

void riga(int n, const char *formato, ...) {
  char testo[26];
  va_list argomenti;
  va_start(argomenti, formato);
  vsnprintf(testo, sizeof(testo), formato, argomenti);
  va_end(argomenti);
  schermo.drawStr(0, n * 8, testo);
}

// Una riga del rapporto sul monitor seriale; esito: OK, ERRORE, ATTENZIONE o VEDI (da controllare a vista)
void rapporto(const char *esito, const char *formato, ...) {
  char testo[160];
  va_list argomenti;
  va_start(argomenti, formato);
  vsnprintf(testo, sizeof(testo), formato, argomenti);
  va_end(argomenti);
  Serial.printf("[%s] %s\n", esito, testo);
  if (strcmp(esito, "ERRORE") == 0) {
    errori++;
  }
}
