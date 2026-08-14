from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

from roster.alerts import calculate_alerts
from roster.database import RosterDatabase
from roster.diff import diff_rosters
from roster.models import AlertType

from helpers import build, fly


NOW = datetime(2037, 1, 1, tzinfo=UTC)


def with_hash(roster, value):
    roster.file_set_hash = value
    return roster


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.original = build(
            [fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")]
        )

    def changed(self, row):
        return diff_rosters(self.original, build([row]))

    def test_added_flight(self):
        candidate = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630"),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
            ]
        )
        self.assertEqual(len(diff_rosters(self.original, candidate).added), 1)

    def test_removed_flight(self):
        candidate = build([], coverage="FULL")
        self.assertEqual(len(diff_rosters(self.original, candidate).removed), 1)

    def test_rpt_change(self):
        diff = self.changed(fly("06Aug37", "ZX410", "SIN-TPE", "1015", "1145", "1630"))
        self.assertTrue(any("RPT 0945 → 1015" in change.description for change in diff.changed))

    def test_std_change(self):
        diff = self.changed(fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1215", "1630"))
        self.assertTrue(any("DEP 1145 → 1215" in change.description for change in diff.changed))

    def test_sta_change(self):
        diff = self.changed(fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1700"))
        self.assertTrue(any("ARR 1630" in change.description for change in diff.changed))

    def test_route_change(self):
        diff = self.changed(fly("06Aug37", "ZX410", "SIN-HKG", "0945", "1145", "1545"))
        self.assertTrue(any("route" in change.description for change in diff.changed))

    def test_flight_number_change(self):
        diff = self.changed(fly("06Aug37", "ZX412", "SIN-TPE", "0945", "1145", "1630"))
        self.assertTrue(any("Flight ZX410 → ZX412" in change.description for change in diff.changed))

    def test_partial_never_reports_removed_unrelated_flight(self):
        active = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630"),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
            ]
        )
        candidate = build(
            [fly("06Aug37", "ZX410", "SIN-TPE", "1015", "1215", "1700")],
            coverage="PARTIAL",
        )
        self.assertFalse(diff_rosters(active, candidate).removed)


class DatabaseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "wife-roster.db"
        self.database = RosterDatabase(self.db_path)

    def tearDown(self):
        self.directory.cleanup()

    def apply(self, roster, file_hash):
        with_hash(roster, file_hash)
        return self.database.apply_roster(
            roster,
            [(Path(f"{file_hash}.png"), file_hash, 100)],
            now=NOW,
        )

    def test_exact_file_duplicate(self):
        roster = build([fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")])
        first = self.apply(roster, "file-a")
        second = self.apply(deepcopy(roster), "file-a")
        self.assertFalse(first.unchanged)
        self.assertTrue(second.unchanged)
        self.assertEqual(second.reason, "exact file set already processed")

    def test_database_and_new_parent_are_private(self):
        nested = Path(self.directory.name) / "new-private" / "state.db"
        database = RosterDatabase(nested)
        database.initialize()
        self.assertEqual(nested.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(nested.stat().st_mode & 0o777, 0o600)

    def test_same_content_from_different_screenshot(self):
        first_roster = build([fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")])
        second_roster = build([fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")])
        self.apply(first_roster, "file-a")
        result = self.apply(second_roster, "file-b")
        self.assertTrue(result.unchanged)
        self.assertEqual(result.reason, "normalized content unchanged")
        self.assertEqual(self.database.status_counts()["roster_versions"], 1)

    def test_unchanged_events_and_pending_alerts_survive_amendment(self):
        active = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630"),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
            ]
        )
        amended = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "1015", "1145", "1630"),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
            ]
        )
        self.apply(active, "file-a")
        original_keys = {alert.event_key for alert in self.database.pending_alerts()}
        result = self.apply(amended, "file-b")
        final_keys = {alert.event_key for alert in self.database.pending_alerts()}
        unchanged_second = {
            alert.event_key
            for alert in calculate_alerts(amended, now=NOW)
            if "ZX411" in alert.message
        }
        self.assertTrue(unchanged_second.issubset(original_keys & final_keys))
        self.assertEqual(result.created_alerts, 2)
        self.assertEqual(result.superseded_alerts, 2)

    def test_period_overlap_does_not_duplicate_alerts(self):
        active = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630"),
                fly("06Sep37", "ZX828", "SIN-PVG", "0800", "1000", "1520"),
            ],
            period_from="01Aug37",
            period_to="01Oct37",
        )
        overlap = build(
            [fly("06Sep37", "ZX828", "SIN-PVG", "0800", "1000", "1520")],
            period_from="01Sep37",
            period_to="01Oct37",
        )
        self.apply(active, "file-a")
        result = self.apply(overlap, "file-b")
        self.assertTrue(result.unchanged)
        self.assertEqual(len(self.database.pending_alerts()), 6)

    def test_partial_upload_never_deletes_unrelated_flights(self):
        active = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630"),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
            ]
        )
        partial = build(
            [fly("06Aug37", "ZX410", "SIN-TPE", "1015", "1215", "1700")],
            coverage="PARTIAL",
        )
        self.apply(active, "file-a")
        self.apply(partial, "file-b")
        self.assertEqual(
            {sector.flight_number for sector in self.database.active_roster().sectors},
            {"ZX410", "ZX411"},
        )

    def test_sent_alert_history_is_preserved(self):
        active = build([fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")])
        amended = build([fly("06Aug37", "ZX410", "SIN-TPE", "1015", "1145", "1630")])
        self.apply(active, "file-a")
        sent = next(
            alert
            for alert in self.database.pending_alerts()
            if alert.alert_type == AlertType.PREP_12H
        )
        self.database.mark_alert_sent(sent.event_key, sent_at=NOW, telegram_message_id="synthetic-1")
        self.apply(amended, "file-b")
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT status, telegram_message_id FROM alerts WHERE event_key=?",
                (sent.event_key,),
            ).fetchone()
        self.assertEqual(row, ("sent", "synthetic-1"))

    def test_new_full_roster_replaces_the_calendar_month(self):
        active = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630"),
                fly("31Aug37", "ZX431", "SIN-SYD", "1930", "2130", "0710"),
                fly("06Sep37", "ZX828", "SIN-PVG", "0800", "1000", "1520"),
            ],
            period_from="01Aug37",
            period_to="01Oct37",
        )
        replacement = build(
            [fly("14Aug37", "ZX215", "SIN-PER", "1645", "1845", "2355")],
            period_from="01Aug37",
            period_to="31Aug37",
        )
        self.apply(active, "file-a")
        result = self.apply(replacement, "file-b")
        flights = {sector.flight_number for sector in self.database.active_roster().sectors}
        self.assertEqual(flights, {"ZX215", "ZX828"})
        self.assertGreater(result.superseded_alerts, 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            active_alert_messages = {
                row[0]
                for row in connection.execute(
                    "SELECT message FROM alerts WHERE status IN ('pending','sending','failed')"
                )
            }
        self.assertFalse(any("ZX410" in message or "ZX431" in message for message in active_alert_messages))

    def test_replacement_supersedes_failed_and_sending_stale_alerts(self):
        active = build(
            [fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")],
            period_from="01Aug37",
            period_to="31Aug37",
        )
        replacement = build(
            [fly("14Aug37", "ZX215", "SIN-PER", "1645", "1845", "2355")],
            period_from="01Aug37",
            period_to="31Aug37",
        )
        self.apply(active, "file-a")
        keys = [alert.event_key for alert in self.database.pending_alerts()]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE alerts SET status='failed',retry_not_before='2037-08-01T01:00:00Z' "
                "WHERE event_key=?",
                (keys[0],),
            )
            connection.execute(
                "UPDATE alerts SET status='sending',attempt_count=1 WHERE event_key=?",
                (keys[1],),
            )
            connection.execute(
                "INSERT INTO delivery_attempts"
                "(event_key,schedule_generation,attempt_number,started_at,outcome) "
                "SELECT event_key,schedule_generation,1,'2037-08-01T00:00:00Z','sending' "
                "FROM alerts WHERE event_key=?",
                (keys[1],),
            )
        self.apply(replacement, "file-b")
        records = [self.database.alert_record(key) for key in keys]
        self.assertTrue(all(record["status"] == "superseded" for record in records))
        self.assertTrue(all(record["retry_not_before"] is None for record in records))
        self.assertEqual(self.database.attempt_history(keys[1])[0]["outcome"], "abandoned")

    def test_schedule_generation_advances_only_for_changed_rosters(self):
        first = build([fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")])
        same = deepcopy(first)
        changed = build([fly("14Aug37", "ZX215", "SIN-PER", "1645", "1845", "2355")])
        self.apply(first, "file-a")
        self.assertEqual(self.database.schedule_state()["schedule_generation"], 1)
        self.apply(same, "file-b")
        self.assertEqual(self.database.schedule_state()["schedule_generation"], 1)
        self.apply(changed, "file-c")
        state = self.database.schedule_state()
        self.assertEqual(state["schedule_generation"], 2)
        self.assertEqual(
            {alert.schedule_generation for alert in self.database.pending_alerts()}, {2}
        )

    def test_edit_preserves_an_unchanged_alert_already_crossing_the_network(self):
        active = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630"),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
            ]
        )
        amended = build(
            [
                fly("06Aug37", "ZX410", "SIN-TPE", "1015", "1215", "1700"),
                fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215"),
            ]
        )
        self.apply(active, "file-a")
        unchanged = next(
            alert for alert in self.database.pending_alerts() if "ZX411" in alert.message
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE alerts SET status='sending',attempt_count=1 WHERE event_key=?",
                (unchanged.event_key,),
            )
            connection.execute(
                "INSERT INTO delivery_attempts"
                "(event_key,schedule_generation,attempt_number,started_at,outcome) "
                "SELECT event_key,schedule_generation,1,'2037-08-01T00:00:00Z','sending' "
                "FROM alerts WHERE event_key=?",
                (unchanged.event_key,),
            )
        self.apply(amended, "file-b")
        record = self.database.alert_record(unchanged.event_key)
        self.assertEqual(record["status"], "sending")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(record["schedule_generation"], 1)
        self.assertEqual(
            self.database.attempt_history(unchanged.event_key)[0]["outcome"],
            "sending",
        )


if __name__ == "__main__":
    unittest.main()
