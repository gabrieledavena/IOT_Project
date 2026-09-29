from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.test import TestCase

from forecast.predictor import ForecastError
from SP.models import City
from SP.tests.helpers import create_city, create_community, create_customer, past_year_yield

from .calculator import Assumptions, calculate_roi
from .forms import RoiForm

NO_DEGRADATION = Assumptions(annual_degradation=0)


class CalculatorTests(TestCase):
    def test_self_consumption_is_capped_by_the_consumption(self):
        # 35% di 4000 kWh = 1400 kWh, ma il cliente ne consuma solo 1000
        first = calculate_roi(4000, 1000, 3000, 0.30, NO_DEGRADATION).first_year

        self.assertAlmostEqual(first.self_consumed_kwh, 1000)
        self.assertAlmostEqual(first.exported_kwh, 3000)
        self.assertAlmostEqual(first.savings, 300)  # 1000 kWh x 0,30 €
        self.assertAlmostEqual(first.revenue, 300)  # 3000 kWh x 0,10 €

    def test_payback_and_roi(self):
        result = calculate_roi(4000, 1000, 3000, 0.30, NO_DEGRADATION)  # 600 € all'anno

        self.assertAlmostEqual(result.payback_years, 5.0)
        self.assertAlmostEqual(result.net_gain, 25 * 600 - 3000)
        self.assertAlmostEqual(result.roi, 4.0)
        self.assertAlmostEqual(result.consumption_coverage, 1.0)

    def test_payback_can_fall_within_a_year(self):
        self.assertAlmostEqual(calculate_roi(4000, 1000, 1500, 0.30, NO_DEGRADATION).payback_years, 2.5)

    def test_production_degrades_every_year(self):
        years = calculate_roi(1000, 5000, 3000, 0.30).years

        self.assertEqual(len(years), 25)
        self.assertAlmostEqual(years[1].production_kwh, 1000 * 0.995)
        self.assertLess(years[-1].production_kwh, years[0].production_kwh)

    def test_investment_that_never_pays_back(self):
        result = calculate_roi(1000, 1000, 1_000_000, 0.30)

        self.assertIsNone(result.payback_years)
        self.assertLess(result.roi, 0)


class RoiFormTests(TestCase):
    def setUp(self):
        self.modena = create_city("Modena", province="Modena", region="Emilia-Romagna")

    def form(self, **overrides):
        data = {"city": self.modena.pk, "peak_power_kw": "4,5", "annual_consumption_kwh": "3000",
                "system_cost": "7500", "energy_price": "0,28", **overrides}
        return RoiForm(data)

    def test_accepts_decimal_commas_and_uses_default_assumptions(self):
        form = self.form()

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["peak_power_kw"], 4.5)
        self.assertEqual(form.cleaned_data["city"].name, "Modena")
        self.assertEqual(form.assumptions(), Assumptions())

    def test_custom_assumptions(self):
        form = self.form(self_consumption_percent="50", export_price="0,08", degradation_percent="1", lifetime_years="20")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.assumptions(), Assumptions(0.5, 0.08, 0.01, 20))

    def test_city_is_chosen_from_the_cities_with_coordinates(self):
        without_coordinates = create_city("Senza coordinate", latitude=None, longitude=None, province="Roma")

        choices = [label for _, label in RoiForm().fields["city"].choices]
        self.assertEqual(choices, ["Scegli un comune", "Modena (Modena)"])
        self.assertIn("city", self.form(city="").errors)
        self.assertIn("city", self.form(city=without_coordinates.pk).errors)

    def test_region_and_province_filter_the_cities(self):
        create_city("Bari", province="Bari", region="Puglia")
        form = RoiForm()

        self.assertEqual(form.fields["region"].choices[1:], [("Emilia-Romagna", "Emilia-Romagna"), ("Puglia", "Puglia")])
        self.assertEqual(form.fields["province"].choices[1:], [("Bari", "Bari"), ("Modena", "Modena")])
        self.assertIn(
            {"id": self.modena.pk, "name": "Modena", "province": "Modena", "region": "Emilia-Romagna"}, form.city_options
        )

    def test_city_must_belong_to_the_chosen_region_and_province(self):
        create_city("Bari", province="Bari", region="Puglia")

        self.assertTrue(self.form(region="Emilia-Romagna", province="Modena").is_valid())
        self.assertIn("city", self.form(region="Puglia").errors)
        self.assertIn("city", self.form(province="Bari").errors)


def roi_data(**overrides):
    city = City.objects.get(name="Modena", province="Modena")
    return {"client": "Anna Bianchi", "city": city.pk, "peak_power_kw": "4",
            "annual_consumption_kwh": "3000", "system_cost": "6000", "energy_price": "0,30", **overrides}


class RoiViewTests(TestCase):
    def setUp(self):
        create_city("Modena", province="Modena")
        self.staff = User.objects.create_user("consulente", first_name="Luca", last_name="Verdi", is_staff=True)

    def post(self, url, data=None, yields=None, error=None):
        with mock.patch("roi.views.estimate_past_year_yield", return_value=yields, side_effect=error):
            return self.client.post(url, data or roi_data())

    def test_only_staff_can_use_the_calculator(self):
        self.assertRedirects(self.client.get("/roi/"), "/sp/login/?next=/roi/", fetch_redirect_response=False)

        self.client.force_login(create_customer(create_community()))
        self.assertEqual(self.client.get("/roi/").status_code, 403)
        self.assertEqual(self.client.post("/roi/", roi_data()).status_code, 403)
        self.assertEqual(self.client.post("/roi/quote/", roi_data()).status_code, 403)

    def test_empty_form(self):
        self.client.force_login(self.staff)

        response = self.client.get("/roi/")

        self.assertContains(response, "Calcola ROI")
        for field in ("region", "province", "city"):
            self.assertContains(response, f'<select name="{field}" class="form-select"')
        self.assertContains(response, ">Modena (Modena)</option>")
        self.assertContains(response, '<script id="city-options" type="application/json">')
        self.assertNotIn("result", response.context)

    def test_data_is_never_read_from_the_url(self):
        self.client.force_login(self.staff)

        response = self.client.get(f"/roi/?{urlencode(roi_data())}")

        self.assertFalse(response.context["form"].is_bound)
        self.assertNotIn("result", response.context)
        self.assertContains(response, '<form method="post" action="/roi/"')

    def test_simulation_with_a_year_of_weather(self):
        self.client.force_login(self.staff)

        response = self.post("/roi/", yields=past_year_yield(4.0))

        result = response.context["result"]
        self.assertAlmostEqual(result.first_year.production_kwh, 4.0 * 365 * 4)  # 4 kWh/kWp x 365 giorni x 4 kWp
        self.assertAlmostEqual(response.context["specific_yield"], 4.0 * 365)
        self.assertAlmostEqual(sum(kwh for _, kwh in response.context["monthly_production"]), 4.0 * 365 * 4)
        # Il pulsante del preventivo invia gli stessi dati in POST, in campi nascosti
        self.assertContains(response, '<form method="post" action="/roi/quote/" target="_blank"')
        self.assertContains(response, '<input type="hidden" name="client" value="Anna Bianchi"')

    def test_missing_days_are_scaled_to_a_whole_year(self):
        self.client.force_login(self.staff)

        response = self.post("/roi/", yields=past_year_yield(4.0, days=360))

        self.assertAlmostEqual(response.context["specific_yield"], 4.0 * 365)
        self.assertEqual(response.context["weather_days"], 360)

    def test_forecast_errors_are_shown(self):
        self.client.force_login(self.staff)

        response = self.post("/roi/", error=ForecastError("Impossibile recuperare i dati meteo"))

        self.assertContains(response, "Impossibile recuperare i dati meteo")

    def test_quote(self):
        self.client.force_login(self.staff)

        response = self.post("/roi/quote/", yields=past_year_yield(4.0))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["quote_number"].startswith("SF-"))
        for text in ("Anna Bianchi", "Modena (Modena)", "5.840 kWh", "Luca Verdi", "Ipotesi e metodo"):
            self.assertContains(response, text)
        # "Torna al calcolo" riporta i dati alla pagina del calcolo, sempre in POST
        self.assertContains(response, '<input type="hidden" name="system_cost" value="6000"')

    def test_quote_with_invalid_data_shows_the_calculator_with_the_errors(self):
        self.client.force_login(self.staff)

        response = self.post("/roi/quote/", data=roi_data(peak_power_kw=""))

        self.assertTemplateUsed(response, "roi/roi.html")
        self.assertIn("peak_power_kw", response.context["form"].errors)

    def test_quote_opened_directly_goes_to_the_calculator(self):
        self.client.force_login(self.staff)

        self.assertRedirects(self.client.get("/roi/quote/"), "/roi/", fetch_redirect_response=False)
