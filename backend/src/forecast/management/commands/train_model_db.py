from datetime import date, timedelta

import pandas as pd
from django.core.management.base import BaseCommand
from django.utils import timezone
from sklearn.ensemble import RandomForestRegressor

from forecast.predictor import MODEL_PATH, predict_specific_yield, save_model
from SP.models import Community
from SP.production import daily_energy_kwh, get_system_series
from SP.weather import WEATHER_COLUMNS, WeatherUnavailable, get_daily_weather

# Target del modello: kWh prodotti in un giorno per ogni kW installato. Non dipende dalla taglia
# dell'impianto, quindi la previsione vale per impianti e community di qualsiasi potenza
# (un Random Forest non estrapola oltre le potenze viste in training)
TARGET = "specific_yield"


class Command(BaseCommand):
    help = "Trains the production forecast model (daily kWh per installed kW) from PanelData and Open-Meteo weather"

    def handle(self, *args, **options):
        self.stdout.write("--- 1. ADDESTRAMENTO DEL MODELLO DA DATABASE ---")
        dataset = self.build_dataset()
        if dataset.empty:
            self.stdout.write(self.style.ERROR("Nessun dato utile trovato per l'addestramento."))
            return
        if len(dataset) < 2:
            self.stdout.write(self.style.WARNING("Troppi pochi dati per l'addestramento. Necessari almeno 2 record."))
            return
        self.stdout.write(
            f"Dati di addestramento pronti, {len(dataset)} record totali "
            f"(resa media {dataset[TARGET].mean():.2f} kWh per kW installato)."
        )

        self.stdout.write("Addestramento del modello Random Forest...")
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(dataset[WEATHER_COLUMNS], dataset[TARGET])

        self.stdout.write("\n--- IMPORTANZA VARIABILI (Top 5) ---")
        importances = pd.Series(model.feature_importances_, index=WEATHER_COLUMNS)
        self.stdout.write(str(importances.sort_values(ascending=False).head(5)))

        self.print_example_forecast(model, dataset)

        save_model(model)
        self.stdout.write(self.style.SUCCESS(f"Modello salvato con successo in {MODEL_PATH}"))

    def build_dataset(self):
        """Una riga per impianto e giornata completa, con il meteo del giorno e la resa per kW."""
        self.stdout.write("Recupero dati di produzione e caratteristiche degli impianti dal database...")
        # La giornata in corso è incompleta: la sua produzione non è confrontabile
        # con il meteo dell'intera giornata, quindi resta fuori dal training
        today_utc = timezone.now().date()

        frames = []
        for community in Community.objects.select_related("city"):
            # Senza coordinate non si può scaricare il meteo da associare alla produzione
            if not community.city.has_coordinates:
                self.stdout.write(self.style.WARNING(
                    f"La città di {community.name} ({community.city}) non ha coordinate: community saltata"
                ))
                continue

            self.stdout.write(f"Elaborazione dati per Community: {community.name}")
            rows = []
            for system in community.photovoltaic_systems.all():
                if system.max_power <= 0:
                    self.stdout.write(self.style.WARNING(
                        f"{system.name} ha potenza installata {system.max_power} kW: impianto saltato"
                    ))
                    continue
                for day, energy in daily_energy_kwh(get_system_series(system)).items():
                    if day < today_utc:
                        rows.append({
                            "Date": day,
                            "system_id": system.id,
                            "max_power": system.max_power,
                            TARGET: energy / system.max_power,
                        })

            if not rows:
                self.stdout.write(self.style.WARNING(f"Nessun dato di produzione per la Community {community.name}"))
                continue

            production = pd.DataFrame(rows)
            first_day, last_day = production["Date"].min(), production["Date"].max()
            self.stdout.write(f"Recupero dati meteo per {community.name} ({first_day} - {last_day})...")
            try:
                weather = get_daily_weather(community.city, first_day, last_day)
            except WeatherUnavailable as error:
                self.stdout.write(self.style.WARNING(f"{error} Community {community.name} saltata."))
                continue
            frames.append(production.merge(weather, on="Date"))

        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def print_example_forecast(self, model, dataset):
        """Esempio: previsione di domani per un impianto di potenza media, nella prima community con coordinate."""
        self.stdout.write("\n--- 2. ESEMPIO DI PREVISIONE PER DOMANI ---")
        community = next((c for c in Community.objects.select_related("city") if c.city.has_coordinates), None)
        if community is None:
            return

        tomorrow = date.today() + timedelta(days=1)
        self.stdout.write(f"Scarico previsioni per domani ({tomorrow}) in {community.name}...")
        try:
            weather = get_daily_weather(community.city, tomorrow)
        except WeatherUnavailable as error:
            self.stdout.write(self.style.WARNING(f"Esempio non disponibile: {error}"))
            return

        average_power = dataset["max_power"].mean()
        specific_yield = predict_specific_yield(model, weather)[0]
        meteo = weather.iloc[0]
        self.stdout.write("-" * 40)
        self.stdout.write(f"DATA: {tomorrow}")
        self.stdout.write(f"Impianto Esempio (Potenza Max: {average_power:.2f}kW)")
        self.stdout.write(f"Scenario Meteo in {community.name}:")
        self.stdout.write(f"  ☀️ Radiazione: {meteo['solar_radiation']:.2f} MJ/m²")
        self.stdout.write(f"  ☁️ Nuvolosità: {meteo['cloud_cover']:.1f}%")
        self.stdout.write(f"  💨 Vento Max: {meteo['wind_speed']:.1f} km/h")
        self.stdout.write(f"  💧 Pioggia: {meteo['precipitation']:.2f} mm")
        self.stdout.write(f"  🌡️ Temp Min/Max: {meteo['temp_min']:.1f}°C / {meteo['temp_max']:.1f}°C")
        self.stdout.write(f"  ⏳ Durata Luce: {meteo['daylight_duration'] / 3600:.1f} ore")
        if meteo["snowfall"] > 0:
            self.stdout.write(f"  ❄️ Neve: {meteo['snowfall']:.2f} cm")
        self.stdout.write("-" * 40)
        self.stdout.write(
            f"⚡ PRODUZIONE STIMATA: {specific_yield * average_power:.2f} kWh "
            f"({specific_yield:.2f} kWh per kW installato)"
        )
        self.stdout.write("-" * 40)
