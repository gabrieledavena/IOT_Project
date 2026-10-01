from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from SP.models import Customer, PhotovoltaicSystem
from SP.tests.helpers import DEFAULT_WEATHER, add_readings, create_community, create_customer, create_system, utc

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

    def test_login_leads_to_the_dashboard(self):
        response = self.client.post("/sp/login/", {"username": "mario", "password": "test-password"})

        self.assertRedirects(response, "/sp/dashboard/", fetch_redirect_response=False)

    def test_customer_sees_the_systems_of_the_community_with_status_and_link(self):
        self.client.force_login(self.user)

        response = self.client.get("/sp/dashboard/")

        rows = response.context["rows"]
        # Prima i guasti, poi i pannelli sporchi, gli impianti a posto e quelli mai controllati
        self.assertEqual([system.name for system, _ in rows], ["Capannone", "Magazzino", "Sede", "Uffici"])
        self.assertEqual(response.context["counts"], {"total": 4, "fault": 1, "dirty": 1, "ok": 1, "new": 1})
        self.assertContains(response, "Probabile guasto")
        self.assertContains(response, "In attesa del primo controllo")
        self.assertContains(response, "Titolare")  # il primo utente è il titolare
        for system, _ in rows:
            self.assertContains(response, f'href="/sp/system/{system.id}/"')
        self.assertNotContains(response, "Impianto altrui")

    def test_status_filter(self):
        self.client.force_login(self.user)

        response = self.client.get("/sp/dashboard/?status=fault")

        self.assertEqual([system.name for system, _ in response.context["rows"]], ["Capannone"])

    def test_staff_sees_every_system_with_its_community(self):
        self.client.force_login(User.objects.create_user("tecnico", is_staff=True))

        response = self.client.get("/sp/dashboard/")

        self.assertEqual(response.context["counts"]["total"], 5)
        self.assertContains(response, "Impianto altrui")
        self.assertContains(response, "Bianchi Srl")

    def test_access(self):
        self.assertRedirects(self.client.get("/sp/dashboard/"), "/sp/login/?next=/sp/dashboard/",
                             fetch_redirect_response=False)
        self.client.force_login(User.objects.create_user("nessuno"))
        self.assertEqual(self.client.get("/sp/dashboard/").status_code, 403)


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
