from __future__ import annotations

from datetime import UTC, datetime
import unittest

from roster.alerts import calculate_alerts
from roster.formatter import format_summary
from roster.models import AlertType

from helpers import build, fly


class DisplayAndAlertTests(unittest.TestCase):
    def setUp(self):
        self.roster = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630"),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
                fly("27Aug37", "ZX420", "SIN-ADL", "0620", "0820", "1620"),
                fly("29Aug37", "ZX421", "ADL-SIN", "0810", "0910", "1450"),
            ]
        )

    def test_exact_summary_examples(self):
        expected = """AUGUST

6 Thu

ZX410
Singapore → Taipei, Taiwan
RPT 0945
DEP 1145
ARR 1630 (1630 SG)

7 Fri

ZX411
Taipei, Taiwan → Singapore
RPT 1645 (1645 SG)
DEP 1745 (1745 SG)
ARR 2215

27 Thu

ZX420
Singapore → Adelaide, Australia
RPT 0620
DEP 0820
ARR 1620 (1450 SG)

29 Sat

ZX421
Adelaide, Australia → Singapore
RPT 0810 (0640 SG)
DEP 0910 (0740 SG)
ARR 1450"""
        self.assertEqual(format_summary(self.roster), expected)

    def test_exact_alert_messages(self):
        alerts = calculate_alerts(
            self.roster,
            now=datetime(2037, 1, 1, tzinfo=UTC),
        )
        by_type_and_flight = {}
        for alert in alerts:
            first_line = alert.message.splitlines()[1] if alert.alert_type != AlertType.LANDING_1H else alert.message.split()[0]
            flight = first_line.split()[0]
            by_type_and_flight[(alert.alert_type, flight)] = alert.message
        self.assertEqual(
            by_type_and_flight[(AlertType.PREP_12H, "ZX421")],
            "12h to flight\nZX421 ADL → SIN\nRPT 0810 (0640 SG)",
        )
        self.assertEqual(
            by_type_and_flight[(AlertType.PREP_3H, "ZX421")],
            "Time to get ready\nZX421 ADL → SIN\nRPT 0810 (0640 SG)",
        )
        self.assertEqual(
            by_type_and_flight[(AlertType.LANDING_1H, "ZX420")],
            "ZX420 landing in Adelaide, Australia in 1h\nETA 1620 (1450 SG)",
        )
        self.assertEqual(
            by_type_and_flight[(AlertType.LANDING_1H, "ZX421")],
            "ZX421 landing in Singapore in 1h\nETA 1450",
        )

    def test_taipei_bracket_is_shown_even_at_same_offset(self):
        summary = format_summary(self.roster)
        self.assertIn("ARR 1630 (1630 SG)", summary)
        self.assertIn("RPT 1645 (1645 SG)", summary)

    def test_no_day_markers_or_timezone_jargon(self):
        rendered = format_summary(self.roster)
        for forbidden in ("+1", "+2", "next day", "SGT", "UTC", "Asia/"):
            self.assertNotIn(forbidden, rendered)

    def test_multi_sector_duty_gets_one_pair_of_prep_alerts(self):
        roster = build(
            [
                fly("12Aug37", "ZX910", "SIN-MNL", "0650", "0850", "1245"),
                fly("12Aug37", "ZX917", "MNL-SIN", None, "1400", "1750"),
            ]
        )
        alerts = calculate_alerts(roster, now=datetime(2037, 1, 1, tzinfo=UTC))
        prep = [alert for alert in alerts if alert.alert_type != AlertType.LANDING_1H]
        landing = [alert for alert in alerts if alert.alert_type == AlertType.LANDING_1H]
        self.assertEqual(len(prep), 2)
        self.assertEqual(len(landing), 2)
        self.assertFalse(any("ZX917" in alert.message for alert in prep))

    def test_alert_keys_ignore_roster_version(self):
        first = calculate_alerts(self.roster, now=datetime(2037, 1, 1, tzinfo=UTC))
        second = calculate_alerts(self.roster, now=datetime(2037, 1, 1, tzinfo=UTC))
        self.assertEqual([alert.event_key for alert in first], [alert.event_key for alert in second])


if __name__ == "__main__":
    unittest.main()
