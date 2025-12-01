  // === INIZIO BLOCCO AGGIUNTO (da sensor.ino) ===
  // Ascolta per comandi in arrivo dalla seriale (e dal bridge)
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    // Aggiungi qui i tuoi comandi.
    // Esempio: un comando PING per testare la connessione
    if (command == "PING") {
      Serial.println("PONG: Comando ricevuto da Arduino!");
    } 
    // Potresti aggiungere comandi come "STOP" o "START" per
    // fermare o avviare la stampa dei dati.
  }
  // === FINE BLOCCO AGGIUNTO ===
