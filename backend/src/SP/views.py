from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from .forms import CustomerRegistrationForm
from .models import PanelData
from .serializers import PanelDataSerializer
from rest_framework import generics
import pandas as pd
import os
from django.conf import settings
from datetime import timedelta
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


class PanelDataList(generics.ListCreateAPIView):
    queryset = PanelData.objects.all()
    serializer_class = PanelDataSerializer


class PanelDataDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = PanelData.objects.all()
    serializer_class = PanelDataSerializer


def getHistory():
    # Definisci il percorso
    file_path = os.path.join(settings.BASE_DIR, 'staticfiles', 'report-2025-12-01.xlsx')

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Il file non esiste in: {file_path}")

    try:
        df = pd.read_csv(file_path, sep=None, engine='python', header=0)
    except Exception:
        df = pd.read_excel(file_path)

    df.columns = df.columns.str.strip()
    required_cols = ['Timestamp', 'Daily Production (Active)']
    if not set(required_cols).issubset(df.columns):
        raise Exception(f"Colonne mancanti. Trovate: {df.columns.tolist()}")

    df['Timestamp'] = pd.to_datetime(df['Timestamp'], errors='coerce')
    df['Daily Production (Active)'] = pd.to_numeric(df['Daily Production (Active)'], errors='coerce')

    df = df.dropna(subset=['Timestamp', 'Daily Production (Active)'])
    df = df.sort_values('Timestamp')

    new_timestamps = []
    new_productions = []

    records = df.to_dict('records')

    for i in range(1, len(records)):
        curr_row = records[i]
        prev_row = records[i - 1]

        curr_time = curr_row['Timestamp']
        prev_time = prev_row['Timestamp']

        curr_val = curr_row['Daily Production (Active)']
        prev_val = prev_row['Daily Production (Active)']

        delta_prod = curr_val - prev_val
        if delta_prod < 0:
            delta_prod = curr_val

        time_diff = (curr_time - prev_time).total_seconds() / 60
        minutes_count = int(round(time_diff))

        if minutes_count <= 0:
            continue

        value_per_minute = delta_prod / minutes_count

        for m in range(1, minutes_count + 1):
            minute_timestamp = prev_time + timedelta(minutes=m)
            new_timestamps.append(minute_timestamp.strftime('%Y-%m-%d %H:%M'))
            new_productions.append(round(value_per_minute, 4))

    result_df = pd.DataFrame({
        'Timestamp': new_timestamps,
        'ProductionPerMinute': new_productions
    })
    return result_df


class SolarHistoryView(LoginRequiredMixin,View):
    def get(self, request):
        try:
            df = getHistory()
            if pd.api.types.is_datetime64_any_dtype(df['Timestamp']):
                labels = df['Timestamp'].dt.strftime('%H:%M').tolist()
            else:
                labels = [str(t).split(' ')[-1][:5] for t in df['Timestamp']]
            labels = labels[:1500:22]

            data_values = [round(x, 3) for x in df['ProductionPerMinute'].tolist()]
            data_values = data_values[:1500:22]
            data_values = [d*1000 for d in data_values]

            pixel_per_bar = 20
            total_width = max(len(labels) * pixel_per_bar, 1000)

            context = {
                'chart_labels': json.dumps(labels),
                'chart_data': json.dumps(data_values),
                'total_records': len(labels),
                'chart_width': total_width,
            }
            return render(request, 'solar_history.html', context)

        except Exception as e:
            return render(request, 'solar_history.html', {'error': str(e)})