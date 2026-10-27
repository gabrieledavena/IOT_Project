import os
import tempfile
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

import joblib
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from forecast import predictor
from forecast.predictor import (
    FEATURE_COLUMNS, ForecastError, build_features, day_of_year, estimate_past_year_yield, solar_declination, typical_yield,
)
from SP.access import NOT_A_CUSTOMER
from SP.tests.helpers import (
    FakeModel, create_city, create_community, create_customer, create_system, fake_weather, reference_dataset,
)
from SP.weather import WeatherUnavailable


class FeatureTests(TestCase):
    def test_solar_declination_follows_the_seasons(self):
        summer, equinox, winter = solar_declination([date(2026, 6, 21), date(2026, 3, 21), date(2026, 12, 21)])

        self.assertAlmostEqual(summer, 23.44, places=1)
        self.assertAlmostEqual(equinox, 0, delta=0.5)
        self.assertAlmostEqual(winter, -23.44, places=1)

    def test_features_are_the_weather_plus_the_sun_position(self):
        features = build_features(fake_weather(date(2026, 6, 21)), latitude=44.6)

        self.assertEqual(list(features.columns), FEATURE_COLUMNS)
        self.assertEqual(features["latitude"].iloc[0], 44.6)


class RadiationModel:
    """Modello finto con resa proporzionale all'irraggiamento (kWh per kW installato = MJ/m² / 4)."""

    def predict(self, features):
        return features["solar_radiation"].to_numpy() / 4


def weather_by_source(city, start, end=None, historical=False):
    """Previsioni con 20 MJ/m² (resa 5) e meteo storico con 8 MJ/m² (resa 2), per distinguere le due fonti."""
    return fake_weather(start, end, solar_radiation=8.0 if historical else 20.0)


class ForecastViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.community = create_community()
        create_system(self.community, "A", max_power=3.0)
        create_system(self.community, "B", max_power=5.0)
        self.client.force_login(create_customer(self.community))

    def get(self, model=None, weather=weather_by_source):
        with mock.patch("forecast.predictor.load_model", return_value=model or RadiationModel()), \
                mock.patch("forecast.predictor.get_daily_weather", side_effect=weather) as get_weather:
            self.response = self.client.get("/forecast/")
        return self.response, get_weather

    def test_today_tomorrow_next_7_and_30_days(self):
        response, _ = self.get()

        # 8 kW installati: 5 x 8 = 40 kWh nei giorni delle previsioni, 2 x 8 = 16 kWh in quelli dello storico
        self.assertEqual(response.context["today"]["production"], 40.0)
        self.assertEqual(response.context["today"]["Date"], date.today())
        self.assertEqual(response.context["tomorrow"]["Date"], date.today() + timedelta(days=1))
        self.assertEqual(response.context["week_total"], 7 * 40.0)
        self.assertEqual(response.context["month_total"], 16 * 40.0 + 14 * 16.0)
        self.assertEqual(response.context["history_total"], 14 * 16.0)
        self.assertEqual(response.context["history_start"], date.today() + timedelta(days=16))
        self.assertEqual(len(response.context["week"]), 7)
        self.assertContains(response, "Prossimi 30 giorni")

    def test_chart_marks_the_days_estimated_from_the_history(self):
        response, _ = self.get()

        chart = response.context["chart_data"]
        self.assertEqual(len(chart), 30)
        self.assertEqual(chart[15], {"label": mock.ANY, "production": 40.0, "low": None, "high": None, "history": False})
        self.assertEqual(chart[16], {"label": mock.ANY, "production": 16.0, "low": 16.0, "high": 16.0, "history": True})
        self.assertContains(response, 'id="forecast-data"')

    def test_one_request_for_the_forecast_and_one_for_the_history_then_cache(self):
        self.get()
        _, get_weather = self.get()  # la seconda volta il meteo arriva dalla cache
        get_weather.assert_not_called()

        cache.clear()
        _, get_weather = self.get()
        today = date.today()
        forecast_call, history_call = get_weather.call_args_list
        self.assertEqual(forecast_call.args[1:], (today, today + timedelta(days=15)))
        self.assertTrue(history_call.kwargs["historical"])
        self.assertEqual(history_call.args[2], today - timedelta(days=1))

    def test_prediction_is_not_capped_for_large_communities(self):
        create_system(self.community, "Grande", max_power=50.0)

        self.assertEqual(self.get()[0].context["today"]["production"], 290.0)

    def test_weather_errors_are_shown(self):
        def unavailable(city, start, end=None, historical=False):
            raise WeatherUnavailable("Impossibile recuperare i dati meteo da Open-Meteo.")

        self.assertContains(self.get(weather=unavailable)[0], "Impossibile recuperare i dati meteo")

    def test_missing_history(self):
        def no_history(city, start, end=None, historical=False):
            return fake_weather(start, end).iloc[:0] if historical else fake_weather(start, end)

        self.assertContains(self.get(weather=no_history)[0], "Meteo storico non disponibile")

    def test_city_without_coordinates(self):
        self.community.city = create_city("Senza coordinate", latitude=None, longitude=None)
        self.community.save()

        with mock.patch("forecast.predictor.load_model", return_value=FakeModel()):
            response = self.client.get("/forecast/")

        self.assertContains(response, "non ha coordinate")

    def test_missing_model(self):
        with mock.patch("forecast.predictor.load_model", return_value=None):
            response = self.client.get("/forecast/")

        self.assertContains(response, "Il modello di previsione non è disponibile")

    def test_model_trained_with_other_features_is_reported(self):
        incompatible = mock.Mock()
        incompatible.predict.side_effect = ValueError("The feature names should match")

        self.assertContains(self.get(model=incompatible)[0], "non è compatibile")

    def test_community_without_systems(self):
        self.community.photovoltaic_systems.all().delete()

        self.assertContains(self.get()[0], "Nessun impianto fotovoltaico registrato")

    def test_user_without_customer(self):
        self.client.force_login(User.objects.create_user("not_a_customer"))

        self.assertContains(self.client.get("/forecast/"), NOT_A_CUSTOMER, status_code=403)


class TypicalYieldTests(TestCase):
    def test_average_of_the_days_around_the_same_day_of_the_past_years(self):
        def weather(city, start, end=None, historical=False):
            past = fake_weather(start, end, solar_radiation=4.0)
            # Il 31 dicembre è a 2 giorni dal 2 gennaio, a cavallo di capodanno
            past.loc[past["Date"] == date(2024, 12, 31), "solar_radiation"] = 40.0
            return past

        with mock.patch("forecast.predictor.get_daily_weather", side_effect=weather):
            typical = typical_yield(RadiationModel(), create_city(), [date(2027, 1, 2)], today=date(2026, 12, 20))

        # Dal 30 dicembre al 5 gennaio negli ultimi 5 anni: 35 giorni, uno con resa 10 e gli altri con resa 1
        row = typical.iloc[0]
        self.assertAlmostEqual(row["specific_yield"], (34 * 1 + 10) / 35)
        self.assertEqual((row["yield_low"], row["yield_high"]), (1.0, 10.0))

    def test_february_29_is_february_28(self):
        self.assertEqual(day_of_year(date(2028, 2, 29)), day_of_year(date(2027, 2, 28)))
        self.assertEqual(day_of_year(date(2028, 12, 31)), 365)


class PastYearYieldTests(TestCase):
    def setUp(self):
        cache.clear()
        self.city = create_city()

    def test_uses_the_real_weather_of_the_last_365_days(self):
        with mock.patch("forecast.predictor.load_model", return_value=FakeModel(4.0)), \
                mock.patch("forecast.predictor.get_daily_weather",
                           side_effect=lambda city, start, end, historical=False: fake_weather(start, end)) as weather:
            daily = estimate_past_year_yield(self.city)
            estimate_past_year_yield(self.city)  # la seconda volta il meteo arriva dalla cache

        self.assertEqual(len(daily), 365)
        self.assertEqual(daily["Date"].max(), date.today() - timedelta(days=1))
        self.assertAlmostEqual(daily["specific_yield"].sum(), 4.0 * 365)
        weather.assert_called_once()
        self.assertTrue(weather.call_args.kwargs["historical"])

    def test_weather_errors_become_forecast_errors(self):
        with mock.patch("forecast.predictor.load_model", return_value=FakeModel()), \
                mock.patch("forecast.predictor.get_daily_weather", side_effect=WeatherUnavailable("offline")):
            with self.assertRaisesMessage(ForecastError, "offline"):
                estimate_past_year_yield(self.city)


class ModelLoadingTests(TestCase):
    def test_model_is_reloaded_when_the_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("forecast.predictor.MODEL_PATH", Path(tmp) / "model.joblib"):
            self.assertIsNone(predictor.load_model())

            predictor.save_model(FakeModel(1.0))
            os.utime(predictor.MODEL_PATH, (1, 1))
            self.assertEqual(predictor.load_model().specific_yield, 1.0)

            predictor.save_model(FakeModel(2.0))
            os.utime(predictor.MODEL_PATH, (2, 2))
            self.assertEqual(predictor.load_model().specific_yield, 2.0)


class TrainForecastModelCommandTests(TestCase):
    def train(self, *args, dataset_exists=True):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path, model_path = Path(tmp) / "reference.csv.gz", Path(tmp) / "model.joblib"
            if dataset_exists:
                reference_dataset().to_csv(dataset_path, index=False)
            with mock.patch("forecast.management.commands.train_forecast_model.DATASET_PATH", dataset_path), \
                    mock.patch("forecast.reference_data.DATASET_PATH", dataset_path), \
                    mock.patch("forecast.predictor.MODEL_PATH", model_path), \
                    mock.patch("forecast.management.commands.train_forecast_model.download_reference_dataset",
                               return_value=reference_dataset()) as download:
                out = StringIO()
                call_command("train_forecast_model", *args, stdout=out)
                return out.getvalue(), joblib.load(model_path), download

    def test_validates_on_unseen_locations_and_saves_the_model(self):
        output, model, download = self.train()

        download.assert_not_called()
        self.assertIn("VALIDAZIONE SU LOCALITÀ ESCLUSE DAL TRAINING", output)
        self.assertIn("Errore sulla produzione annua", output)
        self.assertEqual(list(model.feature_names_in_), FEATURE_COLUMNS)
        june = date(2026, 6, 21)
        sunny = model.predict(build_features(fake_weather(june, solar_radiation=27.0), 44.0))[0]
        cloudy = model.predict(build_features(fake_weather(june, solar_radiation=5.0), 44.0))[0]
        self.assertGreater(sunny, cloudy)

    def test_downloads_the_dataset_when_missing_or_asked(self):
        self.assertTrue(self.train(dataset_exists=False)[2].called)
        self.assertTrue(self.train("--refresh-data")[2].called)
