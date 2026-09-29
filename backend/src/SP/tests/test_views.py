from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from SP.models import Customer
from SP.tests.helpers import (
    DEFAULT_WEATHER, add_readings, create_city, create_community, create_customer, create_system, utc,
)

WEATHER = mock.patch("SP.views.get_day_weather", return_value=DEFAULT_WEATHER)


class CommunityDashboardTests(TestCase):
    def setUp(self):
        self.community = create_community()
        system = create_system(self.community)
        add_readings(system, utc(2026, 9, 27, 12, 0), [2.0] * 61)
        add_readings(system, utc(2026, 9, 28, 12, 0), [6.0] * 61)  # 6 kW per un'ora: 6 kWh
        self.client.force_login(create_customer(self.community))

    def test_without_day_redirects_to_most_recent_day(self):
        response = self.client.get("/sp/community/")

        self.assertRedirects(response, "/sp/community/?day=2026-09-28", fetch_redirect_response=False)

    def test_invalid_day_redirects_instead_of_loading_all_data(self):
        response = self.client.get("/sp/community/?day=xyz")

        self.assertRedirects(response, "/sp/community/?day=2026-09-28", fetch_redirect_response=False)

    @WEATHER
    def test_shows_energy_chart_weather_and_map(self, get_day_weather):
        response = self.client.get("/sp/community/?day=2026-09-28")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_energy"], 6.0)
        self.assertEqual(len(response.context["chart_data"]), 30)  # 60 minuti, un punto ogni 2
        self.assertContains(response, '<script id="chart-data" type="application/json">')
        self.assertContains(response, "maps.google.com")
        get_day_weather.assert_called_once_with(self.community.city, "2026-09-28")

    @mock.patch("SP.weather.requests.get")
    def test_city_without_coordinates_has_no_weather_and_no_map(self, get):
        self.community.city = create_city("Senza coordinate", latitude=None, longitude=None)
        self.community.save()

        response = self.client.get("/sp/community/?day=2026-09-28")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["weather"])
        self.assertNotContains(response, "maps.google.com")
        get.assert_not_called()

    def test_user_without_customer_gets_403(self):
        self.client.force_login(User.objects.create_user("not_a_customer"))

        response = self.client.get("/sp/community/")

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Utente non associato a un cliente.", status_code=403)

    def test_anonymous_user_is_sent_to_login(self):
        self.client.logout()

        response = self.client.get("/sp/community/")

        self.assertRedirects(response, "/sp/login/?next=/sp/community/", fetch_redirect_response=False)


class SystemDashboardTests(TestCase):
    def setUp(self):
        self.community = create_community()
        self.system = create_system(self.community)
        add_readings(self.system, utc(2026, 9, 28, 12, 0), [3.0] * 61)
        other_system = create_system(create_community("Other"), "Other system")
        add_readings(other_system, utc(2026, 9, 28, 12, 0), [3.0] * 61)
        self.other_url = f"/sp/system/{other_system.id}/?day=2026-09-28"
        self.client.force_login(create_customer(self.community))

    @WEATHER
    def test_customer_sees_its_system_with_all_charts(self, _):
        response = self.client.get(f"/sp/system/{self.system.id}/?day=2026-09-28")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_energy"], 3.0)
        for canvas in ("productionChart", "lightnessChart", "temperatureChart"):
            self.assertContains(response, f'id="{canvas}"')

    def test_system_of_another_community_is_forbidden(self):
        response = self.client.get(self.other_url)

        self.assertContains(response, "Non sei autorizzato", status_code=403)

    def test_missing_system_is_404(self):
        self.assertEqual(self.client.get("/sp/system/999999/").status_code, 404)

    @WEATHER
    def test_staff_sees_every_system(self, _):
        self.client.force_login(User.objects.create_user("staff", is_staff=True))

        self.assertEqual(self.client.get(self.other_url).status_code, 200)


class SystemListAndRegistrationTests(TestCase):
    def test_system_list_shows_only_own_systems(self):
        community = create_community()
        create_system(community, "Mio impianto")
        create_system(create_community("Other"), "Impianto altrui")
        self.client.force_login(create_customer(community))

        response = self.client.get("/sp/system/")

        self.assertContains(response, "Mio impianto")
        self.assertNotContains(response, "Impianto altrui")

    def test_registration_creates_customer_and_logs_in(self):
        community = create_community()

        response = self.client.post("/sp/register/", {
            "username": "nuovo", "password1": "Una-password-sicura-42", "password2": "Una-password-sicura-42",
            "name": "Anna", "surname": "Bianchi", "community": community.id,
        })

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(Customer.objects.get(user__username="nuovo").community, community)
