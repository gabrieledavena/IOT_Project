from datetime import date

from django.test import TestCase

from SP.reports import day_report, latest_reading
from SP.tests.helpers import add_readings, create_community, create_system, install_device, utc

NOW = utc(2026, 11, 17, 15, 0)


class DayReportTests(TestCase):
    def setUp(self):
        self.community = create_community("Rossi Trasporti")
        self.sede = create_system(self.community, "Sede", max_power=4.0)
        self.magazzino = create_system(self.community, "Magazzino", max_power=2.0)
        self.negozio = create_system(self.community, "Negozio", max_power=4.0)

    def test_energy_peaks_and_temperatures_of_a_past_day(self):
        # Sede: 3 kW dalle 10 alle 12 (6 kWh); Magazzino: 1 kW dalle 11 alle 12, poi 2 kW per mezz'ora (2 kWh)
        add_readings(self.sede, utc(2026, 11, 16, 10, 0), [3.0] * 120, temperature=20.0)
        add_readings(self.magazzino, utc(2026, 11, 16, 11, 0), [1.0] * 60 + [2.0] * 30, temperature=10.0)
        # Il giorno prima la Sede ha prodotto 3 kWh; le misure di altre community non contano
        add_readings(self.sede, utc(2026, 11, 15, 10, 0), [3.0] * 60)
        add_readings(create_system(create_community("Altra")), utc(2026, 11, 16, 10, 0), [9.0] * 60)

        report = day_report(self.community, date(2026, 11, 16), now=NOW)

        self.assertTrue(report.complete)
        self.assertEqual([(item.system.name, round(item.energy, 6), item.readings) for item in report.systems],
                         [("Magazzino", 2.0, 90), ("Negozio", 0.0, 0), ("Sede", 6.0, 120)])
        magazzino, negozio, sede = report.systems
        self.assertEqual((sede.peak, sede.peak_at), (3.0, utc(2026, 11, 16, 10, 0)))
        self.assertEqual((magazzino.peak, magazzino.peak_at), (2.0, utc(2026, 11, 16, 12, 0)))
        self.assertEqual((negozio.peak, negozio.peak_at), (None, None))
        self.assertAlmostEqual(sede.specific_yield, 1.5)
        self.assertAlmostEqual(report.energy, 8.0)
        self.assertAlmostEqual(report.specific_yield, 0.8)  # 10 kW installati
        # Picco della community: dalle 11 producono insieme 3 + 1 kW
        self.assertEqual((report.peak, report.peak_at), (4.0, utc(2026, 11, 16, 11, 0)))
        self.assertEqual((report.temperature_min, report.temperature_max), (10.0, 20.0))
        self.assertAlmostEqual(report.previous_energy, 3.0)
        self.assertAlmostEqual(report.change, 5 / 3)
        self.assertAlmostEqual(report.coverage, 210 / (3 * 24 * 60))
        self.assertAlmostEqual(report.co2_kg, 2.0)

    def test_today_until_now_compared_with_yesterday_until_the_same_time(self):
        add_readings(self.sede, utc(2026, 11, 17, 14, 0), [3.0] * 60)
        # Ieri ha prodotto anche dopo le 15: nel confronto conta solo fino alle 15
        add_readings(self.sede, utc(2026, 11, 16, 14, 0), [3.0] * 120)

        report = day_report(self.community, date(2026, 11, 17), now=NOW)

        self.assertFalse(report.complete)
        self.assertEqual(report.end, NOW)
        self.assertAlmostEqual(report.energy, 3.0)
        self.assertAlmostEqual(report.previous_energy, 3.0)
        self.assertAlmostEqual(report.change, 0.0)
        self.assertAlmostEqual(report.coverage, 60 / (3 * 15 * 60))

    def test_a_day_without_readings(self):
        report = day_report(self.community, date(2026, 11, 10), now=NOW)

        self.assertEqual(report.energy, 0.0)
        self.assertEqual((report.peak, report.previous_energy, report.change), (None, None, None))
        self.assertEqual((report.temperature_min, report.coverage), (None, 0.0))


class LatestReadingTests(TestCase):
    def setUp(self):
        self.system = create_system(create_community(), "Sede", max_power=4.0)

    def test_last_reading_energy_of_today_and_device(self):
        add_readings(self.system, utc(2026, 11, 16, 12, 0), [3.0] * 60)
        add_readings(self.system, utc(2026, 11, 17, 0, 0), [1.0] * 59 + [2.0], temperature=21.5, lightness=640.0)

        latest = latest_reading(self.system, now=NOW)

        self.assertEqual(latest.reading.time_stamp, utc(2026, 11, 17, 0, 59))
        self.assertEqual((latest.reading.power, latest.reading.temperature), (2.0, 21.5))
        self.assertAlmostEqual(latest.power_share, 0.5)
        self.assertAlmostEqual(latest.today_energy, 61 / 60)
        self.assertIsNone(latest.device)

        device, _ = install_device(self.system)
        self.assertEqual(latest_reading(self.system, now=NOW).device, device)

    def test_a_system_without_readings(self):
        latest = latest_reading(self.system, now=NOW)

        self.assertIsNone(latest.reading)
        self.assertIsNone(latest.power_share)
        self.assertEqual(latest.today_energy, 0.0)
