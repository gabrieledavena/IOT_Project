import os
import tempfile
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

import joblib
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from forecast import predictor
from SP.tests.helpers import add_readings, create_city, create_community, create_customer, create_system, fake_weather, utc
from SP.weather import WEATHER_COLUMNS, WeatherUnavailable


class FakeModel:
    """Modello finto con resa costante (kWh per kW installato)."""

    def __init__(self, specific_yield=5.0):
        self.specific_yield = specific_yield

    def predict(self, weather):
        if list(weather.columns) != WEATHER_COLUMNS:
            raise ValueError("feature names should match")
        return [self.specific_yield] * len(weather)


class ForecastViewTests(TestCase):
    def setUp(self):
        self.community = create_community()
        create_system(self.community, "A", max_power=3.0)
        create_system(self.community, "B", max_power=5.0)
        self.client.force_login(create_customer(self.community))

    def post(self, url, model=None, weather=None):
        weather = weather or (lambda city, day: fake_weather(day))
        with mock.patch("forecast.predictor.load_model", return_value=model or FakeModel()), \
                mock.patch("forecast.predictor.get_daily_weather", side_effect=weather):
            return self.client.post(url)

    def test_prediction_is_specific_yield_times_installed_power(self):
        response = self.post("/forecast/tomorrow/")

        self.assertEqual(response.context["prediction"], 40.0)  # 5 kWh/kW x 8 kW
        self.assertEqual(response.context["day"], date.today() + timedelta(days=1))
        self.assertContains(response, "per domani")

    def test_today_page_uses_today_date(self):
        response = self.post("/forecast/today/")

        self.assertEqual(response.context["day"], date.today())
        self.assertContains(response, f"Previsione per il {date.today():%d/%m/%Y}")
        self.assertContains(response, "per oggi")

    def test_prediction_is_not_capped_for_large_communities(self):
        create_system(self.community, "Grande", max_power=50.0)

        self.assertEqual(self.post("/forecast/tomorrow/").context["prediction"], 290.0)

    def test_weather_errors_are_shown(self):
        def unavailable(city, day):
            raise WeatherUnavailable("Impossibile recuperare i dati meteo da Open-Meteo.")

        self.assertContains(self.post("/forecast/tomorrow/", weather=unavailable), "Impossibile recuperare i dati meteo")

    def test_city_without_coordinates(self):
        self.community.city = create_city("Senza coordinate", latitude=None, longitude=None)
        self.community.save()

        with mock.patch("forecast.predictor.load_model", return_value=FakeModel()):
            response = self.client.post("/forecast/tomorrow/")

        self.assertContains(response, "non ha coordinate")

    def test_missing_model(self):
        with mock.patch("forecast.predictor.load_model", return_value=None):
            response = self.client.post("/forecast/tomorrow/")

        self.assertContains(response, "Il modello di previsione non è disponibile")

    def test_model_trained_with_other_features_is_reported(self):
        incompatible = mock.Mock()
        incompatible.predict.side_effect = ValueError("The feature names should match")

        self.assertContains(self.post("/forecast/tomorrow/", model=incompatible), "non è compatibile")

    def test_user_without_customer(self):
        self.client.force_login(User.objects.create_user("not_a_customer"))

        self.assertContains(self.client.post("/forecast/today/"), "Utente non associato a nessuna community.")

    def test_get_shows_only_the_form(self):
        response = self.client.get("/forecast/tomorrow/")

        self.assertContains(response, "Calcola Previsione")
        self.assertNotIn("prediction", response.context)


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


class TrainModelCommandTests(TestCase):
    def test_trains_on_complete_days_only_and_saves_the_model(self):
        system = create_system(create_community(), max_power=4.0)
        today = timezone.now().date()
        two_days_ago, yesterday = today - timedelta(days=2), today - timedelta(days=1)
        add_readings(system, utc(two_days_ago.year, two_days_ago.month, two_days_ago.day), [4.0] * 1440)
        add_readings(system, utc(yesterday.year, yesterday.month, yesterday.day), [2.0] * 1440)
        add_readings(system, utc(today.year, today.month, today.day), [4.0] * 3)  # oggi: giornata incompleta

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("forecast.predictor.MODEL_PATH", Path(tmp) / "model.joblib"), \
                mock.patch("forecast.management.commands.train_model_db.get_daily_weather",
                           side_effect=lambda city, start, end=None: fake_weather(start, end)):
            out = StringIO()
            call_command("train_model_db", stdout=out)
            model = joblib.load(Path(tmp) / "model.joblib")

        # Rese per kW: 1439 min x 4 kW / 60 / 4 kW = 23,98 e 1440 min x 2 kW / 60 / 4 kW = 12,00
        self.assertIn("2 record totali (resa media 17.99 kWh per kW installato)", out.getvalue())
        self.assertEqual(list(model.feature_names_in_), WEATHER_COLUMNS)
        prediction = model.predict(fake_weather(today)[WEATHER_COLUMNS])[0]
        self.assertTrue(12.0 <= prediction <= 23.99)

    def test_without_data_no_model_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("forecast.predictor.MODEL_PATH", Path(tmp) / "model.joblib"):
            out = StringIO()
            call_command("train_model_db", stdout=out)

            self.assertIn("Nessun dato utile", out.getvalue())
            self.assertFalse((Path(tmp) / "model.joblib").exists())
