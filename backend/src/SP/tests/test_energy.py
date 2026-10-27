from datetime import date

from django.test import TestCase

from SP.energy import CO2_KG_PER_CAR_KM, CO2_KG_PER_KWH, CO2_KG_PER_TREE, period_summary, totalizer
from SP.models import PanelData
from SP.tests.helpers import add_readings, create_community, create_customer, create_system, create_technician, utc

TODAY = date(2026, 10, 10)


class EnergyTestCase(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.system = create_system(self.community, "Sede", max_power=2.0)
        # Un'ora a 1 kW, due ore a 0,5 kW e mezz'ora a 1 kW: 1 kWh, 1 kWh e 0,5 kWh
        add_readings(self.system, utc(2025, 12, 31, 12, 0), [1.0] * 60)
        add_readings(self.system, utc(2026, 10, 5, 10, 0), [0.5] * 120)
        add_readings(self.system, utc(2026, 10, 3, 10, 0), [1.0] * 30)
        self.readings = PanelData.objects.filter(system=self.system)


class TotalizerTests(EnergyTestCase):
    def test_energy_since_the_first_reading_in_the_year_and_in_the_month(self):
        totals = totalizer(self.readings, 2.0, TODAY)

        self.assertAlmostEqual(totals["total"], 2.5)
        self.assertAlmostEqual(totals["year"], 1.5)
        self.assertAlmostEqual(totals["month"], 1.5)
        self.assertEqual(totals["since"], date(2025, 12, 31))
        # Media sui 3 giorni con misure e resa sui 2 kW installati
        self.assertAlmostEqual(totals["daily_mean"], 2.5 / 3)
        self.assertAlmostEqual(totals["specific_yield"], 1.25)

    def test_environmental_impact(self):
        totals = totalizer(self.readings, 2.0, TODAY)

        co2 = 2.5 * CO2_KG_PER_KWH
        self.assertAlmostEqual(totals["co2_kg"], co2)
        self.assertAlmostEqual(totals["trees"], co2 / CO2_KG_PER_TREE)
        self.assertAlmostEqual(totals["car_km"], co2 / CO2_KG_PER_CAR_KM)

    def test_no_readings(self):
        self.assertIsNone(totalizer(PanelData.objects.none(), 2.0, TODAY))


class PeriodSummaryTests(EnergyTestCase):
    def test_month_with_a_bar_per_day(self):
        add_readings(self.system, utc(2026, 10, 10, 9, 0), [3.0] * 20)  # oggi, incompleto: 1 kWh

        summary = period_summary(self.readings, 2.0, "month", "2026-10", TODAY)

        self.assertEqual((summary["label"], summary["value"]), ("Ottobre 2026", "2026-10"))
        self.assertEqual(len(summary["bars"]), 31)
        self.assertEqual([bar["energy"] for bar in summary["bars"][:10]], [None, None, 0.5, None, 1.0, None, None, None, None, 1.0])
        stats = summary["stats"]
        # Minimo, massimo e media senza il giorno in corso; il totale con
        self.assertEqual((stats["min"], stats["min_day"], stats["max"], stats["max_day"]), (0.5, "03/10", 1.0, "05/10"))
        self.assertEqual((stats["mean"], stats["total"], stats["days"]), (0.75, 2.5, 3))
        self.assertEqual((stats["specific_yield"], stats["daily_specific_yield"]), (1.25, 0.38))
        # Ci sono misure prima (dicembre 2025), nessun mese dopo quello in corso
        self.assertEqual((summary["previous"], summary["next"]), ("2026-09", None))

    def test_year_with_a_bar_per_month(self):
        summary = period_summary(self.readings, 2.0, "year", "2026", TODAY)

        self.assertEqual(summary["label"], "2026")
        self.assertEqual([bar["energy"] for bar in summary["bars"]], [None] * 9 + [1.5, None, None])
        self.assertEqual(summary["bars"][9]["label"], "Ott")
        self.assertEqual(summary["stats"]["total"], 1.5)
        self.assertEqual((summary["previous"], summary["next"]), ("2025", None))

        last_year = period_summary(self.readings, 2.0, "year", "2025", TODAY)
        self.assertEqual(last_year["stats"]["total"], 1.0)
        self.assertEqual((last_year["previous"], last_year["next"]), (None, "2026"))

    def test_invalid_or_future_periods_show_the_current_one(self):
        for period, value in (("month", "2027-01"), ("month", "ottobre"), ("week", None), ("month", None)):
            with self.subTest(period=period, value=value):
                self.assertEqual(period_summary(self.readings, 2.0, period, value, TODAY)["value"], "2026-10")
        self.assertEqual(period_summary(self.readings, 2.0, "year", "x", TODAY)["value"], "2026")

    def test_period_without_readings(self):
        summary = period_summary(self.readings, 2.0, "month", "2026-06", TODAY)

        self.assertTrue(all(bar["energy"] is None for bar in summary["bars"]))
        self.assertEqual((summary["stats"]["total"], summary["stats"]["min"], summary["stats"]["mean"]), (0, None, None))


class EnergyPagesTests(EnergyTestCase):
    def setUp(self):
        super().setUp()
        self.user = create_customer(self.community)

    def test_summary_of_the_community_and_of_a_system(self):
        other = create_system(self.community, "Magazzino", max_power=3.0)
        add_readings(other, utc(2026, 10, 5, 10, 0), [1.0] * 60)
        self.client.force_login(self.user)

        community = self.client.get("/sp/community/energy/", {"period": "year", "date": "2026"}).json()
        system = self.client.get(f"/sp/system/{self.system.id}/energy/", {"period": "year", "date": "2026"}).json()

        self.assertEqual((community["stats"]["total"], community["stats"]["specific_yield"]), (2.5, 0.5))
        self.assertEqual((system["stats"]["total"], system["stats"]["specific_yield"]), (1.5, 0.75))

    def test_access(self):
        foreign = create_system(create_community("Bianchi Srl"), "Altrui")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(f"/sp/system/{foreign.id}/energy/").status_code, 403)

        self.client.force_login(create_technician())
        self.assertEqual(self.client.get(f"/sp/system/{foreign.id}/energy/").status_code, 200)
        self.assertEqual(self.client.get("/sp/community/energy/").status_code, 403)  # nessuna community

        self.client.logout()
        self.assertEqual(self.client.get("/sp/community/energy/").status_code, 302)

    def test_totalizer_and_summary_on_the_home_and_the_system_page(self):
        self.client.force_login(self.user)

        home = self.client.get("/")
        self.assertAlmostEqual(home.context["totals"]["total"], 2.5)
        self.assertContains(home, "Totalizzatore della community")
        self.assertContains(home, 'data-energy-summary="/sp/community/energy/"')

        page = self.client.get(f"/sp/system/{self.system.id}/", follow=True)
        self.assertContains(page, f'data-energy-summary="/sp/system/{self.system.id}/energy/"')

    def test_technicians_have_no_community_totalizer(self):
        self.client.force_login(create_technician())

        home = self.client.get("/")

        self.assertNotIn("totals", home.context)
        self.assertNotContains(home, "data-energy-summary")

    def test_users_without_readings(self):
        community = create_community("Nuova Srl")
        create_system(community, "Sede")
        self.client.force_login(create_customer(community, username="nuovo"))

        home = self.client.get("/")

        self.assertIsNone(home.context["totals"])
        self.assertNotContains(home, "Totalizzatore della community")
        self.assertContains(home, "data-energy-summary")
