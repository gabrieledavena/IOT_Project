from django.shortcuts import render

# Create your views here.
from SP.models import PanelData
from SP.serializers import PanelDataSerializer
from django.http import Http404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from rest_framework import generics
import pandas as pd
import os
from django.conf import settings
import numpy as np

from django.shortcuts import render
from django.views import View
import json  # Serve per serializzare i dati per JavaScript
from datetime import timedelta




class PanelDataList(generics.ListCreateAPIView):
    queryset = PanelData.objects.all()
    serializer_class = PanelDataSerializer


class PanelDataDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = PanelData.objects.all()
    serializer_class = PanelDataSerializer


import pandas as pd
import os
import numpy as np
from django.conf import settings
from datetime import timedelta


def getHistory():
    # Definisci il percorso
    file_path = os.path.join(settings.BASE_DIR, 'staticfiles', 'report-2025-12-01.xlsx')

    print(f"DEBUG: Cerco il file in: {file_path}")

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Il file non esiste in: {file_path}")

    # Tentativo di lettura
    try:
        # Usa header=0 per dire che la prima riga sono i titoli
        df = pd.read_csv(file_path, sep=None, engine='python', header=0)
        print("DEBUG: Letto come CSV")
    except Exception as e_csv:
        try:
            df = pd.read_excel(file_path)
            print("DEBUG: Letto come Excel")
        except Exception as e_excel:
            raise Exception(f"Impossibile leggere il file. CSV error: {e_csv}, Excel error: {e_excel}")

    # 1. PULIZIA NOMI COLONNE (Fondamentale: rimuove spazi extra dai nomi)
    df.columns = df.columns.str.strip()

    # Verifica colonne
    required_cols = ['Timestamp', 'Daily Production (Active)']
    if not set(required_cols).issubset(df.columns):
        raise Exception(f"Colonne mancanti. Trovate: {df.columns.tolist()}")

    # 2. CONVERSIONE TIPI (Gestione errori di formato)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], errors='coerce')
    # Converte in numero, trasformando errori in NaN
    df['Daily Production (Active)'] = pd.to_numeric(df['Daily Production (Active)'], errors='coerce')

    # Rimuove righe dove i dati essenziali sono NaN (vuoti o corrotti)
    df = df.dropna(subset=['Timestamp', 'Daily Production (Active)'])
    df = df.sort_values('Timestamp')

    print(f"DEBUG: Righe valide trovate nel file: {len(df)}")

    new_timestamps = []
    new_productions = []

    records = df.to_dict('records')

    # Loop di generazione
    for i in range(1, len(records)):
        curr_row = records[i]
        prev_row = records[i - 1]

        curr_time = curr_row['Timestamp']
        prev_time = prev_row['Timestamp']

        curr_val = curr_row['Daily Production (Active)']
        prev_val = prev_row['Daily Production (Active)']

        # Calcolo Delta
        delta_prod = curr_val - prev_val

        # Se il contatore si è resettato (es. nuovo giorno), prendiamo il valore attuale come delta
        if delta_prod < 0:
            delta_prod = curr_val

            # Se il delta è 0, la produzione è 0, ma dobbiamo comunque generare le barrette vuote
        # Calcolo minuti
        time_diff = (curr_time - prev_time).total_seconds() / 60
        minutes_count = int(round(time_diff))

        if minutes_count <= 0:
            continue

        value_per_minute = delta_prod / minutes_count

        # Generazione minuti
        for m in range(1, minutes_count + 1):
            minute_timestamp = prev_time + timedelta(minutes=m)
            new_timestamps.append(minute_timestamp.strftime('%Y-%m-%d %H:%M'))
            # Arrotondiamo a 4 cifre decimali per pulizia
            new_productions.append(round(value_per_minute, 4))

    result_df = pd.DataFrame({
        'Timestamp': new_timestamps,
        'ProductionPerMinute': new_productions
    })

    print(f"DEBUG: Dati generati (minuto per minuto): {len(result_df)}")
    return result_df


from django.shortcuts import render
from django.views import View
import json


# Importa la tua funzione getHistory corretta (quella del messaggio precedente)

class SolarHistoryView(View):
    def get(self, request):
        try:
            df = getHistory()

            # DEBUG: Stampa nel terminale per conferma
            print(f"Dati recuperati: {len(df)} righe")
            if not df.empty:
                print(f"Esempio dati: {df.iloc[0].to_dict()}")

            # FORMATTAZIONE LABEL: Usiamo solo HH:MM per pulizia visuale
            # Se il df['Timestamp'] è datetime, usiamo .dt.strftime
            # Se è già stringa, va bene così, ma meglio essere sicuri
            if pd.api.types.is_datetime64_any_dtype(df['Timestamp']):
                labels = df['Timestamp'].dt.strftime('%H:%M').tolist()
            else:
                # Se sono già stringhe, proviamo a tagliare la data se presente
                labels = [str(t).split(' ')[-1][:5] for t in df['Timestamp']]
            labels = labels[:1500:22]

            # ARROTONDAMENTO DATI: 3 decimali bastano, evita numeri tipo 0.333333333
            data_values = [round(x, 3) for x in df['ProductionPerMinute'].tolist()]
            data_values = data_values[:1500:22]
            data_values = [d*1000 for d in data_values]

            # CALCOLO LARGHEZZA:
            # 20px a barra sono ideali per vederle bene separate
            pixel_per_bar = 20
            total_width = max(len(labels) * pixel_per_bar, 1000)  # Minimo 1000px

            context = {
                'chart_labels': json.dumps(labels),
                'chart_data': json.dumps(data_values),
                'total_records': len(labels),
                'chart_width': total_width,
            }
            return render(request, 'solar_history.html', context)

        except Exception as e:
            print(f"ERRORE: {e}")
            return render(request, 'solar_history.html', {'error': str(e)})