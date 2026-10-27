from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from SP.models import Customer, PhotovoltaicSystem
from SP.tests.helpers import (
    DEFAULT_WEATHER, add_readings, create_city, create_community, create_customer, create_system, utc,
)

Status = PhotovoltaicSystem.Status


def checked(system, status):
    system.status, system.last_check = status, timezone.now() - timedelta(hours=1)
    system.save()
    return system


class UserDashboardTests(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.user = create_customer(self.community)
        checked(create_system(self.community, "Sede"), Status.OK)
        checked(create_system(self.community, "Capannone"), Status.FAULT)
        checked(create_system(self.community, "Magazzino"), Status.DIRTY)
        create_system(self.community, "Uffici")  # mai controllato
        create_system(create_community("Bianchi Srl"), "Impianto altrui")

    def test_login_leads_to_the_home_dashboard(self):
        response = self.client.post("/sp/login/", {"username": "mario", "password": "test-password"})

        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_customer_sees_the_systems_of_the_community_with_status_and_link(self):
        self.client.force_login(self.user)

        response = self.client.get("/")

        rows = response.context["rows"]
        # Prima i guasti, poi i pannelli sporchi, gli impianti a posto e quelli mai controllati
        self.assertEqual([system.name for system, _ in rows], ["Capannone", "Magazzino", "Sede", "Uffici"])
        self.assertEqual(response.context["counts"], {"total": 4, "broken": 0, "fault": 1, "dirty": 1, "ok": 1, "new": 1})
        self.assertContains(response, "Probabile guasto")
        self.assertContains(response, "In attesa del primo controllo")
        self.assertContains(response, "Titolare")  # il primo utente è il titolare
        for system, _ in rows:
            self.assertContains(response, f'href="/sp/system/{system.id}/"')
        self.assertNotContains(response, "Impianto altrui")

    def test_status_filter(self):
        self.client.force_login(self.user)

        response = self.client.get("/?status=fault")

        self.assertEqual([system.name for system, _ in response.context["rows"]], ["Capannone"])

    def test_faults_confirmed_after_the_washing_come_first(self):
        checked(create_system(self.community, "Tettoia"), Status.BROKEN)
        self.client.force_login(self.user)

        response = self.client.get("/")

        self.assertEqual(response.context["rows"][0][0].name, "Tettoia")
        self.assertEqual(response.context["counts"]["broken"], 1)
        self.assertContains(response, "fa-times-circle me-1\"></i>Guasto")
        self.assertEqual([system.name for system, _ in self.client.get("/?status=broken").context["rows"]], ["Tettoia"])

    def test_staff_sees_every_system_with_its_community(self):
        self.client.force_login(User.objects.create_user("tecnico", is_staff=True))

        response = self.client.get("/")

        self.assertEqual(response.context["counts"]["total"], 5)
        self.assertContains(response, "Impianto altrui")
        self.assertContains(response, "Bianchi Srl")

    def test_menu_links_to_production_and_forecasts_only_for_customers(self):
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertContains(response, 'href="/sp/community/"')
        self.assertContains(response, 'href="/forecast/"')

        self.client.force_login(User.objects.create_user("tecnico", is_staff=True))
        response = self.client.get("/")
        self.assertNotContains(response, 'href="/sp/community/"')
        self.assertNotContains(response, 'href="/forecast/"')

    @mock.patch("SP.views.get_day_weather", return_value=DEFAULT_WEATHER)
    def test_today_production_of_the_community(self, get_day_weather):
        today = timezone.now()
        system = self.community.photovoltaic_systems.get(name="Sede")
        add_readings(system, utc(today.year, today.month, today.day, 10, 0), [2.0] * 61)
        self.client.force_login(self.user)

        response = self.client.get("/")

        production = response.context["today"]
        self.assertAlmostEqual(production["energy"], 2.0)  # un'ora a 2 kW
        self.assertEqual(production["peak"], 2.0)
        self.assertAlmostEqual(production["specific_yield"], 2.0 / 16)  # 4 impianti da 4 kW
        self.assertEqual(production["weather"], DEFAULT_WEATHER)
        get_day_weather.assert_called_once_with(self.community.city, today.date())
        self.assertTrue(response.context["chart_data"])
        self.assertContains(response, 'id="productionChart"')

    def test_no_readings_today(self):
        add_readings(self.community.photovoltaic_systems.get(name="Sede"), utc(2026, 9, 28, 12, 0), [2.0] * 61)
        self.client.force_login(self.user)

        response = self.client.get("/")

        self.assertContains(response, "Oggi non sono ancora arrivate misure")
        self.assertNotContains(response, 'id="productionChart"')

    def test_staff_home_has_no_community_production(self):
        self.client.force_login(User.objects.create_user("tecnico", is_staff=True))

        self.assertNotIn("today", self.client.get("/").context)

    def test_access(self):
        self.client.force_login(User.objects.create_user("nessuno"))
        self.assertEqual(self.client.get("/").status_code, 403)


class LandingPageTests(TestCase):
    def test_visitors_see_the_services_and_the_link_to_the_city_comparison(self):
        modena = create_city()
        community = create_community("Rossi Trasporti", city=modena)
        create_system(community, "Sede", max_power=10.0)
        create_system(community, "Magazzino", max_power=5.5)
        create_system(create_community("Bianchi Srl", city=modena), "Sede")

        response = self.client.get("/")

        self.assertTemplateUsed(response, "SP/landing.html")
        self.assertContains(response, 'href="/sp/cities/"')
        self.assertContains(response, 'href="/sp/login/"')
        self.assertEqual(
            {key: response.context[key] for key in ("systems", "installed_power", "communities", "cities")},
            {"systems": 3, "installed_power": 19.5, "communities": 2, "cities": 1},
        )

    def test_logout_leads_to_the_landing_page(self):
        self.client.force_login(create_customer(create_community()))

        response = self.client.post("/sp/logout/")

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertTemplateUsed(self.client.get("/"), "SP/landing.html")


@mock.patch("SP.views.get_day_weather", return_value=DEFAULT_WEATHER)
class SystemPageLayoutTests(TestCase):
    def test_map_in_the_weather_card_and_interventions_at_the_end(self, _):
        community = create_community()
        system = create_system(community)
        add_readings(system, utc(2026, 9, 28, 12, 0), [2.0] * 61)
        system.interventions.create(preferred_date=timezone.localdate(), requested_by=create_customer(community))
        self.client.force_login(Customer.objects.get().user)

        content = self.client.get(f"/sp/system/{system.id}/?day=2026-09-28").content.decode()

        positions = [content.index(text) for text in (
            "Energia Totale Prodotta", "Previsioni Meteo", "maps.google.com", "productionChart", "temperatureChart",
            "Interventi di manutenzione",
        )]
        self.assertEqual(positions, sorted(positions))
