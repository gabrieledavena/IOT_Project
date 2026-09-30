from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from SP.benchmark import measured_city_yields, reference_yields
from SP.models import PanelData
from SP.tests.helpers import add_readings, create_city, create_community, create_system, reference_dataset, utc


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


class ReferenceYieldsTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_yield_is_the_average_of_every_day_of_each_location(self):
        dataset = reference_dataset(locations=2, days=10)
        with mock.patch("SP.benchmark.load_reference_dataset", return_value=dataset):
            yields = reference_yields()

        expected = dataset[dataset["location"] == "Località 1"]["specific_yield"].mean()
        self.assertEqual(len(yields), 2)
        self.assertEqual(
            {k: yields[1][k] for k in ("name", "latitude", "longitude")},
            {"name": "Località 1", "latitude": 39, "longitude": 12.0},
        )
        self.assertAlmostEqual(yields[1]["specific_yield"], expected)


class CityBenchmarkPageTests(TestCase):
    REFERENCE = [{"name": "Bari", "latitude": 41.117, "longitude": 16.872, "specific_yield": 4.046605}]

    def setUp(self):
        cache.clear()
        self.yesterday = timezone.now().date() - timedelta(days=1)
        self.modena = create_city("Modena", province="Modena", region="Emilia-Romagna")
        self.bari = create_city("Bari", province="Bari", region="Puglia")
        system = create_system(create_community(city=self.modena), max_power=2.0)
        add_readings(system, at_noon(self.yesterday), [3.0] * 61)

        patcher = mock.patch("SP.views.reference_yields", return_value=self.REFERENCE)
        self.reference_yields = patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_map_shows_the_measured_cities_with_coordinates_and_the_reference_yields(self):
        # Città con misure ma senza coordinate: in tabella sì, sulla mappa no
        unknown = create_city("Senza coordinate", latitude=None, longitude=None)
        system = create_system(create_community("Community B", city=unknown))
        add_readings(system, at_noon(self.yesterday), [1.0] * 61)

        response = self.client.get("/sp/cities/?region=Puglia")

        heatmap = response.context["heatmap"]
        self.assertEqual(heatmap["measured"], [{
            "name": "Modena", "province": "Modena", "latitude": self.modena.latitude, "longitude": self.modena.longitude,
            "specific_yield": 1.5, "systems": 1, "days": 1,
            "first_day": self.yesterday.strftime("%d/%m/%Y"), "last_day": self.yesterday.strftime("%d/%m/%Y"),
        }])
        self.assertEqual(heatmap["reference"], [{**self.REFERENCE[0], "specific_yield": 4.047}])
        self.assertContains(response, 'id="heatmap-data"')
        self.assertContains(response, "SP/city_heatmap.js")

    def test_map_is_hidden_without_any_data(self):
        PanelData.objects.all().delete()
        self.reference_yields.return_value = []

        response = self.client.get("/sp/cities/")

        self.assertNotContains(response, "city-heatmap")
