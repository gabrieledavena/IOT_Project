// Master di una community, su una scheda Heltec WiFi LoRa 32 V3 con il display: l'unico dispositivo della
// community collegato a internet. I nodi sui tetti (RoofNode), uno per impianto e senza display, gli parlano via
// LoRa e lui pubblica per loro sul broker MQTT. Nulla va scritto nello sketch: tutto si configura con il telefono.
//
//   1. Wi-Fi. La scheda nuova crea una propria rete (Solar-XXXX, con una password casuale) e il display ne mostra il
//      QR: inquadrandolo con la fotocamera il telefono si collega e apre da solo la pagina della scheda (captive
//      portal, come il Wi-Fi di un albergo; altrimenti http://192.168.4.1). Lì il tecnico sceglie la rete di casa e
//      ne scrive la password; la scheda la prova tenendo aperta la propria rete, dice sulla pagina se funziona e
//      solo allora la salva nella flash e spegne la propria rete.
//   2. Installazione. La scheda chiede al server un codice da master (POST /sp/devices/pairing/, role=gateway) e
//      mostra il QR della pagina di abbinamento con il codice: il tecnico lo scansiona (o scrive il codice nella
//      pagina della community) e sceglie la community. Appena ha scelto la scheda riceve username (gw-<id della
//      community>) e token e si collega al broker.
//   3. Nodi, come in una rete Zigbee. Dal sito il tecnico preme "Aggiungi nodi" (comando permit_join): per 3 minuti il
//      master accetta i nodi nuovi che sente, concorda con ognuno una chiave (ProtocolloLora.h) e lo segnala al
//      server, dove il tecnico lo assegna al suo impianto. Da allora il master pubblica la presenza del nodo sul
//      topic connection dell'impianto e gli inoltra lo stato dell'impianto e l'ora (NTP), con cui il nodo regola
//      il suo orologio.
// Il server è descritto in backend/src/SP/pairing.py e SP/gateways.py; il monitor seriale (115200 baud) mostra
// ogni passo.
//
// Il master gestisce tutta la community e non ha sensori: ogni nodo sul tetto misura il suo impianto (produzione
// dalla pinza amperometrica, temperatura, luce) e gliene manda una misura ogni 10 secondi. Il display mostra la
// produzione della community e lo stato degli impianti; PRG sfoglia una pagina per impianto.
// Delle misure di ogni nodo assegnato a un impianto il master fa la media di ogni minuto e la pubblica sul topic
// telemetry dell'impianto: il server la salva come faceva con bridge.py. A ogni battito risponde al nodo con lo
// stato dell'impianto, l'ora e, se il nodo non lo ha ancora, il nome dell'impianto da mostrare sul suo display.
//
// Pulsante PRG, con le istruzioni sul display mentre è premuto:
//   - premuto e rilasciato subito: la pagina seguente del display. Dopo quella generale c'è una pagina per ogni
//     impianto della community con un nodo (vero o virtuale): stato, ultima misura, nodo e ultimo lavaggio;
//   - tenuto 3 secondi: la scheda dimentica l'installazione e mostra un nuovo QR di abbinamento;
//   - tenuto 10 secondi: dimentica anche il Wi-Fi e riapre la configurazione (l'indirizzo del server resta).
// Se la rete salvata non risponde per 2 minuti (password cambiata, router spostato) la configurazione si riapre
// da sola, senza dimenticare la rete: se nessuno la usa per 5 minuti la scheda riprova la rete salvata.
//
// Avvitare sempre l'antenna LoRa prima di accendere la scheda: trasmettere senza antenna può rovinare la radio.
//
// Arduino IDE: scheda "Heltec WiFi LoRa 32(V3)" del pacchetto esp32 di Espressif (3.x: Wi-Fi, pagina web, DNS, QR e
// crittografia sono già inclusi); librerie U8g2, RadioLib (8 o successiva), PubSubClient (Nick O'Leary) e
// ArduinoJson (Benoit Blanchon, 7 o successiva), dal Gestore librerie.
#include <ArduinoJson.h>
#include <DNSServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <RadioLib.h>
#include <SPI.h>
#include <U8g2lib.h>
#include <WebServer.h>
#include <WiFi.h>
#include <time.h>
#include "ProtocolloLora.h"
#include "qrcode.h"

// Server proposto nella configurazione (si cambia in "Avanzate" e resta salvato). In un impianto vero sarebbe
// l'indirizzo fisso del server; per le prove è il PC con docker compose nella rete locale, porta 8000
const char SERVER_PREDEFINITO[] = "http://192.168.1.10:8000";

// --- PIN DELLA SCHEDA ---
const int pinVext = 36;         // LOW: accende l'uscita Vext (3,3 V), che alimenta anche il display
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

const unsigned long MISURA_RECENTE = 60000;   // l'ultima misura di un nodo, più vecchia, è segnata come vecchia

// --- IMPIANTI VIRTUALI (per le prove, con un solo nodo vero) ---
// Il master simula NODI_VIRTUALI nodi sul tetto in più, senza radio: entrano nella rete quando il tecnico preme
// "Aggiungi nodi" sul sito (lì compaiono come "virtuale sano", "virtuale sporco", "virtuale guasto") e si
// assegnano a un impianto come quelli veri. Hanno il tetto accanto a quello vero: temperatura e luce sono quelle
// dell'ultima misura del nodo vero, la produzione è quella della sua pinza scalata sulla potenza dell'impianto e
// moltiplicata per la resa dello scenario. Il master pubblica per loro presenza, misure e lavaggi come per un nodo
// vero, quindi il server li controlla davvero:
//   - sano:   produce come l'impianto vero;
//   - sporco: produce il 70%; il lavaggio lo riporta al 100% per 6 ore, poi si risporca;
//   - guasto: produce il 30%, anche dopo il lavaggio.
// Con NODI_VIRTUALI = 0 non ce n'è nessuno (un master vero)
const int NODI_VIRTUALI = 3;
enum Scenario : uint8_t { SANO, SPORCO, GUASTO };
const char *const NOMI_SCENARI[] = {"sano", "sporco", "guasto"};
const float RESA_SPORCO = 0.7;
const float RESA_GUASTO = 0.3;
const unsigned long RISPORCO = 6UL * 3600 * 1000;    // ms dopo il lavaggio: i pannelli sporchi tornano sporchi
const unsigned long INTERVALLO_VIRTUALE = 10000;    // una misura ogni 10 s, come il nodo vero
const unsigned long BATTITO_VIRTUALE = 60000;
const unsigned long DURATA_LAVAGGIO = 10000;        // come nel nodo sul tetto

// --- PARAMETRI ---
const unsigned long PRESSIONE_PAGINA = 1000;          // PRG premuto meno: pagina successiva del display
const unsigned long PRESSIONE_INSTALLAZIONE = 3000;   // PRG: dimentica l'installazione
const unsigned long PRESSIONE_WIFI = 10000;           // PRG: dimentica anche il Wi-Fi
const unsigned long ATTESA_RIPROVA = 10000;    // dopo un errore del server o del broker
const unsigned long ATTESA_WIFI = 120000;      // rete salvata che non risponde: si riapre la configurazione
const unsigned long DURATA_PORTALE = 300000;   // configurazione riaperta da sola e non usata: si riprova la rete
const unsigned long DURATA_PROVA = 20000;      // per collegarsi alla rete scelta sulla pagina
const int TENTATIVI_PROVA = 3;                 // disconnessioni dopo cui la prova è fallita anche prima
const unsigned long ATTESA_CHIUSURA = 8000;    // rete giusta: il telefono vede l'esito, poi la pagina si chiude
const unsigned long INTERVALLO_DISPLAY = 250;
const uint16_t TIMEOUT_HTTP = 5000;            // ms
const unsigned long ASSENZA_NODO = 210000;     // senza battiti per 3 minuti e mezzo il nodo è scollegato
const unsigned long ATTESA_CONFERMA = 120000;  // nodo appena entrato: il server lo salva e lo rimanda nei nodi
const unsigned long DURATA_RIMOZIONE = 600000; // nodo rimosso dal sito: glielo si dice al prossimo battito
const char PREFERENZE[] = "solarfamily";       // credenziali dell'installazione nella flash (Preferences)
const char PREFERENZE_RETE[] = "sf-rete";      // rete Wi-Fi e indirizzo del server
const char PREFERENZE_NODI[] = "sf-nodi";      // contatori dei pacchetti di ogni nodo
const char PREFISSO_USERNAME[] = "gw-";        // username gw-<id della community>
const char FUSO_ORARIO[] = "CET-1CEST,M3.5.0,M10.5.0/3";   // ora italiana, con l'ora legale
const char SERVER_NTP[] = "pool.ntp.org";
const char SERVER_NTP_RISERVA[] = "ntp1.inrim.it";      // l'Istituto Nazionale di Ricerca Metrologica
// Lettere e cifre che non si confondono (niente 0/O, 1/I/L): la password della rete della scheda si legge anche
// dal display e si scrive a mano
const char ALFABETO_PASSWORD[] = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";

// Il QR occupa la metà sinistra del display, 64 x 64 pixel. Con l'indirizzo IP del server nella rete locale è
// di 29 moduli per lato (versione 3), 2 pixel per modulo: oltre i 31 moduli ne avrebbe uno solo, difficile da
// leggere con il telefono
const int LATO_QR = 64;
const int VERSIONE_QR_MASSIMA = 6;
const int MODULI_MASSIMI = 4 * VERSIONE_QR_MASSIMA + 17;

U8G2_SSD1306_128X64_NONAME_F_HW_I2C schermo(U8G2_R0, pinOledReset, pinOledScl, pinOledSda);
SX1262 radio = new Module(pinLoraNss, pinLoraDio1, pinLoraReset, pinLoraBusy);
WiFiClient connessione;
PubSubClient mqtt(connessione);
WebServer portale(80);
DNSServer dns;

// --- RETE WI-FI ---
// Salvata nella flash dopo una prova riuscita. Senza rete la scheda apre la configurazione
String reteNome;
String retePassword;
String serverUrl;
bool wifiAssente = false;          // la rete salvata non risponde
unsigned long wifiAssenteDa = 0;

// --- CONFIGURAZIONE DEL WI-FI (la rete della scheda e la sua pagina) ---
enum Prova { PROVA_NESSUNA, PROVA_IN_CORSO, PROVA_RIUSCITA, PROVA_FALLITA };
bool portaleAperto = false;
unsigned long portaleApertoDa = 0;
String portaleNome;                // Solar-XXXX: le ultime cifre esadecimali del MAC
String portalePassword;            // casuale, nuova a ogni apertura
Prova prova = PROVA_NESSUNA;
unsigned long provaDa = 0;
String provaNome;
String provaPassword;
String provaServer;
String erroreProva;                // spiegazione per la pagina
String erroreBreve;                // per il display
volatile uint8_t motivoDisconnessione = 0;   // dagli eventi del Wi-Fi, che arrivano da un altro task
volatile int disconnessioni = 0;

// Reti trovate, dalla più forte
struct Rete {
  String nome;
  int rssi;
  bool protetta;
};
const int RETI_MASSIME = 20;
Rete reti[RETI_MASSIME];
int numeroReti = 0;
int canaleMigliore = 1;

// --- CREDENZIALI DELL'INSTALLAZIONE ---
// Ricevute dal server e salvate nella flash: restano dopo lo spegnimento. Senza username la scheda si abbina
String username;
String token;
String hostBroker;
uint16_t portaBroker = 1883;
String nomeCommunity;
uint32_t idCommunity = 0;          // dallo username: la "rete" dei pacchetti LoRa

// --- ABBINAMENTO IN CORSO ---
String codice;                   // vuoto: da chiedere
String segreto;                  // lo conosce solo la scheda: serve per ritirare le credenziali
unsigned long scadenzaCodice = 0;
unsigned long intervalloRichieste = 5000;
unsigned long prossimaRichiesta = 0;
bool qrLetto = false;            // il tecnico ha aperto la pagina del QR e sta scegliendo la community

// Moduli del QR mostrato (true: modulo scuro): quello della rete della scheda o quello dell'abbinamento
bool moduli[MODULI_MASSIMI][MODULI_MASSIMI];
int latoQr = 0;

// --- BROKER ---
unsigned long prossimoTentativo = 0;
int statoBroker = MQTT_DISCONNECTED;   // mqtt.state() dell'ultimo tentativo

String errore;                         // ultimo errore di rete o del server, "" se nessuno

// --- RADIO E NODI ---
volatile bool radioPronta = false;     // la radio ha ricevuto un pacchetto (interrupt)
int16_t statoRadio = RADIOLIB_ERR_UNKNOWN;
bool finestraAperta = false;           // "Aggiungi nodi": il master accetta nodi nuovi
unsigned long finestraFino = 0;
String ultimoEvento;                   // per il display: l'ultimo nodo entrato o uscito

// Un nodo della rete del master. L'elenco vero è sul server (topic config): qui ci sono anche i nodi appena
// entrati, finché il server non li rimanda, e quelli rimossi dal sito, finché non sono stati avvisati
struct Nodo {
  bool usato;
  uint32_t id;
  uint8_t chiave[lora::CHIAVE];
  int impianto;                // id dell'impianto, 0 se il nodo è da assegnare
  char stato[lora::STATO + 1]; // stato dell'impianto da inoltrare al nodo
  uint32_t ricevuto;           // contatore dell'ultimo pacchetto del nodo
  uint32_t inviato;            // contatore dell'ultimo pacchetto verso il nodo
  bool sentito;                // pubblicato "online" sul topic connection dell'impianto
  unsigned long sentitoDa;
  bool confermato;             // presente nell'elenco del server
  bool nellElenco;
  unsigned long entratoDa;
  bool daRimuovere;
  unsigned long rimozioneDa;
  bool misurato;               // ha mandato almeno una misura
  unsigned long misuraDa;      // millis() dell'ultima misura
  lora::Misura misura;         // l'ultima misura
  // Media del minuto in corso, per il topic telemetry dell'impianto
  uint32_t minuto;             // inizio del minuto (secondi Unix UTC), 0 se non ce n'è uno aperto
  float sommaTemperatura;      // °C
  int campioniTemperatura;
  float sommaLuce;             // lux
  float sommaPotenza;          // W
  int campioni;
  float potenza;               // W, della misura più recente (dalla pinza del nodo, o simulata)
  uint16_t improntaNome;       // del nome che il nodo conosce, dal suo ultimo battito
  bool improntaNota;
  char nomeImpianto[32];       // dall'elenco del server
  float potenzaMassima;        // kW, dal topic config dell'impianto; 0 se non ancora arrivata
  // Lavaggio dei pannelli: per un nodo vero lo dice la sua misura, per uno virtuale lo simula il master
  bool lavaggio;
  unsigned long lavaggioDa;
  uint32_t ultimoLavaggio;     // fine dell'ultimo lavaggio (secondi Unix UTC), 0 se non c'è stato
  // Nodo virtuale (vedi IMPIANTI VIRTUALI)
  bool virtuale;
  uint8_t scenario;
  char statoLavato[lora::STATO + 1];   // ultimo stato già gestito, come nel nodo sul tetto
  bool pulito;                 // scenario sporco: lavato da poco
  unsigned long pulitoDa;
  unsigned long prossimaMisuraVirtuale;
  unsigned long prossimoBattitoVirtuale;
};
const int NODI_MASSIMI = 16;
Nodo nodi[NODI_MASSIMI];
uint32_t ultimoMisurato = 0;   // il nodo dell'ultima misura, mostrata sul display

// --- MISURE DA PUBBLICARE ---
// Le medie dei minuti finiti aspettano qui il broker: senza internet ne restano le ultime 2 ore, in memoria (si
// perdono se il master si riavvia). Il server scarta i doppioni: una misura per impianto e minuto
struct MediaMinuto {
  int impianto;
  char testo[160];             // il JSON del topic telemetry
};
const int CODA_MASSIMA = 120;
MediaMinuto coda[CODA_MASSIMA];
int codaInizio = 0;
int codaLunghezza = 0;
unsigned long pubblicate = 0;
const unsigned long ATTESA_FINE_MINUTO = 15;   // s: un minuto senza misure nuove si chiude 15 s dopo la sua fine

// --- PINZA E ORA ---
bool ntpAvviato = false;

// Adesione in corso: il master ha risposto e aspetta il primo battito del nodo, protetto con la nuova chiave.
// La risposta resta pronta: se il nodo non l'ha ricevuta e richiede, gliela si rimanda uguale
struct Adesione {
  bool attiva;
  uint32_t nodo;
  uint8_t pubblicaNodo[lora::CHIAVE_PUBBLICA];
  uint8_t chiave[lora::CHIAVE];
  uint8_t risposta[lora::MASSIMO];
  size_t lunghezza;
  uint8_t tipo;
  float rssi;
  float snr;
  unsigned long da;
};
const int ADESIONI_MASSIME = 4;
Adesione adesioni[ADESIONI_MASSIME];

// --- PULSANTE E PAGINE DEL DISPLAY ---
// Pagina 0: la pagina generale; poi una per ogni impianto con un nodo, vero o virtuale. PRG premuto e rilasciato
// subito passa alla successiva, e dopo l'ultima si torna alla generale
int pagina = 0;
bool pulsantePremuto = false;
unsigned long pulsantePremutoDa = 0;

// ===================================================================================================================

String argomento(const char *nome) {
  return "solarfamily/gateways/" + String(idCommunity) + "/" + nome;
}

String argomentoImpianto(int impianto, const char *nome) {
  return "solarfamily/systems/" + String(impianto) + "/" + nome;
}

String codiceLeggibile() {
  return codice.substring(0, 4) + "-" + codice.substring(4);
}

String idLeggibile(uint32_t id) {
  char testo[9];
  snprintf(testo, sizeof(testo), "%08lX", (unsigned long)id);
  return testo;
}

bool scaduto(unsigned long istante) {
  return (long)(millis() - istante) >= 0;
}

// --- RETE E CREDENZIALI NELLA FLASH ---

void caricaRete() {
  Preferences preferenze;
  preferenze.begin(PREFERENZE_RETE, false);
  reteNome = preferenze.getString("nome", "");
  retePassword = preferenze.getString("password", "");
  serverUrl = preferenze.getString("server", SERVER_PREDEFINITO);
  preferenze.end();
}

void salvaRete() {
  Preferences preferenze;
  preferenze.begin(PREFERENZE_RETE, false);
  preferenze.putString("nome", reteNome);
  preferenze.putString("password", retePassword);
  preferenze.putString("server", serverUrl);
  preferenze.end();
}

void caricaCredenziali() {
  Preferences preferenze;
  preferenze.begin(PREFERENZE, false);
  username = preferenze.getString("username", "");
  token = preferenze.getString("token", "");
  hostBroker = preferenze.getString("host", "");
  portaBroker = preferenze.getUShort("porta", 1883);
  nomeCommunity = preferenze.getString("community", "");
  preferenze.end();
  idCommunity = username.startsWith(PREFISSO_USERNAME) ? username.substring(strlen(PREFISSO_USERNAME)).toInt() : 0;
}

void salvaCredenziali() {
  Preferences preferenze;
  preferenze.begin(PREFERENZE, false);
  preferenze.putString("username", username);
  preferenze.putString("token", token);
  preferenze.putString("host", hostBroker);
  preferenze.putUShort("porta", portaBroker);
  preferenze.putString("community", nomeCommunity);
  preferenze.end();
}

// La scheda dimentica l'installazione (e i nodi): chiede un nuovo codice e mostra il QR di abbinamento
void dimenticaCredenziali() {
  if (mqtt.connected()) {
    mqtt.publish(argomento("connection").c_str(), "offline", true);
    mqtt.disconnect();
  }
  Preferences preferenze;
  preferenze.begin(PREFERENZE, false);
  preferenze.clear();
  preferenze.end();
  username = token = hostBroker = nomeCommunity = codice = segreto = errore = ultimoEvento = "";
  idCommunity = 0;
  for (int i = 0; i < NODI_MASSIMI; i++) {
    nodi[i].usato = false;
  }
  for (int i = 0; i < ADESIONI_MASSIME; i++) {
    adesioni[i].attiva = false;
  }
  finestraAperta = false;
  prossimaRichiesta = millis();
  statoBroker = MQTT_DISCONNECTED;
  Serial.println("Installazione dimenticata: nuovo abbinamento");
}

// La scheda torna come nuova (resta solo l'indirizzo del server) e riapre la configurazione del Wi-Fi
void dimenticaRete() {
  dimenticaCredenziali();
  Preferences preferenze;
  preferenze.begin(PREFERENZE_RETE, false);
  preferenze.remove("nome");
  preferenze.remove("password");
  preferenze.end();
  reteNome = retePassword = "";
  Serial.println("Rete Wi-Fi dimenticata");
  if (!portaleAperto) {
    apriPortale();
  }
}

// --- CONFIGURAZIONE DEL WI-FI: LA RETE DELLA SCHEDA ---

void cercaReti() {
  int trovate = WiFi.scanNetworks();
  numeroReti = 0;
  int rssiMigliore = -1000;
  for (int i = 0; i < trovate; i++) {
    String nome = WiFi.SSID(i);
    if (WiFi.RSSI(i) > rssiMigliore) {
      rssiMigliore = WiFi.RSSI(i);
      canaleMigliore = WiFi.channel(i);
    }
    if (nome.isEmpty()) {
      continue;   // Rete nascosta: il nome si scrive a mano
    }
    int j = 0;
    while (j < numeroReti && reti[j].nome != nome) {
      j++;
    }
    if (j < numeroReti) {
      reti[j].rssi = max(reti[j].rssi, (int)WiFi.RSSI(i));   // La stessa rete da più punti di accesso
    } else if (numeroReti < RETI_MASSIME) {
      reti[numeroReti++] = {nome, (int)WiFi.RSSI(i), WiFi.encryptionType(i) != WIFI_AUTH_OPEN};
    }
  }
  WiFi.scanDelete();
  // Dalla più forte
  for (int i = 1; i < numeroReti; i++) {
    for (int j = i; j > 0 && reti[j].rssi > reti[j - 1].rssi; j--) {
      Rete scambio = reti[j];
      reti[j] = reti[j - 1];
      reti[j - 1] = scambio;
    }
  }
  Serial.printf("Reti trovate: %d\n", numeroReti);
}

void apriPortale() {
  if (WiFi.getMode() & WIFI_STA) {
    WiFi.disconnect();   // Smette di cercare la rete salvata: cambiare canale staccherebbe il telefono
  }
  WiFi.mode(WIFI_AP_STA);
  cercaReti();
  uint64_t mac = ESP.getEfuseMac();
  char nome[16];
  snprintf(nome, sizeof(nome), "Solar-%04X", (unsigned)((mac >> 32) & 0xFFFF));
  portaleNome = nome;
  portalePassword = "";
  for (int i = 0; i < 8; i++) {
    portalePassword += ALFABETO_PASSWORD[esp_random() % (sizeof(ALFABETO_PASSWORD) - 1)];
  }
  // Sul canale della rete più forte, probabilmente quella di casa: la prova non sposta la rete della scheda
  // su un altro canale, cosa che staccherebbe il telefono
  WiFi.softAP(portaleNome.c_str(), portalePassword.c_str(), canaleMigliore);
  dns.start(53, "*", WiFi.softAPIP());   // Ogni nome porta alla scheda: il telefono apre da solo la pagina
  portale.begin();
  // QR di una rete Wi-Fi, letto dalla fotocamera di iPhone e Android
  generaQr("WIFI:T:WPA;S:" + portaleNome + ";P:" + portalePassword + ";;");
  codice = segreto = "";   // Dopo la configurazione si chiede un nuovo codice di abbinamento
  prova = PROVA_NESSUNA;
  erroreProva = erroreBreve = "";
  portaleAperto = true;
  portaleApertoDa = millis();
  Serial.printf("Configurazione del Wi-Fi: rete %s, password %s, pagina http://%s\n", portaleNome.c_str(),
                portalePassword.c_str(), WiFi.softAPIP().toString().c_str());
}

void chiudiPortale() {
  portale.stop();
  dns.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  portaleAperto = false;
  Serial.println("Configurazione del Wi-Fi chiusa");
}

// Spiegazione di una prova fallita, dall'ultimo motivo di disconnessione del Wi-Fi
void spiegaProvaFallita() {
  switch (motivoDisconnessione) {
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
      erroreBreve = "Password errata";
      erroreProva = "La password non è giusta: controlla maiuscole e minuscole.";
      break;
    case WIFI_REASON_NO_AP_FOUND:
    case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY:
    case WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD:
      erroreBreve = "Rete non trovata";
      erroreProva = "La scheda non trova la rete: controlla il nome e che sia a 2,4 GHz (quelle a 5 GHz non le vede).";
      break;
    default:
      erroreBreve = "Non collegata";
      erroreProva = "La scheda non è riuscita a collegarsi (motivo " + String(motivoDisconnessione) + "): riprova.";
  }
}

void gestisciPortale() {
  dns.processNextRequest();
  portale.handleClient();
  if (prova == PROVA_IN_CORSO) {
    if (WiFi.status() == WL_CONNECTED) {
      prova = PROVA_RIUSCITA;
      provaDa = millis();
      reteNome = provaNome;
      retePassword = provaPassword;
      serverUrl = provaServer;
      salvaRete();
      Serial.printf("Wi-Fi %s: collegata (%s)\n", reteNome.c_str(), WiFi.localIP().toString().c_str());
    } else if (disconnessioni >= TENTATIVI_PROVA || millis() - provaDa > DURATA_PROVA) {
      prova = PROVA_FALLITA;
      spiegaProvaFallita();
      WiFi.disconnect();   // Smette di riprovare; la rete della scheda resta accesa
      Serial.printf("Wi-Fi %s: %s\n", provaNome.c_str(), erroreBreve.c_str());
    }
  } else if (prova == PROVA_RIUSCITA && millis() - provaDa > ATTESA_CHIUSURA) {
    chiudiPortale();
  } else if (!reteNome.isEmpty() && prova == PROVA_NESSUNA && WiFi.softAPgetStationNum() == 0 &&
             millis() - portaleApertoDa > DURATA_PORTALE) {
    // Riaperta perché la rete salvata non rispondeva, e nessuno l'ha usata: si riprova la rete salvata
    chiudiPortale();
    collegaWifi();
  }
}

// --- CONFIGURAZIONE DEL WI-FI: LA PAGINA ---

String html(const String &testo) {
  String risultato;
  risultato.reserve(testo.length());
  for (unsigned int i = 0; i < testo.length(); i++) {
    char c = testo[i];
    switch (c) {
      case '&': risultato += "&amp;"; break;
      case '<': risultato += "&lt;"; break;
      case '>': risultato += "&gt;"; break;
      case '"': risultato += "&quot;"; break;
      case '\'': risultato += "&#39;"; break;
      default: risultato += c;
    }
  }
  return risultato;
}

const char STILE[] =
    "body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f4f6f8;color:#222}"
    "main{max-width:420px;margin:auto;padding:20px}"
    "h1{font-size:1.3em;color:#2e7d4f;margin:8px 0 16px}"
    ".scheda{background:#fff;border-radius:16px;padding:20px;box-shadow:0 1px 4px #0002;margin-bottom:16px}"
    "label{display:block;font-weight:600;margin:14px 0 6px;font-size:.9em;color:#555}"
    "select,input{width:100%;box-sizing:border-box;padding:12px;font-size:1em;border:1px solid #ccc;border-radius:10px}"
    "input[type=checkbox]{width:auto;margin-right:6px}"
    "button{width:100%;margin-top:18px;padding:14px;font-size:1em;font-weight:700;color:#fff;background:#2e7d4f;"
    "border:0;border-radius:999px}"
    "summary{margin-top:16px;color:#555;font-size:.9em}a{color:#2e7d4f}"
    ".ok{color:#2e7d4f;font-weight:700}.errore{color:#c62828;font-weight:700}small{color:#777}";

void inviaPagina(const String &contenuto, bool aggiorna) {
  String pagina = "<!DOCTYPE html><html lang=\"it\"><head><meta charset=\"utf-8\">"
                  "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">";
  if (aggiorna) {
    pagina += "<meta http-equiv=\"refresh\" content=\"2\">";
  }
  pagina += "<title>SolarFamily - Wi-Fi</title><style>";
  pagina += STILE;
  pagina += "</style></head><body><main><h1>&#9728; SolarFamily</h1><div class=\"scheda\">";
  pagina += contenuto;
  pagina += "</div><small>Master " + html(portaleNome) + "</small></main></body></html>";
  portale.sendHeader("Cache-Control", "no-store");
  portale.send(200, "text/html; charset=utf-8", pagina);
}

String segnale(int rssi) {
  if (rssi > -60) return "ottimo";
  if (rssi > -70) return "buono";
  if (rssi > -80) return "debole";
  return "molto debole";
}

String modulo() {
  // Proposta: la rete appena provata, poi quella salvata, poi la più forte
  String scelta = !provaNome.isEmpty() ? provaNome : reteNome;
  String elenco;
  bool trovata = false;
  for (int i = 0; i < numeroReti; i++) {
    bool selezionata = reti[i].nome == scelta || (scelta.isEmpty() && i == 0);
    trovata = trovata || selezionata;
    elenco += "<option value=\"" + html(reti[i].nome) + "\"" + (selezionata ? " selected" : "") + ">" +
              html(reti[i].nome) + " (" + segnale(reti[i].rssi) + (reti[i].protetta ? "" : ", aperta") + ")</option>";
  }
  String nascosta = !scelta.isEmpty() && !trovata ? html(scelta) : "";
  String server = html(!provaServer.isEmpty() ? provaServer : serverUrl);
  bool serverDaScrivere = serverUrl == SERVER_PREDEFINITO;
  return "<form method=\"post\" action=\"/salva\">"
         "<label for=\"rete\">Rete Wi-Fi</label><select id=\"rete\" name=\"rete\">" + elenco +
         "<option value=\"\"" + (numeroReti == 0 ? " selected" : "") + ">Altra rete (scrivi il nome)</option></select>"
         "<label for=\"nascosta\">Nome di un'altra rete</label>"
         "<input id=\"nascosta\" name=\"nascosta\" value=\"" + nascosta + "\" maxlength=\"32\" autocapitalize=\"none\">"
         "<label for=\"password\">Password</label>"
         "<input id=\"password\" name=\"password\" type=\"password\" maxlength=\"63\" autocomplete=\"off\">"
         "<label><input type=\"checkbox\" onclick=\"password.type=this.checked?'text':'password'\">Mostra la password</label>"
         "<details" + String(serverDaScrivere ? " open" : "") + "><summary>Avanzate</summary>"
         "<label for=\"server\">Indirizzo del server</label>"
         "<input id=\"server\" name=\"server\" value=\"" + server + "\" inputmode=\"url\" autocapitalize=\"none\">"
         "<small>Il PC con docker compose nella stessa rete, con la porta: http://192.168.1.10:8000</small></details>"
         "<button type=\"submit\">Collega il master</button></form>"
         "<p><small>La scheda vede solo le reti a 2,4 GHz. <a href=\"/?cerca=1\">Cerca di nuovo le reti</a></small></p>";
}

void paginaPrincipale() {
  if (portale.hasArg("cerca") && prova != PROVA_IN_CORSO) {
    cercaReti();
  }
  if (prova == PROVA_IN_CORSO) {
    inviaPagina("<p><b>Collegamento a " + html(provaNome) + "&hellip;</b></p>"
                "<p><small>Ci vogliono pochi secondi. Se il telefono si stacca dalla rete " + html(portaleNome) +
                ", ricollegalo: il risultato compare anche sul display.</small></p>", true);
  } else if (prova == PROVA_RIUSCITA) {
    inviaPagina("<p class=\"ok\">&#10003; Master collegato a " + html(reteNome) + "</p>"
                "<p>Ora sul display compare il QR per installarlo sulla community: inquadralo con il telefono.</p>"
                "<p><small>Puoi chiudere questa pagina: la rete " + html(portaleNome) + " si spegne.</small></p>", false);
  } else {
    String testa = prova == PROVA_FALLITA
        ? "<p class=\"errore\">" + html(erroreProva) + "</p>"
        : !reteNome.isEmpty()
              ? "<p>Il master non riesce a collegarsi a <b>" + html(reteNome) + "</b>: scegli la rete o correggi la password.</p>"
              : "<p>Scegli la rete Wi-Fi a cui collegare il master della community.</p>";
    inviaPagina(testa + modulo(), false);
  }
}

void salvaModulo() {
  String nome = portale.arg("nascosta");
  nome.trim();
  if (nome.isEmpty()) {
    nome = portale.arg("rete");
  }
  String password = portale.arg("password");
  String server = portale.arg("server");
  server.trim();
  while (server.endsWith("/")) {
    server.remove(server.length() - 1);
  }
  provaNome = nome;
  provaServer = server;
  if (nome.isEmpty() || nome.length() > 32) {
    erroreProva = "Scegli una rete, o scrivi il nome di un'altra.";
  } else if (!password.isEmpty() && (password.length() < 8 || password.length() > 63)) {
    erroreProva = "La password di una rete Wi-Fi ha da 8 a 63 caratteri (nessuna se la rete è aperta).";
  } else if (!server.startsWith("http://") || server.length() <= 7 || server.indexOf(' ') >= 0) {
    erroreProva = "L'indirizzo del server comincia con http://, per esempio http://192.168.1.10:8000.";
  } else {
    erroreProva = "";
  }
  if (!erroreProva.isEmpty()) {
    prova = PROVA_FALLITA;
    erroreBreve = "Dati non validi";
  } else {
    provaPassword = password;
    disconnessioni = 0;
    motivoDisconnessione = 0;
    WiFi.begin(provaNome.c_str(), provaPassword.c_str());
    prova = PROVA_IN_CORSO;
    provaDa = millis();
    Serial.printf("Prova della rete %s\n", provaNome.c_str());
  }
  // Dopo un POST si torna alla pagina con un GET: ricaricandola non si rimanda il modulo
  portale.sendHeader("Location", "/", true);
  portale.send(303, "text/plain", "");
}

// Qualunque altro indirizzo porta alla pagina: è così che iPhone e Android capiscono che devono aprirla
void reindirizza() {
  portale.sendHeader("Location", "http://" + WiFi.softAPIP().toString() + "/", true);
  portale.send(302, "text/plain", "");
}

// --- WI-FI SALVATO ---

void collegaWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(reteNome.c_str(), retePassword.c_str());
  wifiAssente = false;
  Serial.printf("Wi-Fi: collegamento a %s\n", reteNome.c_str());
}

// La scheda riprova da sola; se la rete non risponde per troppo tempo si riapre la configurazione
void controllaWifi() {
  if (!wifiAssente) {
    wifiAssente = true;
    wifiAssenteDa = millis();
  } else if (millis() - wifiAssenteDa > ATTESA_WIFI) {
    Serial.printf("Wi-Fi %s non raggiungibile: riapro la configurazione\n", reteNome.c_str());
    apriPortale();
  }
}

// --- RICHIESTE AL SERVER ---

// POST di un form al server; la risposta (JSON) finisce in risposta. Restituisce lo status HTTP, o un numero
// negativo se il server non risponde
int richiesta(const char *percorso, const String &corpo, JsonDocument &risposta) {
  HTTPClient http;
  http.setConnectTimeout(TIMEOUT_HTTP);
  http.setTimeout(TIMEOUT_HTTP);
  if (!http.begin(serverUrl + percorso)) {
    return -1;
  }
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  int stato = http.POST(corpo);
  if (stato > 0 && deserializeJson(risposta, http.getString())) {
    risposta.clear();   // Non è JSON: per esempio la pagina di errore di un indirizzo sbagliato
  }
  http.end();
  return stato;
}

void erroreServer(int stato) {
  errore = stato > 0 ? "Server: risposta HTTP " + String(stato) : String("Server non raggiungibile");
  Serial.printf("%s (%s)\n", errore.c_str(), serverUrl.c_str());
}

// Il QR viene generato una volta: la funzione di ESP-IDF chiama salvaModuli con il risultato
void salvaModuli(esp_qrcode_handle_t qr) {
  latoQr = min(esp_qrcode_get_size(qr), MODULI_MASSIMI);
  for (int y = 0; y < latoQr; y++) {
    for (int x = 0; x < latoQr; x++) {
      moduli[y][x] = esp_qrcode_get_module(qr, x, y);
    }
  }
}

bool generaQr(const String &testo) {
  esp_qrcode_config_t configurazione = {};
  configurazione.display_func = salvaModuli;
  configurazione.max_qrcode_version = VERSIONE_QR_MASSIMA;
  configurazione.qrcode_ecc_level = ESP_QRCODE_ECC_LOW;   // la più alta che ci sta, per il resto è scelta da sola
  latoQr = 0;
  return esp_qrcode_generate(&configurazione, testo.c_str()) == ESP_OK && latoQr > 0;
}

// Nuovo codice da master; restituisce dopo quanti ms fare la richiesta seguente
unsigned long chiediCodice() {
  JsonDocument risposta;
  int stato = richiesta("/sp/devices/pairing/", "role=gateway", risposta);
  if (stato != 201 || !risposta["code"].is<const char *>()) {
    erroreServer(stato);
    return ATTESA_RIPROVA;
  }
  String indirizzo = risposta["url"].as<String>();
  if (!generaQr(indirizzo)) {
    errore = "URL troppo lungo per il QR";
    Serial.printf("%s: %s\n", errore.c_str(), indirizzo.c_str());
    return ATTESA_RIPROVA;
  }
  codice = risposta["code"].as<String>();
  segreto = risposta["secret"].as<String>();
  scadenzaCodice = millis() + risposta["expires_in"].as<unsigned long>() * 1000UL;
  intervalloRichieste = (risposta["interval"] | 5) * 1000UL;
  qrLetto = false;
  errore = "";
  Serial.printf("Codice %s: scansiona il QR o apri %s (QR di %d moduli)\n", codiceLeggibile().c_str(),
                indirizzo.c_str(), latoQr);
  return intervalloRichieste;
}

// Chiede se il tecnico ha scelto la community; restituisce dopo quanti ms fare la richiesta seguente
unsigned long chiediCredenziali() {
  JsonDocument risposta;
  int stato = richiesta("/sp/devices/pairing/credentials/", "code=" + codice + "&secret=" + segreto, risposta);
  if (stato == 202) {
    errore = "";
    if (!qrLetto && risposta["status"] == "scanned") {
      qrLetto = true;
      Serial.println("QR letto: il tecnico sta scegliendo la community");
    }
    return intervalloRichieste;
  }
  if (stato == 200 && risposta["status"] == "installed" && risposta["role"] == "gateway") {
    username = risposta["username"].as<String>();
    token = risposta["token"].as<String>();
    hostBroker = risposta["mqtt_host"].as<String>();
    portaBroker = risposta["mqtt_port"] | 1883;
    nomeCommunity = risposta["community"].as<String>();
    idCommunity = risposta["community_id"].as<uint32_t>();
    salvaCredenziali();
    codice = segreto = errore = "";
    prossimoTentativo = millis();
    Serial.printf("Installato come %s (%s): broker %s:%u\n", username.c_str(), nomeCommunity.c_str(),
                  hostBroker.c_str(), portaBroker);
    return 0;
  }
  if (stato == 404) {
    // Codice scaduto o già usato: se ne chiede subito un altro, con un nuovo QR
    Serial.println("Codice scaduto: ne chiedo un altro");
    codice = segreto = "";
    return 0;
  }
  erroreServer(stato);   // Si riprova con lo stesso codice
  return intervalloRichieste;
}

void abbina() {
  if (!scaduto(prossimaRichiesta)) {
    return;
  }
  unsigned long attesa = codice.isEmpty() ? chiediCodice() : chiediCredenziali();
  prossimaRichiesta = millis() + attesa;
}

// --- NODI: ELENCO E CONTATORI ---

Nodo *trovaNodo(uint32_t id) {
  for (int i = 0; i < NODI_MASSIMI; i++) {
    if (nodi[i].usato && nodi[i].id == id) {
      return &nodi[i];
    }
  }
  return nullptr;
}

// Id dei nodi virtuali di questo master: il suo id con le prime 4 cifre esadecimali cambiate, così sono sempre
// gli stessi (anche dopo un riavvio, quando tornano dall'elenco del server) e diversi da quelli di un altro master
uint32_t idVirtuale(int numero) {
  return lora::idScheda() ^ ((uint32_t)(numero + 1) << 28);
}

int numeroVirtuale(uint32_t id) {
  for (int numero = 0; numero < NODI_VIRTUALI; numero++) {
    if (id == idVirtuale(numero)) {
      return numero;
    }
  }
  return -1;
}

Nodo *nuovoNodo(uint32_t id, const uint8_t chiave[lora::CHIAVE]) {
  for (int i = 0; i < NODI_MASSIMI; i++) {
    if (!nodi[i].usato) {
      Nodo &nodo = nodi[i];
      nodo = Nodo();
      nodo.usato = true;
      nodo.id = id;
      memcpy(nodo.chiave, chiave, lora::CHIAVE);
      strcpy(nodo.stato, lora::NON_ASSEGNATO);
      int numero = numeroVirtuale(id);
      nodo.virtuale = numero >= 0;
      nodo.scenario = numero >= 0 ? numero % 3 : SANO;
      return &nodo;
    }
  }
  Serial.println("Nodi: elenco pieno");
  return nullptr;
}

// I contatori restano nella flash: dopo un riavvio del master il nodo non accetterebbe contatori già usati
void caricaContatori(Nodo &nodo) {
  Preferences preferenze;
  preferenze.begin(PREFERENZE_NODI, false);
  nodo.ricevuto = preferenze.getUInt(("r" + idLeggibile(nodo.id)).c_str(), 0);
  nodo.inviato = preferenze.getUInt(("t" + idLeggibile(nodo.id)).c_str(), 0);
  preferenze.end();
}

void salvaContatori(const Nodo &nodo) {
  Preferences preferenze;
  preferenze.begin(PREFERENZE_NODI, false);
  preferenze.putUInt(("r" + idLeggibile(nodo.id)).c_str(), nodo.ricevuto);
  preferenze.putUInt(("t" + idLeggibile(nodo.id)).c_str(), nodo.inviato);
  preferenze.end();
}

// Stato e configurazione (nome, potenza massima) dell'impianto, retained: arrivano subito
void iscriviImpianto(int impianto) {
  if (mqtt.connected()) {
    mqtt.subscribe(argomentoImpianto(impianto, "status").c_str(), 1);
    mqtt.subscribe(argomentoImpianto(impianto, "config").c_str(), 1);
  }
}

void disiscriviImpianto(int impianto) {
  if (impianto != 0 && mqtt.connected()) {
    mqtt.unsubscribe(argomentoImpianto(impianto, "status").c_str());
    mqtt.unsubscribe(argomentoImpianto(impianto, "config").c_str());
  }
}

void dimenticaNodo(Nodo &nodo) {
  disiscriviImpianto(nodo.impianto);
  nodo.usato = false;
}

int nodiDaAssegnare() {
  int numero = 0;
  for (int i = 0; i < NODI_MASSIMI; i++) {
    numero += nodi[i].usato && !nodi[i].daRimuovere && nodi[i].impianto == 0;
  }
  return numero;
}

int nodiInRete() {
  int numero = 0;
  for (int i = 0; i < NODI_MASSIMI; i++) {
    numero += nodi[i].usato && !nodi[i].daRimuovere;
  }
  return numero;
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
  radio.startReceive();
  Serial.println("Radio LoRa: in ascolto");
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

void inviaAlNodo(Nodo &nodo, uint8_t tipo, const uint8_t *dati, size_t lunghezza) {
  static const uint8_t NESSUN_DATO[1] = {0};
  if (nodo.virtuale) {
    return;   // Non c'è nessuna radio da cui farlo arrivare
  }
  if (dati == nullptr) {
    dati = NESSUN_DATO;
  }
  uint8_t pacchetto[lora::MASSIMO];
  size_t chiaro = lora::scriviIntestazione(pacchetto, tipo, idCommunity, nodo.id);
  nodo.inviato++;
  size_t totale = lora::proteggi(pacchetto, chiaro, nodo.inviato, lora::VERSO_NODO, nodo.chiave, dati, lunghezza);
  salvaContatori(nodo);
  trasmetti(pacchetto, totale);
}

bool oraNota() {
  return time(nullptr) >= (time_t)lora::ORA_MINIMA;
}

// Stato dell'impianto e, se il master sa l'ora (NTP), l'ora: il nodo ci regola il suo orologio
void gestisciStatoVirtuale(Nodo &nodo);

void inviaStato(Nodo &nodo) {
  if (nodo.virtuale) {
    gestisciStatoVirtuale(nodo);
    return;
  }
  // Stato, ora (0 se il master non la sa) e il nome dell'impianto, se il nodo non ha già quello giusto: lo dice
  // l'impronta del suo ultimo battito. Un nodo senza impianto non ha nome
  uint8_t dati[lora::STATO + lora::ORA + lora::NOME];
  memcpy(dati, nodo.stato, lora::STATO);
  lora::scrivi32(dati + lora::STATO, oraNota() ? (uint32_t)time(nullptr) : 0);
  size_t lunghezza = lora::STATO + lora::ORA;
  char nome[lora::NOME + 1];
  lora::accorciaNome(nodo.impianto != 0 ? nodo.nomeImpianto : "", nome);
  if (nome[0] != '\0' && (!nodo.improntaNota || nodo.improntaNome != lora::impronta(nome))) {
    memcpy(dati + lunghezza, nome, strlen(nome));
    lunghezza += strlen(nome);
  }
  inviaAlNodo(nodo, lora::RISPOSTA, dati, lunghezza);
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

// L'ora arriva da internet appena il Wi-Fi è collegato, e poi si aggiorna da sola ogni ora
void avviaNtp() {
  if (ntpAvviato || portaleAperto || WiFi.status() != WL_CONNECTED) {
    return;
  }
  configTzTime(FUSO_ORARIO, SERVER_NTP, SERVER_NTP_RISERVA);
  ntpAvviato = true;
  Serial.println("Ora: la chiedo a internet (NTP)");
}

// --- TESTI ---

String conVirgola(float valore, int decimali, const char *unita) {
  char testo[24];
  snprintf(testo, sizeof(testo), "%.*f%s", decimali, valore, unita);
  char *punto = strchr(testo, '.');
  if (punto != nullptr) {
    *punto = ',';
  }
  return testo;
}

String testoTemperatura(int16_t decimi) {
  if (decimi == lora::TEMPERATURA_ASSENTE) {
    return "-- °C";
  }
  char testo[16];
  snprintf(testo, sizeof(testo), "%s%d,%d °C", decimi < 0 ? "-" : "", abs(decimi) / 10, abs(decimi) % 10);
  return testo;
}

String testoLuce(uint32_t lux) {
  return String(lux) + " lux";
}

// Ora UTC come la vuole il server: 2026-11-11T14:32:00+00:00
String oraIso(uint32_t secondi) {
  time_t istante = secondi;
  struct tm data;
  gmtime_r(&istante, &data);
  char testo[32];
  strftime(testo, sizeof(testo), "%Y-%m-%dT%H:%M:%S+00:00", &data);
  return testo;
}

// --- MISURE AL SERVER: UNA MEDIA AL MINUTO PER IMPIANTO ---
// Come faceva bridge.py: il topic telemetry dell'impianto riceve la media di ogni minuto, con l'ora UTC del minuto
// (time_stamp), temperatura (°C), luce (lux) e potenza (kW). Per un nodo vero la potenza è quella misurata dalla
// sua pinza; per un nodo virtuale è quella simulata

void accodaMedia(int impianto, const char *testo) {
  if (codaLunghezza == CODA_MASSIMA) {
    codaInizio = (codaInizio + 1) % CODA_MASSIMA;   // Coda piena: si perde la più vecchia
    codaLunghezza--;
  }
  MediaMinuto &media = coda[(codaInizio + codaLunghezza) % CODA_MASSIMA];
  media.impianto = impianto;
  strncpy(media.testo, testo, sizeof(media.testo) - 1);
  media.testo[sizeof(media.testo) - 1] = '\0';
  codaLunghezza++;
}

// Il minuto del nodo è finito: la sua media va in coda per il broker
void chiudiMinuto(Nodo &nodo) {
  if (nodo.minuto == 0) {
    return;
  }
  uint32_t minuto = nodo.minuto;
  nodo.minuto = 0;
  if (nodo.impianto == 0) {
    return;   // Nodo non ancora assegnato: le misure non sono di nessun impianto
  }
  if (nodo.campioniTemperatura == 0) {
    Serial.printf("Nodo %s: minuto senza temperatura (DHT11), non inviato\n", idLeggibile(nodo.id).c_str());
    return;
  }
  JsonDocument messaggio;
  messaggio["time_stamp"] = oraIso(minuto);
  messaggio["temperature"] = roundf(nodo.sommaTemperatura / nodo.campioniTemperatura * 100) / 100;
  messaggio["lightness"] = roundf(nodo.sommaLuce / nodo.campioni * 10) / 10;
  messaggio["power"] = roundf(nodo.sommaPotenza / nodo.campioni / 1000 * 10000) / 10000;   // il server vuole kW
  messaggio["samples"] = nodo.campioni;
  char testo[160];
  serializeJson(messaggio, testo, sizeof(testo));
  accodaMedia(nodo.impianto, testo);
}

// Ogni misura del nodo entra nella media del suo minuto. Serve l'ora: quella dell'orologio del nodo, o quella
// del master se il nodo non la sa ancora
void aggiungiAlMinuto(Nodo &nodo, const lora::Misura &misura, float potenza) {
  uint32_t istante = misura.ora >= lora::ORA_MINIMA ? misura.ora : (oraNota() ? (uint32_t)time(nullptr) : 0);
  if (istante == 0) {
    return;   // Né il nodo né il master sanno l'ora: la misura non si può datare
  }
  uint32_t minuto = istante - istante % 60;
  if (nodo.minuto != 0 && minuto != nodo.minuto) {
    chiudiMinuto(nodo);
  }
  if (nodo.minuto == 0) {
    nodo.minuto = minuto;
    nodo.sommaTemperatura = nodo.sommaLuce = nodo.sommaPotenza = 0;
    nodo.campioniTemperatura = nodo.campioni = 0;
  }
  if (misura.temperatura != lora::TEMPERATURA_ASSENTE) {
    nodo.sommaTemperatura += misura.temperatura / 10.0f;
    nodo.campioniTemperatura++;
  }
  nodo.sommaLuce += misura.luce;
  nodo.sommaPotenza += potenza;
  nodo.campioni++;
}

// Un nodo che smette di mandare misure: il suo ultimo minuto si chiude lo stesso
void controllaMinuti() {
  if (!oraNota()) {
    return;
  }
  uint32_t adesso = time(nullptr);
  for (int i = 0; i < NODI_MASSIMI; i++) {
    if (nodi[i].usato && nodi[i].minuto != 0 && adesso >= nodi[i].minuto + 60 + ATTESA_FINE_MINUTO) {
      chiudiMinuto(nodi[i]);
    }
  }
}

// Pubblica le medie in coda, dalla più vecchia, finché il broker le accetta
void svuotaCoda() {
  while (codaLunghezza > 0 && mqtt.connected()) {
    MediaMinuto &media = coda[codaInizio];
    if (!mqtt.publish(argomentoImpianto(media.impianto, "telemetry").c_str(), media.testo)) {
      Serial.println("Telemetry: pubblicazione non riuscita, riprovo");
      return;
    }
    pubblicate++;
    Serial.printf("Telemetry dell'impianto %d: %s\n", media.impianto, media.testo);
    codaInizio = (codaInizio + 1) % CODA_MASSIMA;
    codaLunghezza--;
  }
}

// --- NODI: MESSAGGI AL SERVER ---

void pubblicaPresenza(Nodo &nodo, bool presente) {
  if (nodo.impianto == 0 || !mqtt.connected()) {
    return;
  }
  // Retained come quello del bridge: il server lo usa per mostrare il nodo collegato e l'ora dell'ultimo battito
  mqtt.publish(argomentoImpianto(nodo.impianto, "connection").c_str(), presente ? "online" : "offline", true);
  if (nodo.sentito != presente) {
    Serial.printf("Nodo %s (impianto %d): %s\n", idLeggibile(nodo.id).c_str(), nodo.impianto,
                  presente ? "collegato" : "non risponde");
  }
  nodo.sentito = presente;
}

void pubblicaEvento(const char *evento, Nodo &nodo, const Adesione *adesione) {
  JsonDocument messaggio;
  messaggio["event"] = evento;
  messaggio["node"] = idLeggibile(nodo.id);
  if (adesione != nullptr || (nodo.virtuale && strcmp(evento, "node_joined") == 0)) {
    char chiave[2 * lora::CHIAVE + 1];
    lora::esadecimale(nodo.chiave, lora::CHIAVE, chiave);
    messaggio["key"] = chiave;
  }
  if (adesione != nullptr) {
    messaggio["kind"] = adesione->tipo == lora::NODO_TETTO ? "roof" : "other";
    messaggio["rssi"] = adesione->rssi;
    messaggio["snr"] = adesione->snr;
  } else if (nodo.virtuale && strcmp(evento, "node_joined") == 0) {
    messaggio["kind"] = String("virtuale ") + NOMI_SCENARI[nodo.scenario];   // Così lo mostra il sito
  }
  String testo;
  serializeJson(messaggio, testo);
  if (!mqtt.connected() || !mqtt.publish(argomento("events").c_str(), testo.c_str())) {
    Serial.printf("Evento %s del nodo %s non inviato al server\n", evento, idLeggibile(nodo.id).c_str());
  }
}

// Inizio e fine del lavaggio, sul topic events dell'impianto come faceva bridge.py: con pump_off il server sa
// quando sono stati lavati i pannelli, e un'ora dopo verifica se un probabile guasto era solo sporco
void pubblicaLavaggio(Nodo &nodo, bool acceso) {
  if (!acceso) {
    nodo.ultimoLavaggio = oraNota() ? (uint32_t)time(nullptr) : 0;
  }
  if (nodo.impianto == 0) {
    return;
  }
  JsonDocument messaggio;
  messaggio["event"] = acceso ? "pump_on" : "pump_off";
  if (oraNota()) {
    messaggio["at"] = oraIso(time(nullptr));
  }
  String testo;
  serializeJson(messaggio, testo);
  bool inviato = mqtt.connected() && mqtt.publish(argomentoImpianto(nodo.impianto, "events").c_str(), testo.c_str());
  Serial.printf("Impianto %d (nodo %s%s): lavaggio %s%s\n", nodo.impianto, idLeggibile(nodo.id).c_str(),
                nodo.virtuale ? " virtuale" : "", acceso ? "iniziato" : "finito", inviato ? "" : ", non inviato al server");
}

// Una misura, di un nodo vero o virtuale: per il display e per la media del minuto
void registraMisura(Nodo &nodo, const lora::Misura &misura, float potenza) {
  nodo.sentitoDa = millis();
  nodo.misura = misura;
  nodo.misurato = true;
  nodo.misuraDa = millis();
  nodo.potenza = potenza;
  aggiungiAlMinuto(nodo, misura, potenza);
  if (nodo.virtuale) {
    return;   // Il lavaggio di un nodo virtuale lo segue la simulazione
  }
  ultimoMisurato = nodo.id;   // La pagina generale mostra il nodo vero
  bool lavaggio = misura.segnali & lora::SEGNALE_LAVAGGIO;
  if (lavaggio != nodo.lavaggio) {
    nodo.lavaggio = lavaggio;
    pubblicaLavaggio(nodo, lavaggio);
  }
}

// --- IMPIANTI VIRTUALI ---

// Il nodo vero più recente: i virtuali hanno il tetto accanto al suo
Nodo *nodoVero() {
  Nodo *nodo = ultimoMisurato != 0 ? trovaNodo(ultimoMisurato) : nullptr;
  return nodo != nullptr && nodo->misurato && millis() - nodo->misuraDa < MISURA_RECENTE ? nodo : nullptr;
}

float resaVirtuale(const Nodo &nodo) {
  if (nodo.scenario == GUASTO) {
    return RESA_GUASTO;
  }
  return nodo.scenario == SPORCO && !nodo.pulito ? RESA_SPORCO : 1.0f;
}

// La pinza del nodo vero misura il suo impianto: un vicino sano produce gli stessi kW per kW installato
float potenzaVirtuale(const Nodo &nodo) {
  Nodo *vero = nodoVero();
  float scala = vero != nullptr && vero->potenzaMassima > 0 && nodo.potenzaMassima > 0
                    ? nodo.potenzaMassima / vero->potenzaMassima : 1.0f;
  return (vero != nullptr ? vero->potenza : 0) * scala * resaVirtuale(nodo);
}

// Temperatura e luce del nodo vero (o 20 °C e 500 lux se non c'è), con qualche decimo di grado e qualche
// punto percentuale di luce di differenza per tetto
lora::Misura misuraVirtuale(const Nodo &nodo) {
  Nodo *vero = nodoVero();
  int16_t temperatura = vero != nullptr && vero->misura.temperatura != lora::TEMPERATURA_ASSENTE
                            ? vero->misura.temperatura : 200;
  uint32_t luce = vero != nullptr ? vero->misura.luce : 500;
  int scarto = (int)nodo.scenario - 1;   // -1, 0, +1
  lora::Misura misura;
  misura.ora = oraNota() ? (uint32_t)time(nullptr) : 0;
  misura.temperatura = temperatura + 3 * scarto;
  misura.luce = luce * (100 + 3 * scarto) / 100;
  misura.segnali = nodo.lavaggio ? lora::SEGNALE_LAVAGGIO : 0;
  return misura;
}

// Come il nodo sul tetto: con un nuovo DRT o FLT lava i pannelli, una volta per cambiamento
void gestisciStatoVirtuale(Nodo &nodo) {
  if (strcmp(nodo.stato, lora::NON_ASSEGNATO) == 0 || strcmp(nodo.stato, nodo.statoLavato) == 0) {
    return;
  }
  if ((strcmp(nodo.stato, "DRT") == 0 || strcmp(nodo.stato, "FLT") == 0) && !nodo.lavaggio) {
    nodo.lavaggio = true;
    nodo.lavaggioDa = millis();
    pubblicaLavaggio(nodo, true);
  }
  strcpy(nodo.statoLavato, nodo.stato);
}

// "Aggiungi nodi" sul sito: i nodi virtuali che il master non ha ancora entrano nella rete
void aggiungiNodiVirtuali() {
  for (int numero = 0; numero < NODI_VIRTUALI; numero++) {
    uint32_t id = idVirtuale(numero);
    if (trovaNodo(id) != nullptr) {
      continue;
    }
    uint8_t chiave[lora::CHIAVE];
    esp_fill_random(chiave, sizeof(chiave));   // Il server la vuole; senza radio non serve a nulla
    Nodo *nodo = nuovoNodo(id, chiave);
    if (nodo == nullptr) {
      return;
    }
    nodo->entratoDa = millis();
    ultimoEvento = idLeggibile(id) + " virtuale";
    Serial.printf("Nodo virtuale %s (%s) entrato nella rete\n", idLeggibile(id).c_str(), NOMI_SCENARI[nodo->scenario]);
    pubblicaEvento("node_joined", *nodo, nullptr);
  }
}

// Battiti, misure e lavaggi dei nodi virtuali
void simulaNodiVirtuali() {
  for (int i = 0; i < NODI_MASSIMI; i++) {
    Nodo &nodo = nodi[i];
    if (!nodo.usato || !nodo.virtuale || nodo.daRimuovere) {
      continue;
    }
    if (scaduto(nodo.prossimoBattitoVirtuale)) {
      nodo.sentitoDa = millis();
      pubblicaPresenza(nodo, true);
      nodo.prossimoBattitoVirtuale = millis() + BATTITO_VIRTUALE;
    }
    if (nodo.lavaggio && millis() - nodo.lavaggioDa >= DURATA_LAVAGGIO) {
      nodo.lavaggio = false;
      pubblicaLavaggio(nodo, false);
      if (nodo.scenario == SPORCO) {
        nodo.pulito = true;
        nodo.pulitoDa = millis();
      }
    }
    if (nodo.pulito && millis() - nodo.pulitoDa >= RISPORCO) {
      nodo.pulito = false;
      Serial.printf("Nodo virtuale %s: i pannelli sono di nuovo sporchi\n", idLeggibile(nodo.id).c_str());
    }
    if (scaduto(nodo.prossimaMisuraVirtuale)) {
      registraMisura(nodo, misuraVirtuale(nodo), potenzaVirtuale(nodo));
      nodo.prossimaMisuraVirtuale = millis() + INTERVALLO_VIRTUALE;
    }
  }
}

// --- NODI: PACCHETTI RICEVUTI ---

// Un nodo nuovo chiede di entrare: si accetta solo con la finestra "Aggiungi nodi" aperta
void riceviAdesione(const uint8_t *pacchetto, size_t lunghezza, const lora::Intestazione &intestazione, float rssi,
                    float snr) {
  if (!finestraAperta || idCommunity == 0 || lunghezza != lora::INTESTAZIONE + lora::CHIAVE_PUBBLICA + 1) {
    return;
  }
  const uint8_t *pubblicaNodo = pacchetto + lora::INTESTAZIONE;
  Adesione *adesione = nullptr;
  for (int i = 0; i < ADESIONI_MASSIME; i++) {
    if (adesioni[i].attiva && adesioni[i].nodo == intestazione.nodo) {
      adesione = &adesioni[i];
    }
  }
  if (adesione != nullptr && memcmp(adesione->pubblicaNodo, pubblicaNodo, lora::CHIAVE_PUBBLICA) == 0) {
    trasmetti(adesione->risposta, adesione->lunghezza);   // Il nodo non ha ricevuto l'accettazione
    return;
  }
  if (adesione == nullptr) {
    adesione = &adesioni[0];   // Un posto libero, o il più vecchio
    for (int i = 0; i < ADESIONI_MASSIME; i++) {
      if (!adesioni[i].attiva) {
        adesione = &adesioni[i];
        break;
      }
      if (adesioni[i].da < adesione->da) {
        adesione = &adesioni[i];
      }
    }
  }
  lora::CoppiaChiavi coppia;
  if (!lora::generaCoppia(coppia) ||
      !lora::calcolaChiave(coppia.privata, pubblicaNodo, idCommunity, intestazione.nodo, pubblicaNodo,
                           coppia.pubblica, adesione->chiave)) {
    Serial.println("Adesione: chiave non calcolata");
    return;
  }
  memset(coppia.privata, 0, sizeof(coppia.privata));
  size_t chiaro = lora::scriviIntestazione(adesione->risposta, lora::ACCETTAZIONE, idCommunity, intestazione.nodo);
  memcpy(adesione->risposta + chiaro, coppia.pubblica, lora::CHIAVE_PUBBLICA);
  chiaro += lora::CHIAVE_PUBBLICA;
  uint8_t vuoto[1] = {0};
  adesione->lunghezza = lora::proteggi(adesione->risposta, chiaro, 0, lora::VERSO_NODO, adesione->chiave, vuoto, 0);
  adesione->attiva = adesione->lunghezza > 0;
  adesione->nodo = intestazione.nodo;
  memcpy(adesione->pubblicaNodo, pubblicaNodo, lora::CHIAVE_PUBBLICA);
  adesione->tipo = pacchetto[lora::INTESTAZIONE + lora::CHIAVE_PUBBLICA];
  adesione->rssi = rssi;
  adesione->snr = snr;
  adesione->da = millis();
  Serial.printf("Adesione del nodo %s (RSSI %.0f dBm): accettato, aspetto il suo primo battito\n",
                idLeggibile(intestazione.nodo).c_str(), rssi);
  trasmetti(adesione->risposta, adesione->lunghezza);
}

// Pacchetto protetto di un nodo della rete, o il primo battito di un nodo appena accettato
void riceviDalNodo(const uint8_t *pacchetto, size_t lunghezza, const lora::Intestazione &intestazione) {
  uint8_t dati[lora::MASSIMO];
  size_t lunghezzaDati = 0;
  uint32_t contatore = 0;
  Nodo *nodo = trovaNodo(intestazione.nodo);
  if (nodo != nullptr && lora::apri(pacchetto, lunghezza, lora::INTESTAZIONE, lora::VERSO_MASTER, nodo->chiave,
                                    contatore, dati, lunghezzaDati)) {
    if (contatore <= nodo->ricevuto) {
      Serial.printf("Nodo %s: pacchetto ripetuto scartato\n", idLeggibile(nodo->id).c_str());
      return;
    }
    nodo->ricevuto = contatore;
    salvaContatori(*nodo);
  } else {
    Adesione *adesione = nullptr;
    for (int i = 0; i < ADESIONI_MASSIME; i++) {
      if (adesioni[i].attiva && adesioni[i].nodo == intestazione.nodo) {
        adesione = &adesioni[i];
      }
    }
    if (adesione == nullptr || intestazione.tipo != lora::BATTITO ||
        !lora::apri(pacchetto, lunghezza, lora::INTESTAZIONE, lora::VERSO_MASTER, adesione->chiave, contatore, dati,
                    lunghezzaDati)) {
      return;   // Chiave sconosciuta: un nodo di un'altra rete, o rimosso
    }
    // Il nodo ha la chiave giusta: è entrato. Se c'era già (ha rifatto l'adesione) riparte da capo
    if (nodo != nullptr) {
      dimenticaNodo(*nodo);
    }
    nodo = nuovoNodo(intestazione.nodo, adesione->chiave);
    if (nodo == nullptr) {
      return;
    }
    nodo->ricevuto = contatore;
    nodo->inviato = 0;   // L'accettazione ha usato lo 0
    nodo->entratoDa = millis();
    salvaContatori(*nodo);
    adesione->attiva = false;
    ultimoEvento = idLeggibile(nodo->id) + " entrato";
    Serial.printf("Nodo %s entrato nella rete\n", idLeggibile(nodo->id).c_str());
    pubblicaEvento("node_joined", *nodo, adesione);
  }

  if (intestazione.tipo == lora::BATTITO) {
    if (lunghezzaDati >= lora::DATI_BATTITO) {
      nodo->improntaNome = lora::leggi16(dati + 4);
      nodo->improntaNota = true;
    }
    nodo->sentitoDa = millis();
    pubblicaPresenza(*nodo, true);
    if (nodo->daRimuovere) {
      inviaAlNodo(*nodo, lora::RIMOZIONE, nullptr, 0);
      Serial.printf("Nodo %s rimosso dalla rete\n", idLeggibile(nodo->id).c_str());
      dimenticaNodo(*nodo);
    } else {
      inviaStato(*nodo);
    }
  } else if (intestazione.tipo == lora::MISURA && lunghezzaDati == lora::DATI_MISURA) {
    // Nessuna risposta: il nodo ne manda una ogni 10 secondi
    lora::Misura misura = lora::leggiMisura(dati);
    registraMisura(*nodo, misura, misura.potenza);
    Serial.printf("Misura del nodo %s (impianto %d): %s, %s, %s, ora del nodo %s%s\n",
                  idLeggibile(nodo->id).c_str(), nodo->impianto, conVirgola(misura.potenza / 1000.0f, 2, " kW").c_str(),
                  testoTemperatura(nodo->misura.temperatura).c_str(),
                  testoLuce(nodo->misura.luce).c_str(), oraLocale(nodo->misura.ora).c_str(),
                  (nodo->misura.segnali & lora::SEGNALE_LAVAGGIO) ? ", lavaggio in corso" : "");
  } else if (intestazione.tipo == lora::USCITA) {
    ultimoEvento = idLeggibile(nodo->id) + " uscito";
    Serial.printf("Nodo %s uscito dalla rete (reset)\n", idLeggibile(nodo->id).c_str());
    pubblicaPresenza(*nodo, false);
    pubblicaEvento("node_left", *nodo, nullptr);
    dimenticaNodo(*nodo);
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
  float rssi = radio.getRSSI();
  float snr = radio.getSNR();
  radio.startReceive();
  lora::Intestazione intestazione;
  if (!letto || !lora::leggiIntestazione(pacchetto, lunghezza, intestazione)) {
    return;   // Pacchetto rovinato, o di un'altra rete LoRa della zona
  }
  if (intestazione.tipo == lora::ADESIONE && intestazione.rete == 0) {
    riceviAdesione(pacchetto, lunghezza, intestazione, rssi, snr);
  } else if (idCommunity != 0 && intestazione.rete == idCommunity) {
    riceviDalNodo(pacchetto, lunghezza, intestazione);
  }
}

// Finestra che si chiude, nodi che non si sentono più, nodi rimossi mai raggiunti
void controllaNodi() {
  if (finestraAperta && scaduto(finestraFino)) {
    finestraAperta = false;
    Serial.println("Aggiunta di nodi chiusa");
  }
  for (int i = 0; i < NODI_MASSIMI; i++) {
    Nodo &nodo = nodi[i];
    if (!nodo.usato) {
      continue;
    }
    if (nodo.sentito && millis() - nodo.sentitoDa > ASSENZA_NODO) {
      pubblicaPresenza(nodo, false);
    }
    if (nodo.daRimuovere && millis() - nodo.rimozioneDa > DURATA_RIMOZIONE) {
      dimenticaNodo(nodo);
    }
  }
}

// --- BROKER MQTT: MESSAGGI DAL SERVER ---

// Elenco dei nodi della community (retained): chiavi e impianti, anche dopo un riavvio o con un master nuovo
void riceviElenco(JsonDocument &messaggio) {
  if (messaggio["community"].is<const char *>() && nomeCommunity != messaggio["community"].as<String>()) {
    nomeCommunity = messaggio["community"].as<String>();
    salvaCredenziali();
  }
  for (int i = 0; i < NODI_MASSIMI; i++) {
    nodi[i].nellElenco = false;
  }
  for (JsonObject voce : messaggio["nodes"].as<JsonArray>()) {
    uint8_t chiave[lora::CHIAVE];
    const char *testo = voce["id"].as<const char *>();
    if (testo == nullptr || strlen(testo) != 8 || !lora::daEsadecimale(voce["key"].as<const char *>(), chiave, lora::CHIAVE)) {
      continue;
    }
    uint32_t id = strtoul(testo, nullptr, 16);
    Nodo *nodo = trovaNodo(id);
    if (nodo != nullptr && memcmp(nodo->chiave, chiave, lora::CHIAVE) != 0) {
      dimenticaNodo(*nodo);   // Ha una chiave nuova: è rientrato nella rete
      nodo = nullptr;
    }
    if (nodo == nullptr) {
      nodo = nuovoNodo(id, chiave);
      if (nodo == nullptr) {
        continue;
      }
      caricaContatori(*nodo);
    }
    int impianto = voce["system_id"] | 0;
    strncpy(nodo->nomeImpianto, voce["system"] | "", sizeof(nodo->nomeImpianto) - 1);
    if (impianto != nodo->impianto) {
      disiscriviImpianto(nodo->impianto);
      nodo->impianto = impianto;
      nodo->sentito = false;
      nodo->potenzaMassima = 0;
      nodo->statoLavato[0] = '\0';
      strcpy(nodo->stato, lora::NON_ASSEGNATO);
      if (impianto != 0) {
        iscriviImpianto(impianto);   // Lo stato è retained: arriva subito e il master lo inoltra al nodo
      } else {
        inviaStato(*nodo);
      }
      Serial.printf("Nodo %s: impianto %d\n", idLeggibile(id).c_str(), impianto);
    }
    nodo->confermato = true;
    nodo->nellElenco = true;
  }
  for (int i = 0; i < NODI_MASSIMI; i++) {
    Nodo &nodo = nodi[i];
    // Restano i nodi da avvisare della rimozione e quelli appena entrati che il server non ha ancora salvato
    if (!nodo.usato || nodo.nellElenco || nodo.daRimuovere ||
        (!nodo.confermato && millis() - nodo.entratoDa < ATTESA_CONFERMA)) {
      continue;
    }
    Serial.printf("Nodo %s: non è più nella rete\n", idLeggibile(nodo.id).c_str());
    dimenticaNodo(nodo);
  }
}

void riceviComando(JsonDocument &messaggio) {
  String comando = messaggio["command"] | "";
  if (comando == "permit_join") {
    finestraAperta = true;
    finestraFino = millis() + (messaggio["seconds"] | 180) * 1000UL;
    Serial.println("Aggiunta di nodi aperta: accendi i nodi nuovi");
    aggiungiNodiVirtuali();
    return;
  }
  const char *testo = messaggio["node"].as<const char *>();
  Nodo *nodo = testo != nullptr ? trovaNodo(strtoul(testo, nullptr, 16)) : nullptr;
  if (nodo == nullptr) {
    return;
  }
  if (nodo->virtuale) {
    if (comando == "remove") {
      // Nessuna radio da avvisare: il nodo virtuale esce subito (rientra al prossimo "Aggiungi nodi")
      pubblicaPresenza(*nodo, false);
      ultimoEvento = idLeggibile(nodo->id) + " rimosso";
      Serial.printf("Nodo virtuale %s rimosso\n", idLeggibile(nodo->id).c_str());
      dimenticaNodo(*nodo);
    }
    return;   // "Identifica": un nodo virtuale non ha un LED
  }
  if (comando == "identify") {
    uint8_t secondi = messaggio["seconds"] | 10;
    Serial.printf("Nodo %s: identifica\n", idLeggibile(nodo->id).c_str());
    inviaAlNodo(*nodo, lora::IDENTIFICA, &secondi, 1);
  } else if (comando == "remove") {
    // Se il nodo non è in ascolto, glielo si ripete in risposta al suo prossimo battito
    nodo->daRimuovere = true;
    nodo->rimozioneDa = millis();
    pubblicaPresenza(*nodo, false);
    inviaAlNodo(*nodo, lora::RIMOZIONE, nullptr, 0);
    ultimoEvento = idLeggibile(nodo->id) + " rimosso";
  }
}

void riceviStato(int impianto, JsonDocument &messaggio) {
  const char *stato = messaggio["status"].as<const char *>();
  if (stato == nullptr) {
    return;
  }
  for (int i = 0; i < NODI_MASSIMI; i++) {
    Nodo &nodo = nodi[i];
    if (nodo.usato && nodo.impianto == impianto && !nodo.daRimuovere) {
      strncpy(nodo.stato, stato, lora::STATO);
      nodo.stato[lora::STATO] = '\0';
      Serial.printf("Impianto %d: stato %s, inoltrato al nodo %s\n", impianto, nodo.stato, idLeggibile(nodo.id).c_str());
      inviaStato(nodo);
    }
  }
}

// Configurazione di un impianto: nome e potenza massima (per scalare la produzione dei nodi virtuali)
void riceviConfigurazione(int impianto, JsonDocument &messaggio) {
  for (int i = 0; i < NODI_MASSIMI; i++) {
    Nodo &nodo = nodi[i];
    if (nodo.usato && nodo.impianto == impianto) {
      nodo.potenzaMassima = messaggio["max_power_kw"] | 0.0f;
      if (messaggio["name"].is<const char *>()) {
        strncpy(nodo.nomeImpianto, messaggio["name"].as<const char *>(), sizeof(nodo.nomeImpianto) - 1);
      }
    }
  }
}

void messaggioRicevuto(char *topic, byte *payload, unsigned int lunghezza) {
  JsonDocument messaggio;
  if (deserializeJson(messaggio, payload, lunghezza)) {
    return;   // Vuoto (retained cancellato) o non valido
  }
  String nome(topic);
  if (nome == argomento("config")) {
    riceviElenco(messaggio);
  } else if (nome == argomento("commands")) {
    riceviComando(messaggio);
  } else if (nome.startsWith("solarfamily/systems/") && nome.endsWith("/status")) {
    riceviStato(nome.substring(strlen("solarfamily/systems/")).toInt(), messaggio);
  } else if (nome.startsWith("solarfamily/systems/") && nome.endsWith("/config")) {
    riceviConfigurazione(nome.substring(strlen("solarfamily/systems/")).toInt(), messaggio);
  }
}

// Client id uguale allo username, "offline" come Last Will e "online" appena collegato
void collegaBroker() {
  if (mqtt.loop()) {
    return;
  }
  if (!scaduto(prossimoTentativo)) {
    return;
  }
  mqtt.setServer(hostBroker.c_str(), portaBroker);
  String connection = argomento("connection");
  if (!mqtt.connect(username.c_str(), username.c_str(), token.c_str(), connection.c_str(), 1, true, "offline")) {
    statoBroker = mqtt.state();
    prossimoTentativo = millis() + ATTESA_RIPROVA;
    Serial.printf("Broker %s:%u: collegamento non riuscito (stato %d)\n", hostBroker.c_str(), portaBroker, statoBroker);
    return;
  }
  statoBroker = mqtt.state();
  mqtt.publish(connection.c_str(), "online", true);
  mqtt.subscribe(argomento("config").c_str(), 1);
  mqtt.subscribe(argomento("commands").c_str(), 1);
  for (int i = 0; i < NODI_MASSIMI; i++) {
    if (nodi[i].usato && nodi[i].impianto != 0) {
      iscriviImpianto(nodi[i].impianto);
      if (nodi[i].sentito) {
        pubblicaPresenza(nodi[i], true);   // Il server li aveva segnati scollegati con il master
      }
    }
  }
  Serial.printf("Collegato al broker come %s\n", username.c_str());
}

bool credenzialiRifiutate() {
  return statoBroker == MQTT_CONNECT_BAD_CREDENTIALS || statoBroker == MQTT_CONNECT_UNAUTHORIZED;
}

// --- PULSANTE ---

// Conta quando si rilascia: il display dice cosa succederà finché lo si tiene premuto
void controllaPulsante() {
  bool premuto = digitalRead(pinPulsante) == LOW;
  if (premuto && !pulsantePremuto) {
    pulsantePremuto = true;
    pulsantePremutoDa = millis();
  } else if (!premuto && pulsantePremuto) {
    pulsantePremuto = false;
    unsigned long durata = millis() - pulsantePremutoDa;
    if (durata >= PRESSIONE_WIFI) {
      dimenticaRete();
    } else if (durata >= PRESSIONE_INSTALLAZIONE) {
      dimenticaCredenziali();
    } else if (durata >= 30 && durata < PRESSIONE_PAGINA && !username.isEmpty()) {   // sotto i 30 ms è un rimbalzo
      pagina++;   // mostra() la riporta a 0 dopo l'ultimo impianto
    }
  }
}

unsigned long durataPressione() {
  return pulsantePremuto ? millis() - pulsantePremutoDa : 0;
}

// --- DISPLAY ---

void mostraPulsante(unsigned long durata) {
  bool wifi = durata >= PRESSIONE_WIFI;
  schermo.setFont(u8g2_font_6x10_tf);
  schermo.drawStr(0, 0, "Rilascia ora per:");
  schermo.drawStr(0, 14, wifi ? "configurare il Wi-Fi" : "una nuova");
  schermo.drawStr(0, 26, wifi ? "e reinstallare" : "installazione");
  schermo.setFont(u8g2_font_5x7_tf);
  if (!wifi) {
    schermo.drawStr(0, 40, "Tieni fino a 10 s: Wi-Fi");
  }
  // Barra da 3 a 10 secondi
  int pieno = min(128UL, (durata - PRESSIONE_INSTALLAZIONE) * 128UL / (PRESSIONE_WIFI - PRESSIONE_INSTALLAZIONE));
  schermo.drawFrame(0, 54, 128, 8);
  schermo.drawBox(0, 54, pieno, 8);
}

void mostraQr() {
  int scala = max(1, LATO_QR / (latoQr + 2));   // almeno un modulo di margine chiaro per lato
  int origine = (LATO_QR - latoQr * scala) / 2;
  // Sfondo chiaro (pixel accesi) e moduli scuri (spenti), come un QR stampato
  schermo.setDrawColor(1);
  schermo.drawBox(0, 0, LATO_QR, LATO_QR);
  schermo.setDrawColor(0);
  for (int y = 0; y < latoQr; y++) {
    for (int x = 0; x < latoQr; x++) {
      if (moduli[y][x]) {
        schermo.drawBox(origine + x * scala, origine + y * scala, scala, scala);
      }
    }
  }
  schermo.setDrawColor(1);
}

void mostraPortale() {
  mostraQr();
  const int x = LATO_QR + 3;
  schermo.setFont(u8g2_font_5x7_tf);
  if (prova == PROVA_IN_CORSO) {
    schermo.drawStr(x, 0, "Provo la");
    schermo.drawStr(x, 9, "rete...");
  } else if (prova == PROVA_RIUSCITA) {
    schermo.drawStr(x, 0, "Wi-Fi ok!");
    schermo.drawStr(x, 9, "Ora la");
    schermo.drawStr(x, 18, "community");
  } else if (WiFi.softAPgetStationNum() > 0) {
    schermo.drawStr(x, 0, "Telefono");
    schermo.drawStr(x, 9, "collegato:");
    schermo.drawStr(x, 18, "apri");
    schermo.drawStr(x, 27, WiFi.softAPIP().toString().c_str());
  } else {
    schermo.drawStr(x, 0, "Wi-Fi:");
    schermo.drawStr(x, 9, "inquadra il");
    schermo.drawStr(x, 18, "QR con il");
    schermo.drawStr(x, 27, "telefono");
  }
  schermo.drawStr(x, 38, portaleNome.c_str());
  schermo.drawStr(x, 47, portalePassword.c_str());
  if (prova == PROVA_FALLITA) {
    schermo.setFont(u8g2_font_4x6_tf);
    schermo.drawStr(x, 57, erroreBreve.c_str());
  }
}

void mostraWifi() {
  schermo.setFont(u8g2_font_6x10_tf);
  schermo.drawStr(0, 0, "SolarFamily Master");
  schermo.setFont(u8g2_font_5x7_tf);
  schermo.drawStr(0, 16, "Collegamento al Wi-Fi");
  schermo.drawUTF8(0, 26, reteNome.c_str());
  if (wifiAssente && millis() - wifiAssenteDa > 20000) {
    long resto = max(0L, (long)(ATTESA_WIFI - (millis() - wifiAssenteDa)) / 1000);
    char riga[32];
    snprintf(riga, sizeof(riga), "Configurazione tra %ld:%02ld", resto / 60, resto % 60);
    schermo.drawStr(0, 42, riga);
    schermo.drawStr(0, 52, "PRG 10 s: subito");
  }
}

void mostraAbbinamento() {
  if (codice.isEmpty()) {
    schermo.setFont(u8g2_font_6x10_tf);
    schermo.drawStr(0, 0, "SolarFamily Master");
    schermo.setFont(u8g2_font_5x7_tf);
    if (errore.isEmpty()) {
      schermo.drawStr(0, 16, "Richiesta del codice...");
    } else {
      schermo.drawStr(0, 16, errore.c_str());
      schermo.drawStr(0, 26, serverUrl.c_str());
      schermo.drawStr(0, 40, "Riprovo tra poco");
      schermo.drawStr(0, 50, "PRG 10 s: cambia server");
    }
    return;
  }
  mostraQr();
  const int x = LATO_QR + 3;
  schermo.setFont(u8g2_font_5x7_tf);
  if (qrLetto) {
    schermo.drawStr(x, 0, "QR letto:");
    schermo.drawStr(x, 9, "scegli la");
    schermo.drawStr(x, 18, "community");
  } else {
    schermo.drawStr(x, 0, "Installa il");
    schermo.drawStr(x, 9, "master: QR");
    schermo.drawStr(x, 18, "col telefono");
  }
  schermo.setFont(u8g2_font_6x10_tf);
  schermo.drawStr(x, 30, codiceLeggibile().c_str());
  schermo.setFont(u8g2_font_5x7_tf);
  long resto = max(0L, (long)(scadenzaCodice - millis()) / 1000);
  char scadenza[16];
  snprintf(scadenza, sizeof(scadenza), "scade %ld:%02ld", resto / 60, resto % 60);
  schermo.drawStr(x, 45, scadenza);
  if (!errore.isEmpty()) {
    schermo.drawStr(x, 55, "rete: errore");
  }
}

// Toglie caratteri dalla fine (interi, anche quelli accentati di più byte) finché il testo non sta nei pixel dati
String adatta(String testo, int larghezza) {
  while (testo.length() > 0 && schermo.getUTF8Width(testo.c_str()) > larghezza) {
    int fine = testo.length() - 1;
    while (fine > 0 && (testo[fine] & 0xC0) == 0x80) {
      fine--;
    }
    testo.remove(fine);
  }
  return testo;
}

// Indici in nodi[] degli impianti da mostrare, uno per pagina, in ordine di id dell'impianto
int impiantiMostrati(int elenco[NODI_MASSIMI]) {
  int numero = 0;
  for (int i = 0; i < NODI_MASSIMI; i++) {
    if (nodi[i].usato && nodi[i].impianto != 0 && !nodi[i].daRimuovere) {
      int j = numero++;
      while (j > 0 && nodi[elenco[j - 1]].impianto > nodi[i].impianto) {
        elenco[j] = elenco[j - 1];
        j--;
      }
      elenco[j] = i;
    }
  }
  return numero;
}

// Quanti impianti per stato, per esempio "OK 2  DRT 1  BRK 1"; false se non ce n'è nessuno
bool riepilogoStati(char *riga, size_t spazio) {
  static const char *const CODICI[] = {"OK", "DRT", "FLT", "BRK"};
  int conteggi[4] = {0, 0, 0, 0};
  int totale = 0;
  for (int i = 0; i < NODI_MASSIMI; i++) {
    if (nodi[i].usato && nodi[i].impianto != 0 && !nodi[i].daRimuovere) {
      for (int c = 0; c < 4; c++) {
        conteggi[c] += strcmp(nodi[i].stato, CODICI[c]) == 0;
      }
      totale++;
    }
  }
  riga[0] = '\0';
  for (int c = 0; c < 4; c++) {
    if (conteggi[c] > 0) {
      size_t usato = strlen(riga);
      snprintf(riga + usato, spazio - usato, "%s%s %d", usato > 0 ? "  " : "", CODICI[c], conteggi[c]);
    }
  }
  return totale > 0 && riga[0] != '\0';
}

const char *descrizioneStato(const char *stato) {
  if (strcmp(stato, "OK") == 0) return "OK";
  if (strcmp(stato, "DRT") == 0) return "PANNELLI SPORCHI";
  if (strcmp(stato, "FLT") == 0) return "PROBABILE GUASTO";
  if (strcmp(stato, "BRK") == 0) return "GUASTO";
  return "STATO NON ANCORA NOTO";
}

// Un impianto: nome, stato (in negativo se non è OK), ultima misura, nodo e lavaggi
void mostraImpianto(Nodo &nodo, int numero, int totale) {
  char testo[40];
  schermo.setFont(u8g2_font_5x7_tf);
  snprintf(testo, sizeof(testo), "%d/%d", numero, totale);
  int larghezzaNumero = schermo.getStrWidth(testo);
  schermo.drawStr(128 - larghezzaNumero, 0, testo);
  String nome = nodo.nomeImpianto[0] != '\0' ? String(nodo.nomeImpianto) : "Impianto " + String(nodo.impianto);
  schermo.drawUTF8(0, 0, adatta(nome, 128 - larghezzaNumero - 4).c_str());
  schermo.drawHLine(0, 8, 128);

  // Lo stato: un riquadro vuoto se OK, pieno (testo in negativo) se c'è da intervenire
  const char *stato = descrizioneStato(nodo.stato);
  bool problema = strcmp(nodo.stato, "OK") != 0 && strcmp(nodo.stato, lora::NON_ASSEGNATO) != 0;
  schermo.setFont(u8g2_font_6x10_tf);
  int x = (128 - schermo.getStrWidth(stato)) / 2;
  if (problema) {
    schermo.drawBox(0, 11, 128, 14);
    schermo.setDrawColor(0);
    schermo.drawStr(x, 13, stato);
    schermo.setDrawColor(1);
  } else {
    schermo.drawFrame(0, 11, 128, 14);
    schermo.drawStr(x, 13, stato);
  }

  // Ultima misura: produzione e temperatura, poi luce e ora
  bool recente = nodo.misurato && millis() - nodo.misuraDa < MISURA_RECENTE;
  String valore = recente ? conVirgola(nodo.potenza / 1000, 2, " kW") : String("-- kW");
  schermo.drawStr(0, 28, valore.c_str());
  valore = recente ? testoTemperatura(nodo.misura.temperatura) : String("-- °C");
  schermo.drawUTF8(128 - schermo.getUTF8Width(valore.c_str()), 28, valore.c_str());
  schermo.setFont(u8g2_font_5x7_tf);
  if (recente) {
    valore = testoLuce(nodo.misura.luce);
    schermo.drawStr(0, 40, valore.c_str());
    if (nodo.misura.ora != 0) {
      valore = "alle " + oraLocale(nodo.misura.ora);
      schermo.drawStr(128 - schermo.getStrWidth(valore.c_str()), 40, valore.c_str());
    }
  } else {
    schermo.drawStr(0, 40, nodo.misurato ? "Nessuna misura da 1 min" : "Nessuna misura");
  }

  if (nodo.virtuale) {
    snprintf(testo, sizeof(testo), "Nodo virtuale: %s",
             nodo.scenario == SPORCO && nodo.pulito ? "lavato" : NOMI_SCENARI[nodo.scenario]);
  } else {
    snprintf(testo, sizeof(testo), "Nodo %s %s", idLeggibile(nodo.id).c_str(), nodo.sentito ? "collegato" : "muto");
  }
  schermo.drawStr(0, 48, testo);
  if (nodo.lavaggio) {
    if ((millis() / 500) % 2) {
      schermo.drawStr(0, 56, "LAVAGGIO IN CORSO");
    }
  } else if (nodo.ultimoLavaggio != 0) {
    snprintf(testo, sizeof(testo), "Lavato alle %s", oraLocale(nodo.ultimoLavaggio).substring(0, 5).c_str());
    schermo.drawStr(0, 56, testo);
  } else {
    schermo.drawStr(0, 56, "PRG: impianto seguente");
  }
}

// In alto la community e il broker; al centro la produzione di tutta la community, gli impianti in linea e i loro
// stati; in basso lo stato della rete
void mostraMaster() {
  schermo.setFont(u8g2_font_5x7_tf);
  const char *broker = mqtt.connected() ? "MQTT ok" : "MQTT --";
  int larghezzaBroker = schermo.getStrWidth(broker);
  schermo.drawStr(128 - larghezzaBroker, 0, broker);
  schermo.drawUTF8(0, 0, adatta(nomeCommunity, 128 - larghezzaBroker - 4).c_str());
  schermo.drawHLine(0, 8, 128);

  // Produzione della community: la somma dell'ultima misura di ogni impianto che si sente
  float totale = 0;
  int impianti = 0, inLinea = 0;
  uint32_t ultimaOra = 0;
  for (int i = 0; i < NODI_MASSIMI; i++) {
    Nodo &nodo = nodi[i];
    if (!nodo.usato || nodo.impianto == 0 || nodo.daRimuovere) {
      continue;
    }
    impianti++;
    if (nodo.misurato && millis() - nodo.misuraDa < MISURA_RECENTE) {
      inLinea++;
      totale += nodo.potenza;
      ultimaOra = max(ultimaOra, nodo.misura.ora);
    }
  }
  schermo.setFont(u8g2_font_helvB12_tf);
  schermo.drawStr(0, 11, conVirgola(totale / 1000, 2, " kW").c_str());
  schermo.setFont(u8g2_font_5x7_tf);
  schermo.drawStr(128 - schermo.getStrWidth("produzione"), 11, "produzione");
  schermo.drawStr(128 - schermo.getStrWidth("community"), 19, "community");

  char riga[40];
  snprintf(riga, sizeof(riga), "Impianti: %d, in linea: %d", impianti, inLinea);
  schermo.drawStr(0, 29, riga);
  if (!riepilogoStati(riga, sizeof(riga))) {
    strcpy(riga, impianti == 0 ? "Nessun impianto con un nodo" : "Stati non ancora arrivati");
  }
  schermo.drawStr(0, 38, riga);

  if (statoRadio != RADIOLIB_ERR_NONE) {
    snprintf(riga, sizeof(riga), "Radio LoRa: errore %d", statoRadio);
  } else if (credenzialiRifiutate()) {
    // Master disinstallato, o installato un altro master sulla community: serve un nuovo abbinamento
    strcpy(riga, "Credenziali rifiutate");
  } else if (!mqtt.connected()) {
    strcpy(riga, statoBroker == MQTT_DISCONNECTED ? "Broker: collegamento..." : "Broker non raggiungibile");
  } else if (finestraAperta) {
    long resto = max(0L, (long)(finestraFino - millis()) / 1000);
    snprintf(riga, sizeof(riga), "Aggiunta nodi: %ld:%02ld", resto / 60, resto % 60);
  } else if (nodiDaAssegnare() > 0) {
    snprintf(riga, sizeof(riga), "Nodi da assegnare: %d", nodiDaAssegnare());
  } else if (ultimaOra != 0) {
    snprintf(riga, sizeof(riga), "Ultima misura: %s", oraLocale(ultimaOra).c_str());
  } else {
    snprintf(riga, sizeof(riga), "Nodi in rete: %d", nodiInRete());
  }
  schermo.drawStr(0, 48, riga);
  schermo.drawStr(0, 56, ultimoEvento.isEmpty() ? "PRG: impianti, 3 s: QR" : ("Nodo " + ultimoEvento).c_str());
}

void mostra() {
  static unsigned long ultimo = 0;
  if (millis() - ultimo < INTERVALLO_DISPLAY) {
    return;
  }
  ultimo = millis();
  schermo.clearBuffer();
  if (durataPressione() >= PRESSIONE_INSTALLAZIONE) {
    mostraPulsante(durataPressione());
  } else if (portaleAperto) {
    mostraPortale();
  } else if (WiFi.status() != WL_CONNECTED) {
    mostraWifi();
  } else if (username.isEmpty()) {
    mostraAbbinamento();
  } else {
    int elenco[NODI_MASSIMI];
    int numero = impiantiMostrati(elenco);
    if (pagina > numero) {
      pagina = 0;
    }
    if (pagina == 0) {
      mostraMaster();
    } else {
      mostraImpianto(nodi[elenco[pagina - 1]], pagina, numero);
    }
  }
  schermo.sendBuffer();
}

// ===================================================================================================================

void setup() {
  Serial.begin(115200);
  pinMode(pinPulsante, INPUT_PULLUP);
  // Il display è alimentato da Vext: va acceso prima di inizializzarlo
  pinMode(pinVext, OUTPUT);
  digitalWrite(pinVext, LOW);
  delay(50);
  schermo.begin();
  schermo.setFontPosTop();

  caricaRete();
  caricaCredenziali();
  if (username.isEmpty()) {
    Serial.println("Nessuna installazione: abbinamento con il QR");
  } else {
    Serial.printf("Master di %s (%s)\n", nomeCommunity.c_str(), username.c_str());
  }
  Serial.printf("Id di questa scheda: %s\n", idLeggibile(lora::idScheda()).c_str());
  avviaRadio();

  mqtt.setBufferSize(4096);   // L'elenco dei nodi, con le chiavi, supera i 256 byte di default
  mqtt.setSocketTimeout(5);
  mqtt.setCallback(messaggioRicevuto);

  portale.on("/", HTTP_GET, paginaPrincipale);
  portale.on("/salva", HTTP_POST, salvaModulo);
  portale.onNotFound(reindirizza);

  // La rete la salva lo sketch, solo dopo una prova riuscita: non la libreria Wi-Fi a ogni tentativo
  WiFi.persistent(false);
  WiFi.onEvent([](WiFiEvent_t evento, WiFiEventInfo_t info) {
    motivoDisconnessione = info.wifi_sta_disconnected.reason;
    disconnessioni = disconnessioni + 1;
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);

  if (reteNome.isEmpty()) {
    apriPortale();
  } else {
    collegaWifi();
  }
}

void loop() {
  controllaPulsante();
  gestisciRadio();
  avviaNtp();
  if (portaleAperto) {
    gestisciPortale();
  } else if (WiFi.status() != WL_CONNECTED) {
    controllaWifi();
  } else {
    wifiAssente = false;
    if (username.isEmpty()) {
      abbina();
    } else {
      collegaBroker();
    }
  }
  if (!username.isEmpty()) {
    controllaNodi();
    simulaNodiVirtuali();
    controllaMinuti();
    svuotaCoda();
  }
  mostra();
}
