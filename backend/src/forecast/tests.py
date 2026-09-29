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
from forecast.predictor import FEATURE_COLUMNS, ForecastError, build_features, estimate_past_year_yield, solar_declination
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
