from __future__ import annotations

from datetime import date, timedelta
import unittest

from roster.airports import AirportCatalog
from roster.timezones import resolve_event


class TimezoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.airports = AirportCatalog(records={})

    def event(self, iata: str, day: date, time: str):
        airport = self.airports.lookup(iata)
        self.assertIsNotNone(airport)
        return resolve_event(day, time, airport)

    def test_regression_airport_timezones(self):
        expected = {
            "SIN": "Asia/Singapore",
            "TPE": "Asia/Taipei",
            "ADL": "Australia/Adelaide",
            "SYD": "Australia/Sydney",
            "MEL": "Australia/Melbourne",
            "PER": "Australia/Perth",
            "SFO": "America/Los_Angeles",
            "LHR": "Europe/London",
            "BLR": "Asia/Kolkata",
            "DPS": "Asia/Makassar",
            "CGK": "Asia/Jakarta",
            "MLE": "Indian/Maldives",
        }
        for code, timezone in expected.items():
            with self.subTest(code=code):
                self.assertEqual(self.airports.lookup(code).timezone, timezone)

    def test_port_to_singapore_conversions(self):
        day = date(2037, 8, 10)
        expected = {
            "TPE": ("1200", "2037-08-10"),
            "ADL": ("1030", "2037-08-10"),
            "SYD": ("1000", "2037-08-10"),
            "MEL": ("1000", "2037-08-10"),
            "PER": ("1200", "2037-08-10"),
            "SFO": ("0300", "2037-08-11"),
            "LHR": ("1900", "2037-08-10"),
            "DPS": ("1200", "2037-08-10"),
            "CGK": ("1300", "2037-08-10"),
            "MLE": ("1500", "2037-08-10"),
        }
        for code, (time, sg_date) in expected.items():
            with self.subTest(code=code):
                event = self.event(code, day, "1200")
                self.assertEqual(event.singapore_datetime.strftime("%H%M"), time)
                self.assertEqual(event.singapore_datetime.date().isoformat(), sg_date)

    def test_india_half_hour_and_singapore_date_difference(self):
        event = self.event("BLR", date(2037, 8, 10), "2305")
        self.assertEqual(event.singapore_datetime.strftime("%H%M"), "0135")
        self.assertEqual(event.singapore_datetime.date(), date(2037, 8, 11))

    def test_adelaide_half_hour_and_dst(self):
        winter = self.event("ADL", date(2037, 8, 10), "1200")
        summer = self.event("ADL", date(2037, 1, 10), "1200")
        self.assertEqual(winter.port_local_datetime.utcoffset(), timedelta(hours=9, minutes=30))
        self.assertEqual(summer.port_local_datetime.utcoffset(), timedelta(hours=10, minutes=30))

    def test_dst_for_sydney_melbourne_sfo_and_london(self):
        expectations = {
            "SYD": (timedelta(hours=11), timedelta(hours=10)),
            "MEL": (timedelta(hours=11), timedelta(hours=10)),
            "SFO": (timedelta(hours=-8), timedelta(hours=-7)),
            "LHR": (timedelta(hours=0), timedelta(hours=1)),
        }
        for code, (january, july) in expectations.items():
            with self.subTest(code=code):
                self.assertEqual(
                    self.event(code, date(2037, 1, 10), "1200").port_local_datetime.utcoffset(),
                    january,
                )
                self.assertEqual(
                    self.event(code, date(2037, 7, 10), "1200").port_local_datetime.utcoffset(),
                    july,
                )

    def test_international_date_crossing(self):
        event = self.event("SFO", date(2037, 8, 1), "2330")
        self.assertEqual(event.singapore_datetime.strftime("%Y-%m-%d %H%M"), "2037-08-02 1430")

    def test_month_crossing(self):
        event = self.event("BLR", date(2037, 12, 31), "2305")
        self.assertEqual(event.singapore_datetime.strftime("%Y-%m-%d %H%M"), "2038-01-01 0135")

    def test_sin_round_trip_time_is_unchanged(self):
        event = self.event("SIN", date(2037, 8, 10), "0810")
        self.assertEqual(event.singapore_datetime, event.port_local_datetime)


if __name__ == "__main__":
    unittest.main()
