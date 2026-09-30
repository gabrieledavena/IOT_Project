#include <math.h>
#include <DHT.h>

// --- PIN DI INPUT ---
#define DHTPIN 4           // Pin digitale collegato a 'out' del DHT11 (es. D4 o A4)
#define DHTTYPE DHT11      // Modello del sensore
const int pinSensoreLuce = A5;

// Inizializzazione sensore DHT
DHT dht(DHTPIN, DHTTYPE);

// --- PARAMETRI COMUNI ---
const float VCC = 5.0; // Tensione di alimentazione (5V)

// --- PARAMETRI CALCOLO POTENZA PANNELLI ---
const float TEMP_RIFERIMENTO_C = 25.0; // 25°C in Celsius
const float MAX_PANEL_POWER = 6000.0;  // Potenza massima generata dai pannelli
const float K = -0.004;                // Fattore correttivo di temperatura (-0.4%/°C)

// --- PARAMETRI SENSORE LUCE (A5) ---
const float R_FISSA_LUCE = 100.0;
const float LDR_GAMMA = 0.8;
const float LDR_R1 = 127410.0; // Resistenza LDR a 1 lux (Ohm)

void setup() {
  Serial.begin(9600);
  dht.begin();
  // Linea di avvio commentata o rimossa per non sporcare il flusso MQTT sul bridge
  // Serial.println("Avvio lettura sensori (Luce A5, DHT11 D4)...");
}

void loop() {
  // === 1. LETTURA TEMPERATURA (DHT11) ===
  float tempC = dht.readTemperature();

  // Controllo validità lettura
  if (isnan(tempC)) {
    // Se la lettura fallisce, imposta un valore di fallback o salta il ciclo
    tempC = TEMP_RIFERIMENTO_C;
  }

  // === 2. LETTURA LUMINOSITÀ (A5) ===
  int valoreLuce = analogRead(pinSensoreLuce);
  float VoutLuce = (float)valoreLuce * (VCC / 1023.0);
  float R_LDR = R_FISSA_LUCE * (VCC / VoutLuce - 1.0);
  float lux = pow(LDR_R1 / R_LDR, 1.0 / LDR_GAMMA);
  float lightPercent = (float)valoreLuce / 1023.0;

  // Calcolo potenza stimata con derating termico
  float panelPower = (lightPercent * MAX_PANEL_POWER) * (1.0 + (tempC - TEMP_RIFERIMENTO_C) * K);

  // === 3. STAMPA SU SERIALE (Formato per bridge MQTT) ===
  Serial.print(tempC);
  Serial.print("|");
  Serial.print(lux);
  Serial.print("|");
  Serial.println(panelPower);

  // Il DHT11 richiede almeno 1 secondo (1 Hz) tra una lettura e l'altra
  delay(1500);
}