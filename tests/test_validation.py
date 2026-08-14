from __future__ import annotations

from datetime import UTC, datetime
import unittest

from roster.alerts import calculate_alerts
from roster.formatter import format_summary

from helpers import build, fly


class ValidationTests(unittest.TestCase):
    def test_unknown_airport_needs_review_and_is_not_scheduled(self):
        roster = build([fly("06Aug37", "ZX999", "SIN-ZZZ", "0945", "1145", "1630")])
        self.assertTrue(any(issue.code == "unknown_destination" for issue in roster.issues))
        self.assertEqual(calculate_alerts(roster, now=datetime(2037, 1, 1, tzinfo=UTC)), [])

    def test_critical_unreadable_field_needs_review(self):
        roster = build(
            [
                fly(
                    "06Aug37",
                    "ZX410",
                    "SIN-TPE",
                    "0945",
                    "1145",
                    "1630",
                    unreadable=["sta"],
                )
            ]
        )
        self.assertFalse(roster.sectors[0].valid)
        self.assertTrue(any(issue.code == "critical_unreadable" for issue in roster.issues))

    def test_large_printed_flight_time_mismatch_blocks_only_affected_sector(self):
        roster = build(
            [
                fly(
                    "06Aug37",
                    "ZX410",
                    "SIN-TPE",
                    "0945",
                    "1145",
                    "1630",
                    flight_time="01:00",
                ),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
            ]
        )
        self.assertFalse(roster.sectors[0].valid)
        self.assertTrue(roster.sectors[1].valid)
        alerts = calculate_alerts(roster, now=datetime(2037, 1, 1, tzinfo=UTC))
        self.assertEqual(len(alerts), 3)
        self.assertTrue(all("ZX411" in alert.message for alert in alerts))

    def test_implausible_date_rollover_needs_review(self):
        roster = build(
            [fly("06Aug37", "ZX410", "SIN-TPE", "0945", "2300", "0100")]
        )
        self.assertTrue(any(issue.code == "impossible_duration" for issue in roster.issues))

    def test_report_period_keeps_next_month_flights(self):
        roster = build(
            [
                fly("27Aug37", "ZX420", "SIN-ADL", "0620", "0820", "1620"),
                fly("06Sep37", "ZX828", "SIN-PVG", "0800", "1000", "1520"),
            ]
        )
        summary = format_summary(roster)
        self.assertIn("AUGUST", summary)
        self.assertIn("SEPTEMBER", summary)
        self.assertIn("ZX828", summary)

    def test_duplicate_sector_is_rejected(self):
        row = fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")
        roster = build([row, dict(row, row_index=1)])
        self.assertTrue(any(issue.code == "duplicate_sector" for issue in roster.issues))


if __name__ == "__main__":
    unittest.main()
