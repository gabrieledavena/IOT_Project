# Usa un'immagine base ufficiale di Python leggera
FROM python:3.10-slim

# Evita che Python generi file .pyc
ENV PYTHONDONTWRITEBYTECODE 1
# Assicura che l'output di Python sia inviato direttamente al terminale (utile per il debug)
ENV PYTHONUNBUFFERED 1

# Imposta la cartella di lavoro nel container
WORKDIR /app

# Installa le dipendenze di sistema necessarie (opzionale, ma spesso utile per driver DB)
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copia i requisiti e installali
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del codice sorgente
COPY ./src .

# Espone la porta (solo documentale, il mapping vero lo fa docker-compose)
EXPOSE 8000

# Comando di avvio (può essere sovrascritto da docker-compose)
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
