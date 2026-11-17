#!/bin/sh
# Avvia il progetto come docker compose up -d, passando al server l'indirizzo del PC nella rete locale.
#
# Il server gira in docker e non vede la rete del PC: senza questo script non sa a quale indirizzo lo raggiungono il
# master della community e il telefono. Con lo script la pagina Simulazione del sito lo mostra (da scrivere nella
# configurazione del Wi-Fi del master e da aprire dal telefono) e controlla che risponda ancora. Se il PC cambia
# indirizzo (un'altra rete, o un nuovo IP dal router) basta rilanciarlo: ricrea solo il servizio del server.
#
# Gli argomenti vanno a docker compose up, per esempio ./start.sh --build. Su macOS e Linux.
set -e
cd "$(dirname "$0")"

# L'indirizzo IPv4 dell'interfaccia della rete predefinita (Wi-Fi o cavo)
case "$(uname)" in
  Darwin)
    interface=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')
    address=$(ipconfig getifaddr "${interface:-en0}" 2>/dev/null || true)
    ;;
  *)
    address=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
    ;;
esac

if [ -n "$address" ]; then
  echo "Indirizzo del PC nella rete locale: http://$address:8000"
else
  echo "Indirizzo del PC nella rete locale non trovato (nessuna rete?): la pagina Simulazione non lo mostrerà"
fi
SERVER_LAN_HOST="$address" docker compose up -d "$@"
