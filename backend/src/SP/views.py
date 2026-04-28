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
from datetime import timedelta, datetime, date
import requests
import reverse_geocoder as rg

from django.conf import settings
from django.views import View

METEO_PARAMS = (
            "temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,shortwave_radiation_sum,"
            "wind_speed_10m_max,cloud_cover_mean,daylight_duration,snowfall_sum"
        )

def get_weather_data(latitude, longitude, start_date, end_date, is_forecast=False):
    """Scarica dati meteo estesi da Open-Meteo"""
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "daily": METEO_PARAMS,
        "timezone": "auto"
    }

    response = requests.get(url, params=params)
    if response.status_code != 200:
        raise Exception(f"Errore API Meteo: {response.text}")

    data = response.json()['daily']

    df = pd.DataFrame({
        'Date': pd.to_datetime(data['time']).dt.date if hasattr(pd.to_datetime(data['time']), 'dt') else pd.to_datetime(
            data['time']).date,
        'solar_radiation': data['shortwave_radiation_sum'],
        'temp_max': data['temperature_2m_max'],
        'temp_min': data['temperature_2m_min'],
        'precipitation': data['precipitation_sum'],
        'wind_speed': data['wind_speed_10m_max'],
        'cloud_cover': data['cloud_cover_mean'],
        'daylight_duration': data['daylight_duration'],
        'snowfall': data['snowfall_sum']
    })
    return df.fillna(0)


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

    # Se non ci sono dati, restituisce una lista vuota
    if not panel_data.exists():
        return []

    # Converte i dati
    records = [
        {'Timestamp': pd.to_datetime(p.time_stamp), 'Power': p.power}
        for p in panel_data
    ]

    raw_data = []

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
            if pd.notnull(minute_timestamp):
                raw_data.append({
                    'timestamp': minute_timestamp.strftime('%Y-%m-%dT%H:%M:%S'),
                    'value': round(value_per_minute, 4)
                })

    return raw_data


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
            raw_data = getDataFromDB(community, selected_day)

            # Calcolo dell'energia totale accumulata
            total_energy = 0
            if raw_data:
                # La produzione è in kW, l'intervallo è di 1 minuto (1/60 di ora)
                # Energia (kWh) = Potenza (kW) * Tempo (h)
                total_energy = sum(item['value'] for item in raw_data) / 60

            # Sottocampionamento per la visualizzazione nel grafico
            raw_data_sampled = raw_data[::step]
            
            labels = [item['timestamp'][11:16] for item in raw_data_sampled] # Extract HH:MM
            data_values = [item['value'] for item in raw_data_sampled]

            # Calcolo dinamico della larghezza del grafico per il CSS/JS
            total_width = 1000

            # Meteo
            weather_data = None
            try:
                df = get_weather_data(community.latitude, community.longitude, selected_day, selected_day, is_forecast=False)
                if not df.empty:
                    weather_data = df.iloc[0].to_dict()
                    # Rimuovo oggetti non serializzabili se presenti, Date per renderlo facile da usare.
                    if 'Date' in weather_data:
                        weather_data['Date'] = str(weather_data['Date'])
            except Exception as e:
                print(f"Errore recupero meteo: {e}")

            citta = rg.search([(community.latitude, community.longitude)])[0]['admin2'] or 'boh'
            
            # Formattiamo i giorni per il frontend:
            # Creiamo una lista di dizionari con 'value' (data YYYY-MM-DD) e 'label' (data YYYY-MM-DD o 'Oggi')
            today_str = date.today().strftime('%Y-%m-%d')
            formatted_days = []
            for d in available_days:
                label = 'Oggi' if d == today_str else d
                formatted_days.append({'value': d, 'label': label})

            context = {
                'chart_labels': json.dumps(labels),
                'chart_data': json.dumps(data_values),
                'chart_data_json': json.dumps(raw_data_sampled),
                'total_records': len(labels),
                'chart_width': total_width,
                'available_days': formatted_days,  # Passiamo la lista formattata
                'selected_day': selected_day,  # Passa il giorno selezionato
                'total_energy': round(total_energy, 2),  # Passa l'energia totale
                'community': community,
                'weather': weather_data, # Dati meteo passati al template
                'citta':citta,
                'oggi_str': today_str
            }
            return render(request, 'solar_community.html', context)

        except Customer.DoesNotExist:
            return render(request, 'solar_community.html', {'error': 'Utente non associato a un cliente.'})
        except Exception as e:
            return render(request, 'solar_community.html', {'error': str(e)})