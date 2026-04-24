from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from .forms import CustomerRegistrationForm
from .models import PanelData, Customer
from .serializers import PanelDataSerializer
from rest_framework import generics
import os
import json
import pandas as pd
from datetime import timedelta, datetime

from django.conf import settings
from datetime import timedelta
from django.shortcuts import render
from django.views import View
import json

def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            if user is not None:
                login(request, user)
                return redirect('home')
    else:
        form = AuthenticationForm()
    return render(request, 'SP/login.html', {'form': form})
from rest_framework import generics

def register_view(request):
    if request.method == 'POST':
        form = CustomerRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('home')
    else:
        form = CustomerRegistrationForm()
    return render(request, 'SP/register.html', {'form': form})

@login_required
def home_view(request):
    return render(request, 'SP/home.html')
from SP.models import PanelData
from SP.serializers import PanelDataSerializer


# --- ENDPOINT API REST ---

class PanelDataList(generics.ListCreateAPIView):
    queryset = PanelData.objects.all()
    serializer_class = PanelDataSerializer


class PanelDataDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = PanelData.objects.all()
    serializer_class = PanelDataSerializer


# --- ELABORAZIONE DATI STORICI ---

def getHistory():
    file_path = os.path.join(settings.BASE_DIR, 'staticfiles', 'report-2025-12-01.xlsx')

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Il file non esiste in: {file_path}")

    try:
        df = pd.read_csv(file_path, sep=None, engine='python', header=0)
    except Exception:
        try:
            df = pd.read_excel(file_path)
        except Exception as e_excel:
            raise Exception(f"Impossibile leggere il file. Errore: {e_excel}")

    # Pulizia nomi colonne e conversione tipi
    df.columns = df.columns.str.strip()

    required_cols = ['Timestamp', 'Daily Production (Active)']
    if not set(required_cols).issubset(df.columns):
        raise Exception(f"Colonne mancanti. Trovate: {df.columns.tolist()}")

    df['Timestamp'] = pd.to_datetime(df['Timestamp'], errors='coerce')
    df['Daily Production (Active)'] = pd.to_numeric(df['Daily Production (Active)'], errors='coerce')

    # Rimuove righe invalide e ordina per tempo
    df = df.dropna(subset=['Timestamp', 'Daily Production (Active)'])
    df = df.sort_values('Timestamp')

    new_timestamps = []
    new_productions = []
    records = df.to_dict('records')

    # Interpolazione dei dati minuto per minuto
    for i in range(1, len(records)):
        curr_row = records[i]
        prev_row = records[i - 1]

        curr_time = curr_row['Timestamp']
        prev_time = prev_row['Timestamp']
        curr_val = curr_row['Daily Production (Active)']
        prev_val = prev_row['Daily Production (Active)']

        delta_prod = curr_val - prev_val

        # Gestione reset del contatore (es. nuovo giorno)
        if delta_prod < 0:
            delta_prod = curr_val

        time_diff_minutes = int(round((curr_time - prev_time).total_seconds() / 60))

        if time_diff_minutes <= 0:
            continue

        value_per_minute = delta_prod / time_diff_minutes

        for m in range(1, time_diff_minutes + 1):
            minute_timestamp = prev_time + timedelta(minutes=m)
            new_timestamps.append(minute_timestamp.strftime('%Y-%m-%d %H:%M'))
            new_productions.append(round(value_per_minute, 4))

    return pd.DataFrame({
        'Timestamp': new_timestamps,
        'ProductionPerMinute': new_productions
    })


def getDataFromDB(community, selected_day=None):
    # Filtra per community
    panel_data = PanelData.objects.filter(system__community=community)

    # Filtra i dati per il giorno selezionato, se fornito
    if selected_day:
        try:
            # Converte la stringa del giorno in un oggetto data
            day = datetime.strptime(selected_day, '%Y-%m-%d').date()
            # Filtra i dati per l'inizio e la fine della giornata
            panel_data = panel_data.filter(time_stamp__date=day).order_by('time_stamp')
        except ValueError:
            # Gestisce il caso in cui il formato della data non sia valido
            panel_data = panel_data.order_by('time_stamp')
    else:
        panel_data = panel_data.order_by('time_stamp')

    # Se non ci sono dati, restituisce un DataFrame vuoto
    if not panel_data.exists():
        return pd.DataFrame({'Timestamp': [], 'ProductionPerMinute': []})

    # Converte i dati in un formato simile a quello di `getHistory`
    records = [
        {'Timestamp': pd.to_datetime(p.time_stamp), 'Power': p.power}
        for p in panel_data
    ]

    new_timestamps = []
    new_productions = []

    # Interpolazione dei dati minuto per minuto
    for i in range(1, len(records)):
        curr_row = records[i]
        prev_row = records[i - 1]

        curr_time = curr_row['Timestamp']
        prev_time = prev_row['Timestamp']
        curr_val = curr_row['Power']
        prev_val = prev_row['Power']

        delta_prod = curr_val - prev_val

        # Gestione reset del contatore (es. nuovo giorno)
        if delta_prod < 0:
            delta_prod = curr_val

        time_diff_minutes = int(round((curr_time - prev_time).total_seconds() / 60))

        if time_diff_minutes <= 0:
            continue

        value_per_minute = delta_prod / time_diff_minutes

        for m in range(1, time_diff_minutes + 1):
            minute_timestamp = prev_time + timedelta(minutes=m)
            new_timestamps.append(minute_timestamp.strftime('%Y-%m-%d %H:%M'))
            new_productions.append(round(value_per_minute, 4))

    return pd.DataFrame({
        'Timestamp': new_timestamps,
        'ProductionPerMinute': new_productions
    })


# --- VISTA GRAFICO WEB ---

class SolarCommunityView(LoginRequiredMixin, View):
    def get(self, request):
        try:
            step = 2
            # Ottiene il customer associato all'utente loggato
            customer = Customer.objects.get(user=request.user)
            community = customer.community

            # Ottiene tutti i giorni unici per il menu a tendina
            available_days = PanelData.objects.filter(system__community=community).dates('time_stamp', 'day', order='DESC')
            available_days = [d.strftime('%Y-%m-%d') for d in available_days]

            # Ottiene il giorno selezionato dalla richiesta GET
            selected_day = request.GET.get('day')

            # Se nessun giorno è selezionato e ci sono giorni disponibili,
            # reindirizza al giorno più recente.
            if not selected_day and available_days:
                return redirect(f"/sp/community/?day={available_days[0]}")

            # Ottiene i dati dal database, filtrando per giorno se specificato
            df = getDataFromDB(community, selected_day)

            # Calcolo dell'energia totale accumulata
            total_energy = 0
            if not df.empty:
                # La produzione è in kW, l'intervallo è di 1 minuto (1/60 di ora)
                # Energia (kWh) = Potenza (kW) * Tempo (h)
                total_energy = df['ProductionPerMinute'].sum() / 60

            if pd.api.types.is_datetime64_any_dtype(df['Timestamp']):
                labels = df['Timestamp'].dt.strftime('%H:%M').tolist()
            else:
                # Converte le stringhe in datetime per l'elaborazione
                if not df.empty:
                    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
                    labels = df['Timestamp'].dt.strftime('%H:%M').tolist()
                else:
                    labels = []

            # Sottocampionamento per la visualizzazione nel grafico
            labels = labels[::step]

            # Arrotondamento dei dati
            data_values = [round(x, 3) for x in df['ProductionPerMinute'].tolist()]
            data_values = data_values[::step]

            # Calcolo dinamico della larghezza del grafico per il CSS/JS
            total_width = 1000

            context = {
                'chart_labels': json.dumps(labels),
                'chart_data': json.dumps(data_values),
                'total_records': len(labels),
                'chart_width': total_width,
                'available_days': available_days,  # Passa i giorni al template
                'selected_day': selected_day,  # Passa il giorno selezionato
                'total_energy': round(total_energy, 2),  # Passa l'energia totale
                'community': community.name,
            }
            return render(request, 'solar_community.html', context)

        except Customer.DoesNotExist:
            return render(request, 'solar_community.html', {'error': 'Utente non associato a un cliente.'})
        except Exception as e:
            return render(request, 'solar_community.html', {'error': str(e)})