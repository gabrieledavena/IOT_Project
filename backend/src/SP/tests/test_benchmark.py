from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from SP.benchmark import measured_city_yields
from SP.tests.helpers import add_readings, create_city, create_community, create_system, utc


def at_noon(day):
    return utc(day.year, day.month, day.day, 12, 0)


class MeasuredCityYieldsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.today = timezone.now().date()
        self.yesterday = self.today - timedelta(days=1)

    def test_yield_is_energy_per_installed_kw_averaged_over_the_city(self):
        community = create_community()
        # Un'ora a 3 kW su 2 kW installati (1,5 kWh/kWp) e un'ora a 1 kW su 1 kW installato (1 kWh/kWp)
        add_readings(create_system(community, "A", max_power=2.0), at_noon(self.yesterday), [3.0] * 61)
        add_readings(create_system(community, "B", max_power=1.0), at_noon(self.yesterday), [1.0] * 61)

        stats = measured_city_yields()[community.city_id]

        self.assertAlmostEqual(stats["specific_yield"], 1.25)
        self.assertEqual((stats["systems"], stats["days"]), (2, 1))
        self.assertEqual(stats["first_day"], self.yesterday)

    def test_the_current_day_is_excluded(self):
        community = create_community()
        add_readings(create_system(community), at_noon(self.today) - timedelta(hours=12), [3.0] * 3)

        self.assertNotIn(community.city_id, measured_city_yields())


class CityBenchmarkPageTests(TestCase):
    def setUp(self):
        cache.clear()
        yesterday = timezone.now().date() - timedelta(days=1)
        self.modena = create_city("Modena", province="Modena", region="Emilia-Romagna")
        self.bari = create_city("Bari", province="Bari", region="Puglia")
        system = create_system(create_community(city=self.modena), max_power=2.0)
        add_readings(system, at_noon(yesterday), [3.0] * 61)

    def test_is_public_and_marks_cities_without_data(self):
        response = self.client.get("/sp/cities/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(rows[0][0], self.modena)  # prima le città con dati
        self.assertAlmostEqual(rows[0][1]["specific_yield"], 1.5)
        self.assertContains(response, "1,50")
        self.assertContains(response, "Nessuna informazione disponibile")

    def test_search_region_and_only_with_data_filters(self):
        self.assertEqual([c for c, _ in self.client.get("/sp/cities/?q=bar").context["rows"]], [self.bari])
        self.assertEqual([c for c, _ in self.client.get("/sp/cities/?region=Puglia").context["rows"]], [self.bari])
        self.assertEqual([c for c, _ in self.client.get("/sp/cities/?only_with_data=1").context["rows"]], [self.modena])

    def test_pagination_keeps_the_filters(self):
        for i in range(60):
            create_city(f"Comune {i}", region="Lazio")

        response = self.client.get("/sp/cities/?region=Lazio")

        self.assertEqual(response.context["page"].paginator.num_pages, 2)
        self.assertContains(response, "?region=Lazio&amp;page=2")
