from datetime import date
from unittest import mock

import requests
from django.test import TestCase

from SP.tests.helpers import create_city
from SP.weather import WEATHER_COLUMNS, WeatherUnavailable, get_daily_weather, get_day_weather


def open_meteo_response(days):
    response = mock.Mock()
    response.json.return_value = {"daily": {
        "time": days,
        "shortwave_radiation_sum": [18.3] * len(days),
        "temperature_2m_max": [26.1] * len(days),
        "temperature_2m_min": [9.6] * len(days),
        "precipitation_sum": [None] * len(days),  # Open-Meteo può restituire valori nulli
        "wind_speed_10m_max": [12.5] * len(days),
        "cloud_cover_mean": [3.0] * len(days),
        "daylight_duration": [42480.0] * len(days),
        "snowfall_sum": [0.0] * len(days),
    }}
    return response


class WeatherTests(TestCase):
    @mock.patch("SP.weather.requests.get")
    def test_response_is_mapped_to_project_columns(self, get):
        get.return_value = open_meteo_response(["2026-09-28", "2026-09-29"])

        weather = get_daily_weather(create_city(), date(2026, 9, 28), date(2026, 9, 29))

        self.assertEqual(list(weather.columns), ["Date"] + WEATHER_COLUMNS)
        self.assertEqual(list(weather["Date"]), [date(2026, 9, 28), date(2026, 9, 29)])
        self.assertEqual(list(weather["precipitation"]), [0, 0])
        self.assertEqual(get.call_args.kwargs["params"]["start_date"], "2026-09-28")

    @mock.patch("SP.weather.requests.get")
    def test_city_without_coordinates_does_not_call_open_meteo(self, get):
        city = create_city(latitude=None, longitude=None)

        with self.assertRaisesMessage(WeatherUnavailable, "non ha coordinate"):
            get_daily_weather(city, date(2026, 9, 28))
        get.assert_not_called()

    @mock.patch("SP.weather.requests.get", side_effect=requests.ConnectionError("offline"))
    def test_network_error_makes_weather_unavailable(self, get):
        with self.assertLogs("SP.weather", level="WARNING") as logs:
            with self.assertRaises(WeatherUnavailable):
                get_daily_weather(create_city(), date(2026, 9, 28))
            self.assertIsNone(get_day_weather(create_city(name="Bari"), date(2026, 9, 28)))
        self.assertIn("offline", logs.output[0])
