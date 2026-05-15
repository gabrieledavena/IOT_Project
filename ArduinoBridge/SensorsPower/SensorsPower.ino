#include <math.h> 

// --- PIN DI INPUT ---
const int pinVolt = A0;     
const int pinSensoreTemp = A4;
const int pinSensoreLuce = A5;

// --- PARAMETRI COMUNI ---
const float VCC = 5.0; 

// --- PARAMETRI LETTURA ELETTRICA (POTENZA REALE) ---
const float CORREZIONE_POTENZA = 1240;

// --- PARAMETRI SENSORE TEMPERATURA (A4) ---
const float R_FISSA_TEMP = 100.0;
const float TERMISTORE_B = 3455.0; 
const float TERMISTORE_R25 = 10000.0; 
const float TEMP_RIFERIMENTO_K = 25.0 + 273.15; 

// --- PARAMETRI SENSORE LUCE (A5) ---
const float R_FISSA_LUCE = 100.0;
const float LDR_GAMMA = 0.8;
const float LDR_R1 = 127410.0; 

void setup() {
  Serial.begin(9600);
  Serial.println("Avvio sistema: Lettura V, I, P reale + Sensori Amb.");
}

void loop() {
  // === 1. LETTURA TEMPERATURA (A4) - Invariato ===
  int valoreTemp = analogRead(pinSensoreTemp);
  float VoutTemp = (float)valoreTemp * (VCC / 1023.0);
  float R_Termistore = R_FISSA_TEMP * (VCC / VoutTemp - 1.0);
  float steinhart = log(R_Termistore / TERMISTORE_R25);
  steinhart /= TERMISTORE_B;
  steinhart += 1.0 / TEMP_RIFERIMENTO_K;
  float tempK = 1.0 / steinhart;
  float tempC = tempK - 273.15;

  // === 2. LETTURA LUMINOSITA' (A5) - Solo lettura Lux ===
  int valoreLuce = analogRead(pinSensoreLuce);
  float VoutLuce = (float)valoreLuce * (VCC / 1023.0);
  float R_LDR = R_FISSA_LUCE * (VCC / VoutLuce - 1.0);
  float lux = pow(LDR_R1 / R_LDR, 1.0 / LDR_GAMMA);
  
  // === 3. LETTURA POTENZA REALE (A0) === 
  
  // A. Leggi Tensione Totale (Pin A0)
  int rawVoltage = analogRead(pinVolt);
  float panelPower = (rawVoltage * (VCC / 1023.0)) * CORREZIONE_POTENZA;

  // === 4. STAMPA SU SERIALE ===
  // Formato: Temp | Lux | PotenzaReale
  Serial.print(tempC, 1);
  Serial.print("|");
  Serial.print(lux, 0);
  Serial.print("|");
  Serial.print(panelPower, 3); // 3 decimali per vedere anche i milliwatt
  Serial.print("\n");
  
  delay(1000); 
}