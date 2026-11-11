// Protocollo LoRa tra il master di una community e i nodi sui tetti (LoRa punto-punto, senza LoRaWAN).
// Questo file è identico in MasterNode/ e RoofNode/: se lo cambi, copialo nell'altro sketch (un test in
// ArduinoBridge/tests controlla che le due copie siano uguali).
//
// Ogni pacchetto comincia con un'intestazione in chiaro di 11 byte:
//   0      'S' (0x53)
//   1      versione del protocollo
//   2      tipo del pacchetto
//   3-6    rete: id della community del master (0 nella richiesta di adesione), little endian
//   7-10   nodo: id del nodo, dal suo MAC, little endian
//
// Adesione, come per un dispositivo Zigbee: il master accetta nodi nuovi solo durante la finestra "Aggiungi nodi".
//   ADESIONE      nodo -> tutti    intestazione + chiave pubblica del nodo (32) + tipo di nodo (1), in chiaro
//   ACCETTAZIONE  master -> nodo   intestazione + chiave pubblica del master (32) + un pacchetto protetto senza
//                                  dati: verificandolo, il nodo sa che il master ha calcolato la sua stessa chiave
// Master e nodo calcolano lo stesso segreto con X25519 (Diffie-Hellman su curva ellittica) dalle due chiavi
// pubbliche, senza mai trasmetterlo: chi ascolta la radio vede solo le chiavi pubbliche. La chiave AES-128 del
// nodo è SHA-256(etichetta, rete, nodo, chiavi pubbliche, segreto) troncato a 16 byte.
//
// Dopo l'adesione ogni pacchetto è protetto con AES-128-CCM: intestazione [+ dati in chiaro] + contatore (4) +
// dati cifrati + codice di autenticazione (8). Ogni direzione ha il suo contatore, che cresce sempre: chi riceve
// scarta i contatori già visti, così un pacchetto registrato e ritrasmesso non vale.
//   BATTITO     nodo -> master   ogni minuto: secondi dall'accensione (4) e l'impronta del nome dell'impianto che
//                                il nodo conosce (2, 0 se nessuno)
//   RISPOSTA    master -> nodo   dopo ogni battito e quando cambia: stato dell'impianto (3, "---" se il nodo non
//                                è ancora assegnato a un impianto), l'ora se il master la sa da internet (4, secondi
//                                Unix UTC, altrimenti 0: il nodo ci regola il suo orologio DS1307) e, solo se
//                                l'impronta del battito non è quella giusta, il nome dell'impianto (fino a NOME
//                                byte, UTF-8): così il nome viaggia solo quando il nodo non lo ha
//   MISURA      nodo -> master   ogni 10 secondi, senza risposta: l'ora del nodo (4, secondi Unix UTC, 0 se non
//                                la sa), la temperatura (2, decimi di °C con segno, TEMPERATURA_ASSENTE se il
//                                DHT11 non risponde), la luce (4, lux), i segnali (1: SEGNALE_LAVAGGIO) e la
//                                produzione misurata dalla pinza amperometrica (2, W)
//   IDENTIFICA  master -> nodo   secondi di lampeggio veloce del LED (1)
//   RIMOZIONE   master -> nodo   il nodo esce dalla rete e torna come nuovo
//   USCITA      nodo -> master   il nodo è stato riportato come nuovo (PRG tenuto 10 secondi)
//
// Tempo in aria a SF7: una MISURA (36 byte) dura circa 77 ms, un BATTITO (29 byte) circa 67 ms. Una misura ogni
// 10 secondi e un battito al minuto sono circa lo 0,85% del tempo, sotto il limite dell'1% della banda.
#pragma once

#include <Arduino.h>
#include <esp_random.h>
#include <mbedtls/ccm.h>
#include <mbedtls/ecdh.h>
#include <mbedtls/sha256.h>
#include <string.h>

namespace lora {

// --- Radio: 868,1 MHz (banda 868,0-868,6 MHz, al massimo 14 dBm e l'1% del tempo in trasmissione), SF7 ---
const uint32_t FREQUENZA_KHZ = 868100;
const uint8_t SPREADING_FACTOR = 7;
const uint8_t CODING_RATE = 5;
const int8_t POTENZA_DBM = 14;

const uint8_t MAGICO = 0x53;
const uint8_t VERSIONE = 1;

enum Tipo : uint8_t {
  ADESIONE = 1,
  ACCETTAZIONE = 2,
  BATTITO = 3,
  RISPOSTA = 4,
  IDENTIFICA = 5,
  RIMOZIONE = 6,
  USCITA = 7,
  MISURA = 8,
};

enum Direzione : uint8_t { VERSO_MASTER = 0, VERSO_NODO = 1 };

const uint8_t NODO_TETTO = 1;   // tipo di nodo nella richiesta di adesione

const size_t INTESTAZIONE = 11;
const size_t CHIAVE_PUBBLICA = 32;
const size_t CHIAVE = 16;
const size_t CONTATORE = 4;
const size_t CODICE = 8;
const size_t MASSIMO = 64;      // lunghezza massima di un pacchetto
const size_t STATO = 3;         // codice dello stato dell'impianto: OK, DRT, FLT, BRK
const char NON_ASSEGNATO[] = "---";
const size_t ORA = 4;           // secondi Unix UTC, dopo lo stato nella RISPOSTA
const size_t DATI_MISURA = 13;  // ora (4) + temperatura (2) + luce (4) + segnali (1) + produzione (2)
const size_t DATI_BATTITO = 6;  // secondi dall'accensione (4) + impronta del nome (2)
const size_t NOME = 24;         // byte del nome dell'impianto nella RISPOSTA: oltre si accorcia
const int16_t TEMPERATURA_ASSENTE = INT16_MIN;
const uint8_t SEGNALE_LAVAGGIO = 0x01;   // la pompa (l'animazione dell'acqua sul display) è accesa
const uint32_t ORA_MINIMA = 1735689600;  // 1/1/2025: un'ora prima di questa non è stata regolata

struct Intestazione {
  uint8_t tipo;
  uint32_t rete;
  uint32_t nodo;
};

struct CoppiaChiavi {
  uint8_t privata[32];
  uint8_t pubblica[CHIAVE_PUBBLICA];
};

inline void scrivi32(uint8_t *destinazione, uint32_t valore) {
  for (int i = 0; i < 4; i++) {
    destinazione[i] = (valore >> (8 * i)) & 0xFF;
  }
}

inline uint32_t leggi32(const uint8_t *origine) {
  return (uint32_t)origine[0] | ((uint32_t)origine[1] << 8) | ((uint32_t)origine[2] << 16) | ((uint32_t)origine[3] << 24);
}

inline void scrivi16(uint8_t *destinazione, uint16_t valore) {
  destinazione[0] = valore & 0xFF;
  destinazione[1] = valore >> 8;
}

inline uint16_t leggi16(const uint8_t *origine) {
  return (uint16_t)origine[0] | ((uint16_t)origine[1] << 8);
}

// Una misura del nodo sul tetto
struct Misura {
  uint32_t ora;          // secondi Unix UTC, 0 se il nodo non sa l'ora
  int16_t temperatura;   // decimi di °C, TEMPERATURA_ASSENTE se il DHT11 non risponde
  uint32_t luce;         // lux
  uint8_t segnali;
  uint16_t potenza;      // W, dalla pinza amperometrica
};

inline void scriviMisura(uint8_t dati[DATI_MISURA], const Misura &misura) {
  scrivi32(dati, misura.ora);
  scrivi16(dati + 4, (uint16_t)misura.temperatura);
  scrivi32(dati + 6, misura.luce);
  dati[10] = misura.segnali;
  scrivi16(dati + 11, misura.potenza);
}

inline Misura leggiMisura(const uint8_t dati[DATI_MISURA]) {
  Misura misura;
  misura.ora = leggi32(dati);
  misura.temperatura = (int16_t)leggi16(dati + 4);
  misura.luce = leggi32(dati + 6);
  misura.segnali = dati[10];
  misura.potenza = leggi16(dati + 11);
  return misura;
}

// Il nome dell'impianto accorciato a NOME byte, senza spezzare un carattere UTF-8 (accentato, di più byte)
inline void accorciaNome(const char *nome, char breve[NOME + 1]) {
  size_t lunghezza = strnlen(nome, NOME + 1);
  if (lunghezza > NOME) {
    lunghezza = NOME;
    while (lunghezza > 0 && ((uint8_t)nome[lunghezza] & 0xC0) == 0x80) {
      lunghezza--;   // nome[lunghezza] è il seguito di un carattere: lo si toglie tutto
    }
  }
  memcpy(breve, nome, lunghezza);
  breve[lunghezza] = '\0';
}

// Impronta del nome (FNV-1a a 32 bit ripiegata su 16): 0 solo per il nome vuoto
inline uint16_t impronta(const char *nome) {
  if (nome[0] == '\0') {
    return 0;
  }
  uint32_t valore = 2166136261u;
  for (const char *c = nome; *c != '\0'; c++) {
    valore = (valore ^ (uint8_t)*c) * 16777619u;
  }
  uint16_t risultato = (uint16_t)((valore >> 16) ^ (valore & 0xFFFF));
  return risultato == 0 ? 1 : risultato;
}

// Id della scheda: 4 byte del suo MAC, scritto come 8 cifre esadecimali (come lo mostra il sito)
inline uint32_t idScheda() {
  return (uint32_t)(ESP.getEfuseMac() >> 16);
}

inline size_t scriviIntestazione(uint8_t *pacchetto, uint8_t tipo, uint32_t rete, uint32_t nodo) {
  pacchetto[0] = MAGICO;
  pacchetto[1] = VERSIONE;
  pacchetto[2] = tipo;
  scrivi32(pacchetto + 3, rete);
  scrivi32(pacchetto + 7, nodo);
  return INTESTAZIONE;
}

// False per i pacchetti di altri (altre reti LoRa nella zona, un'altra versione del protocollo)
inline bool leggiIntestazione(const uint8_t *pacchetto, size_t lunghezza, Intestazione &intestazione) {
  if (lunghezza < INTESTAZIONE || pacchetto[0] != MAGICO || pacchetto[1] != VERSIONE) {
    return false;
  }
  intestazione.tipo = pacchetto[2];
  intestazione.rete = leggi32(pacchetto + 3);
  intestazione.nodo = leggi32(pacchetto + 7);
  return true;
}

inline int casuale(void *, unsigned char *destinazione, size_t lunghezza) {
  esp_fill_random(destinazione, lunghezza);
  return 0;
}

// Nuova coppia di chiavi X25519. Il generatore casuale dell'ESP32 è davvero casuale solo con il Wi-Fi acceso o
// con la sorgente di rumore del bootloader: il nodo, che non usa il Wi-Fi, la accende prima di chiamarla
inline bool generaCoppia(CoppiaChiavi &coppia) {
  mbedtls_ecp_group gruppo;
  mbedtls_mpi privata;
  mbedtls_ecp_point pubblica;
  mbedtls_ecp_group_init(&gruppo);
  mbedtls_mpi_init(&privata);
  mbedtls_ecp_point_init(&pubblica);
  size_t scritti = 0;
  bool riuscito = mbedtls_ecp_group_load(&gruppo, MBEDTLS_ECP_DP_CURVE25519) == 0 &&
                  mbedtls_ecp_gen_keypair(&gruppo, &privata, &pubblica, casuale, nullptr) == 0 &&
                  mbedtls_mpi_write_binary(&privata, coppia.privata, sizeof(coppia.privata)) == 0 &&
                  mbedtls_ecp_point_write_binary(&gruppo, &pubblica, MBEDTLS_ECP_PF_UNCOMPRESSED, &scritti,
                                                 coppia.pubblica, sizeof(coppia.pubblica)) == 0 &&
                  scritti == CHIAVE_PUBBLICA;
  mbedtls_ecp_point_free(&pubblica);
  mbedtls_mpi_free(&privata);
  mbedtls_ecp_group_free(&gruppo);
  return riuscito;
}

// Chiave AES del nodo, dalla propria chiave privata e dalla chiave pubblica dell'altro: master e nodo
// ottengono la stessa
inline bool calcolaChiave(const uint8_t privata[32], const uint8_t pubblicaAltro[CHIAVE_PUBBLICA], uint32_t rete,
                          uint32_t nodo, const uint8_t pubblicaNodo[CHIAVE_PUBBLICA],
                          const uint8_t pubblicaMaster[CHIAVE_PUBBLICA], uint8_t chiave[CHIAVE]) {
  mbedtls_ecp_group gruppo;
  mbedtls_mpi segretoPrivato;
  mbedtls_mpi condiviso;
  mbedtls_ecp_point altro;
  mbedtls_ecp_group_init(&gruppo);
  mbedtls_mpi_init(&segretoPrivato);
  mbedtls_mpi_init(&condiviso);
  mbedtls_ecp_point_init(&altro);
  uint8_t segreto[32];
  bool riuscito = mbedtls_ecp_group_load(&gruppo, MBEDTLS_ECP_DP_CURVE25519) == 0 &&
                  mbedtls_mpi_read_binary(&segretoPrivato, privata, 32) == 0 &&
                  mbedtls_ecp_point_read_binary(&gruppo, &altro, pubblicaAltro, CHIAVE_PUBBLICA) == 0 &&
                  mbedtls_ecdh_compute_shared(&gruppo, &condiviso, &altro, &segretoPrivato, casuale, nullptr) == 0 &&
                  mbedtls_mpi_write_binary(&condiviso, segreto, sizeof(segreto)) == 0;
  mbedtls_ecp_point_free(&altro);
  mbedtls_mpi_free(&condiviso);
  mbedtls_mpi_free(&segretoPrivato);
  mbedtls_ecp_group_free(&gruppo);
  // Un segreto tutto a zero viene da una chiave pubblica scelta apposta per renderlo prevedibile
  uint8_t zeri = 0;
  for (size_t i = 0; i < sizeof(segreto); i++) {
    zeri |= segreto[i];
  }
  if (!riuscito || zeri == 0) {
    return false;
  }
  static const char ETICHETTA[] = "SolarFamily LoRa 1";
  uint8_t numeri[8];
  scrivi32(numeri, rete);
  scrivi32(numeri + 4, nodo);
  uint8_t impronta[32];
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  riuscito = mbedtls_sha256_starts(&sha, 0) == 0 &&
             mbedtls_sha256_update(&sha, (const uint8_t *)ETICHETTA, strlen(ETICHETTA)) == 0 &&
             mbedtls_sha256_update(&sha, numeri, sizeof(numeri)) == 0 &&
             mbedtls_sha256_update(&sha, pubblicaNodo, CHIAVE_PUBBLICA) == 0 &&
             mbedtls_sha256_update(&sha, pubblicaMaster, CHIAVE_PUBBLICA) == 0 &&
             mbedtls_sha256_update(&sha, segreto, sizeof(segreto)) == 0 &&
             mbedtls_sha256_finish(&sha, impronta) == 0;
  mbedtls_sha256_free(&sha);
  memcpy(chiave, impronta, CHIAVE);
  memset(segreto, 0, sizeof(segreto));
  return riuscito;
}

// Nonce di CCM (13 byte): rete, nodo, direzione e contatore. Non si ripete mai con la stessa chiave
inline void nonce(const uint8_t *pacchetto, uint8_t direzione, uint32_t contatore, uint8_t risultato[13]) {
  memcpy(risultato, pacchetto + 3, 8);
  risultato[8] = direzione;
  scrivi32(risultato + 9, contatore);
}

// Completa un pacchetto protetto. In pacchetto ci sono già i primi `chiaro` byte (intestazione ed eventuali dati
// in chiaro); aggiunge contatore, dati cifrati e codice. Restituisce la lunghezza, 0 se non è riuscito
inline size_t proteggi(uint8_t *pacchetto, size_t chiaro, uint32_t contatore, uint8_t direzione,
                       const uint8_t chiave[CHIAVE], const uint8_t *dati, size_t lunghezzaDati) {
  if (chiaro + CONTATORE + lunghezzaDati + CODICE > MASSIMO) {
    return 0;
  }
  scrivi32(pacchetto + chiaro, contatore);
  size_t autenticato = chiaro + CONTATORE;   // in chiaro ma protetto dal codice
  uint8_t numero[13];
  nonce(pacchetto, direzione, contatore, numero);
  mbedtls_ccm_context ccm;
  mbedtls_ccm_init(&ccm);
  bool riuscito = mbedtls_ccm_setkey(&ccm, MBEDTLS_CIPHER_ID_AES, chiave, 8 * CHIAVE) == 0 &&
                  mbedtls_ccm_encrypt_and_tag(&ccm, lunghezzaDati, numero, sizeof(numero), pacchetto, autenticato,
                                              dati, pacchetto + autenticato, pacchetto + autenticato + lunghezzaDati,
                                              CODICE) == 0;
  mbedtls_ccm_free(&ccm);
  return riuscito ? autenticato + lunghezzaDati + CODICE : 0;
}

// Verifica e decifra un pacchetto protetto: false se il codice non torna (chiave sbagliata o pacchetto
// alterato). In dati (almeno MASSIMO byte) finiscono i dati, in contatore il contatore del pacchetto
inline bool apri(const uint8_t *pacchetto, size_t lunghezza, size_t chiaro, uint8_t direzione,
                 const uint8_t chiave[CHIAVE], uint32_t &contatore, uint8_t *dati, size_t &lunghezzaDati) {
  if (lunghezza < chiaro + CONTATORE + CODICE || lunghezza > MASSIMO) {
    return false;
  }
  contatore = leggi32(pacchetto + chiaro);
  size_t autenticato = chiaro + CONTATORE;
  lunghezzaDati = lunghezza - autenticato - CODICE;
  uint8_t numero[13];
  nonce(pacchetto, direzione, contatore, numero);
  mbedtls_ccm_context ccm;
  mbedtls_ccm_init(&ccm);
  bool riuscito = mbedtls_ccm_setkey(&ccm, MBEDTLS_CIPHER_ID_AES, chiave, 8 * CHIAVE) == 0 &&
                  mbedtls_ccm_auth_decrypt(&ccm, lunghezzaDati, numero, sizeof(numero), pacchetto, autenticato,
                                           pacchetto + autenticato, dati, pacchetto + autenticato + lunghezzaDati,
                                           CODICE) == 0;
  mbedtls_ccm_free(&ccm);
  return riuscito;
}

inline void esadecimale(const uint8_t *byte, size_t lunghezza, char *testo) {
  static const char CIFRE[] = "0123456789abcdef";
  for (size_t i = 0; i < lunghezza; i++) {
    testo[2 * i] = CIFRE[byte[i] >> 4];
    testo[2 * i + 1] = CIFRE[byte[i] & 0x0F];
  }
  testo[2 * lunghezza] = '\0';
}

inline bool daEsadecimale(const char *testo, uint8_t *byte, size_t lunghezza) {
  if (testo == nullptr || strlen(testo) != 2 * lunghezza) {
    return false;
  }
  for (size_t i = 0; i < lunghezza; i++) {
    char coppia[3] = {testo[2 * i], testo[2 * i + 1], '\0'};
    char *fine = nullptr;
    byte[i] = (uint8_t)strtoul(coppia, &fine, 16);
    if (*fine != '\0') {
      return false;
    }
  }
  return true;
}

}  // namespace lora
