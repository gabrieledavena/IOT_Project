from datetime import date, timedelta

from django.test import TestCase

from SP.models import PanelData
from SP.production import (
    daily_energy_kwh, energy_kwh, get_community_series, get_system_series, system_daily_energy_kwh,
)
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


class SystemDailyEnergyTests(TestCase):
    def test_equals_the_energy_of_the_minute_by_minute_series(self):
        system = create_system(create_community())
        start = utc(2026, 9, 26, 23, 50)
        # Letture ogni minuto, un buco di 7 minuti, uno a cavallo della mezzanotte e uno di più di un giorno
        offsets = [0, 1, 2, 9, 10, 11, 25, 26, 40, 41 + 26 * 60, 42 + 26 * 60, 50 + 26 * 60]
        PanelData.objects.bulk_create([
            PanelData(system=system, time_stamp=start + timedelta(minutes=offset, seconds=20 * (i % 2)),
                      temperature=20.0, lightness=500.0, power=0.5 + (i * 7 % 5))
            for i, offset in enumerate(offsets)
        ])

        expected = daily_energy_kwh(get_system_series(system))
        actual = system_daily_energy_kwh(system)

        self.assertEqual(actual.keys(), expected.keys())
        self.assertEqual(len(actual), 3)
        for day, energy in expected.items():
            self.assertAlmostEqual(actual[day], energy, places=3)

    def test_needs_at_least_two_readings(self):
        system = create_system(create_community())
        add_readings(system, utc(2026, 9, 28, 12, 0), [3.0])

        self.assertEqual(system_daily_energy_kwh(system), {})
