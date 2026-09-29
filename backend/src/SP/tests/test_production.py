from datetime import date

from django.test import TestCase

from SP.production import daily_energy_kwh, energy_kwh, get_community_series, get_system_series
from SP.tests.helpers import add_readings, create_community, create_system, utc


class SystemSeriesTests(TestCase):
    def setUp(self):
        self.system = create_system(create_community())

    def test_power_is_interpolated_linearly_between_readings(self):
        add_readings(self.system, utc(2026, 9, 28, 12, 0), [0.0, 3.0], step_minutes=3)

        series = get_system_series(self.system)

        self.assertEqual([p["timestamp"] for p in series], [
            "2026-09-28T12:01:00", "2026-09-28T12:02:00", "2026-09-28T12:03:00",
        ])
        self.assertEqual([p["value"] for p in series], [1.0, 2.0, 3.0])

    def test_instantaneous_power_is_not_treated_as_a_counter(self):
        # Una potenza che scende non deve essere scambiata per un contatore azzerato
        add_readings(self.system, utc(2026, 9, 28, 12, 0), [5.0, 4.0, 3.0])

        self.assertEqual([p["value"] for p in get_system_series(self.system)], [4.0, 3.0])

    def test_day_filter_keeps_only_that_day(self):
        add_readings(self.system, utc(2026, 9, 27, 23, 58), [1.0, 1.0, 1.0, 1.0])  # 23:58 -> 00:01

        series = get_system_series(self.system, date(2026, 9, 28))

        self.assertEqual([p["timestamp"] for p in series], ["2026-09-28T00:01:00"])


class CommunitySeriesTests(TestCase):
    def test_community_series_sums_the_systems(self):
        community = create_community()
        add_readings(create_system(community, "A"), utc(2026, 9, 28, 12, 0), [1.0, 1.0, 1.0])
        add_readings(create_system(community, "B"), utc(2026, 9, 28, 12, 0), [2.0, 2.0, 2.0])

        self.assertEqual([p["value"] for p in get_community_series(community)], [3.0, 3.0])


class EnergyTests(TestCase):
    def test_constant_power_for_an_hour(self):
        series = [{"timestamp": f"2026-09-28T12:{m:02d}:00", "value": 6.0} for m in range(60)]

        self.assertAlmostEqual(energy_kwh(series), 6.0)

    def test_daily_energy_groups_by_day(self):
        series = [
            {"timestamp": "2026-09-27T12:00:00", "value": 60.0},
            {"timestamp": "2026-09-28T12:00:00", "value": 30.0},
            {"timestamp": "2026-09-28T12:01:00", "value": 30.0},
        ]

        self.assertEqual(daily_energy_kwh(series), {date(2026, 9, 27): 1.0, date(2026, 9, 28): 1.0})
