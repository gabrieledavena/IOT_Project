#include <math.h> // Necessario per le funzioni pow() e log()

// --- PIN DI INPUT ---
int pinSensoreTemp = A4;
const int pinSensoreLuce = A5;

// --- PARAMETRI COMUNI ---
const float VCC = 5.0; // Tensione di alimentazione (5V)

// --- PARAMETRI SENSORE TEMPERATURA (A4) ---
const float R_FISSA_TEMP = 100.0;
const float TERMISTORE_B = 3455.0; // Coefficiente Beta
const float TERMISTORE_R25 = 10000.0; // Resistenza a 25°C (Ohm)
const float TEMP_RIFERIMENTO_K = 25.0 + 273.15; // 25°C in Kelvin
const float TEMP_RIFERIMENTO_C = 25.0; // 25°C in Celsius
const float MAX_PANEL_POWER = 6000.0; // Potenza massima generata dai pannelli
const float K = -0.004; // Fattore correttivo di temperatura

// --- PARAMETRI SENSORE LUCE (A5) ---
const float R_FISSA_LUCE = 100.0;
const float LDR_GAMMA = 0.8;
const float LDR_R1 = 127410.0; // Resistenza LDR a 1 lux (Ohm)


void setup() {
  Serial.begin(9600);
  Serial.println("Avvio lettura sensori (Luce A5, Temp A4)...");
}

void loop() {
  // === 1. LETTURA TEMPERATURA (A4) ===
  int valoreTemp = analogRead(pinSensoreTemp);
  float VoutTemp = (float)valoreTemp * (VCC / 1023.0);
  float R_Termistore = R_FISSA_TEMP * (VCC / VoutTemp - 1.0);
  float steinhart = log(R_Termistore / TERMISTORE_R25);
  steinhart /= TERMISTORE_B;
  steinhart += 1.0 / TEMP_RIFERIMENTO_K;
  float tempK = 1.0 / steinhart;
  float tempC = tempK - 273.15;

  // === 2. LETTURA LUMINOSITA' (A5) ===
  int valoreLuce = analogRead(pinSensoreLuce);
  float VoutLuce = (float)valoreLuce * (VCC / 1023.0);
  float R_LDR = R_FISSA_LUCE * (VCC / VoutLuce - 1.0);
  float lux = pow(LDR_R1 / R_LDR, 1.0 / LDR_GAMMA);
  float lightPercent = valoreLuce / 1023.0;
  float panelPower = (lightPercent * MAX_PANEL_POWER) * (1 + (tempC - TEMP_RIFERIMENTO_C) * K);

  // === 3. STAMPA SU SERIALE ===
  // Questa stringa verrà inviata al bridge e pubblicata su MQTT
  Serial.print(tempC);
  Serial.print("|");
  Serial.print(lux);
  Serial.print("|");
  Serial.print(panelPower);
  Serial.print("\n");
  delay(1000); // Attendi 1 secondo prima della prossima lettura
}