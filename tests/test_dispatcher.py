from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import UTC, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from roster.alerts import calculate_alerts
from roster.console_notifier import ConsoleNotifier
from roster.database import RosterDatabase
from roster.dispatcher import Dispatcher
from roster.models import Alert, AlertStatus, AlertType

from helpers import build, fly


NOW = datetime(2037, 8, 10, 4, 0, tzinfo=UTC)


class RecordingNotifier:
    def __init__(self, error: Exception | None = None):
        self.alerts = []
        self.error = error

    def send(self, alert, *, sent_at):
        self.alerts.append((alert, sent_at))
        if self.error:
            raise self.error
        return f"recording:{alert.event_key}"


def make_alert(
    event_key: str,
    alert_type: AlertType = AlertType.PREP_3H,
    *,
    due: datetime = NOW,
) -> Alert:
    return Alert(
        event_key=event_key,
        alert_type=alert_type,
        due_utc=due,
        message="Time to get ready\nZX421 ADL → SIN\nRPT 0810 (0640 SG)",
    )


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = RosterDatabase(Path(self.directory.name) / "roster.db")

    def tearDown(self):
        self.directory.cleanup()

    def dispatch(self, notifier=None, now=NOW):
        selected = notifier or RecordingNotifier()
        result = Dispatcher(self.database, selected, clock=lambda: now).dispatch_due()
        return selected, result

    def test_pending_alert_sends_and_records_notification(self):
        self.database.enqueue_alert(make_alert("due"), created_at=NOW - timedelta(minutes=1))
        notifier, result = self.dispatch()
        record = self.database.alert_record("due")
        self.assertEqual(result.sent, 1)
        self.assertEqual(len(notifier.alerts), 1)
        self.assertEqual(record["status"], "sent")
        self.assertEqual(record["notification_id"], "recording:due")
        self.assertEqual(record["attempt_count"], 1)

    def test_future_alert_does_not_send(self):
        self.database.enqueue_alert(make_alert("future", due=NOW + timedelta(seconds=1)))
        notifier, result = self.dispatch()
        self.assertEqual(result.claimed, 0)
        self.assertFalse(notifier.alerts)
        self.assertEqual(self.database.alert_record("future")["status"], "pending")

    def test_sent_alert_does_not_resend(self):
        self.database.enqueue_alert(make_alert("sent"))
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            connection.execute("UPDATE alerts SET status='sent' WHERE event_key='sent'")
        notifier, result = self.dispatch()
        self.assertEqual(result.claimed, 0)
        self.assertFalse(notifier.alerts)

    def test_recent_sending_alert_does_not_resend(self):
        self.database.enqueue_alert(make_alert("sending"), created_at=NOW)
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            connection.execute(
                "UPDATE alerts SET status='sending',updated_at=? WHERE event_key='sending'",
                (NOW.isoformat().replace("+00:00", "Z"),),
            )
        notifier, result = self.dispatch()
        self.assertEqual(result.claimed, 0)
        self.assertFalse(notifier.alerts)
        self.assertEqual(self.database.alert_record("sending")["status"], "sending")

    def test_failed_alert_records_error(self):
        self.database.enqueue_alert(make_alert("failure"))
        notifier = RecordingNotifier(RuntimeError("synthetic notifier failure"))
        _, result = self.dispatch(notifier)
        record = self.database.alert_record("failure")
        self.assertEqual(result.failed, 1)
        self.assertEqual(record["status"], "failed")
        self.assertIn("synthetic notifier failure", record["last_error"])
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(
            self.database.attempt_history("failure")[0]["outcome"], "failed"
        )

    def test_duplicate_dispatch_invocation_is_safe(self):
        self.database.enqueue_alert(make_alert("once"))
        notifier = RecordingNotifier()
        first = Dispatcher(self.database, notifier, clock=lambda: NOW).dispatch_due()
        second = Dispatcher(self.database, notifier, clock=lambda: NOW).dispatch_due()
        self.assertEqual((first.sent, second.sent), (1, 0))
        self.assertEqual(len(notifier.alerts), 1)

    def test_last_dispatch_is_recorded_even_when_nothing_is_due(self):
        self.dispatch()
        self.assertEqual(self.database.last_dispatch(), NOW)

    def test_console_notifier_uses_singapore_display(self):
        stream = StringIO()
        notifier = ConsoleNotifier(stream)
        notification_id = notifier.send(
            make_alert("console"),
            sent_at=datetime(2037, 8, 29, 3, 40, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual(
            stream.getvalue(),
            "[2037-08-29 03:40 SG]\n\nTime to get ready\nZX421 ADL → SIN\nRPT 0810 (0640 SG)\n",
        )
        self.assertEqual(notification_id, "console:console")

    def test_utc_comparison_accepts_singapore_now(self):
        self.database.enqueue_alert(
            make_alert("utc", due=datetime(2037, 8, 10, 3, 59, tzinfo=UTC))
        )
        singapore_now = datetime(2037, 8, 10, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        notifier, result = self.dispatch(now=singapore_now)
        self.assertEqual(result.sent, 1)
        self.assertEqual(len(notifier.alerts), 1)

    def test_overseas_event_due_calculation_remains_utc_based(self):
        roster = build([fly("29Aug37", "ZX421", "ADL-SIN", "0810", "0910", "1450")])
        alerts = calculate_alerts(roster, now=datetime(2037, 8, 1, tzinfo=UTC))
        three_hour = next(alert for alert in alerts if alert.alert_type == AlertType.PREP_3H)
        self.assertEqual(three_hour.due_utc, datetime(2037, 8, 28, 19, 40, tzinfo=UTC))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = RosterDatabase(Path(self.directory.name) / "roster.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_within_grace_period_sends(self):
        self.database.enqueue_alert(
            make_alert(
                "within",
                AlertType.PREP_12H,
                due=NOW - timedelta(minutes=59),
            )
        )
        notifier = RecordingNotifier()
        result = Dispatcher(self.database, notifier, clock=lambda: NOW).dispatch_due()
        self.assertEqual(result.sent, 1)
        self.assertEqual(self.database.alert_record("within")["status"], "sent")

    def test_outside_grace_period_marks_missed(self):
        self.database.enqueue_alert(
            make_alert(
                "stale",
                AlertType.PREP_12H,
                due=NOW - timedelta(minutes=61),
            )
        )
        notifier = RecordingNotifier()
        result = Dispatcher(self.database, notifier, clock=lambda: NOW).dispatch_due()
        self.assertEqual(result.missed, 1)
        self.assertFalse(notifier.alerts)
        self.assertEqual(self.database.alert_record("stale")["status"], "missed")

    def test_each_alert_type_uses_its_own_grace_period(self):
        cases = (
            (AlertType.PREP_12H, 59, True),
            (AlertType.PREP_12H, 61, False),
            (AlertType.PREP_3H, 29, True),
            (AlertType.PREP_3H, 31, False),
            (AlertType.LANDING_1H, 19, True),
            (AlertType.LANDING_1H, 21, False),
        )
        for index, (alert_type, minutes, should_send) in enumerate(cases):
            key = f"grace-{index}"
            self.database.enqueue_alert(
                make_alert(key, alert_type, due=NOW - timedelta(minutes=minutes))
            )
        notifier = RecordingNotifier()
        result = Dispatcher(self.database, notifier, clock=lambda: NOW).dispatch_due()
        self.assertEqual(result.sent, 3)
        self.assertEqual(result.missed, 3)
        for index, (_, _, should_send) in enumerate(cases):
            expected = "sent" if should_send else "missed"
            self.assertEqual(self.database.alert_record(f"grace-{index}")["status"], expected)

    def test_failed_alert_is_retried_within_grace(self):
        self.database.enqueue_alert(make_alert("retry", due=NOW - timedelta(minutes=5)))
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            connection.execute(
                "UPDATE alerts SET status='failed',last_error='first failure',retry_not_before=? "
                "WHERE event_key='retry'",
                ((NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
            )
        notifier = RecordingNotifier()
        result = Dispatcher(self.database, notifier, clock=lambda: NOW).dispatch_due()
        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.sent, 1)
        self.assertEqual(self.database.alert_record("retry")["attempt_count"], 1)

    def test_failure_is_not_retried_before_backoff(self):
        self.database.enqueue_alert(make_alert("backoff"))
        failing = RecordingNotifier(RuntimeError("temporary"))
        first = Dispatcher(self.database, failing, clock=lambda: NOW).dispatch_due()
        waiting = Dispatcher(
            self.database, RecordingNotifier(), clock=lambda: NOW + timedelta(minutes=4)
        ).dispatch_due()
        record = self.database.alert_record("backoff")
        self.assertEqual(first.failed, 1)
        self.assertEqual(waiting.claimed, 0)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["retry_not_before"], "2037-08-10T04:05:00Z")

    def test_retry_is_bounded_to_three_attempts_with_history(self):
        self.database.enqueue_alert(make_alert("bounded"))
        moments = (NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=20))
        for moment in moments:
            result = Dispatcher(
                self.database,
                RecordingNotifier(RuntimeError("temporary")),
                clock=lambda value=moment: value,
            ).dispatch_due()
            self.assertEqual(result.failed, 1)
        terminal = self.database.alert_record("bounded")
        self.assertEqual(terminal["attempt_count"], 3)
        self.assertIsNone(terminal["retry_not_before"])
        self.assertEqual(
            [item["outcome"] for item in self.database.attempt_history("bounded")],
            ["failed", "failed", "failed"],
        )
        later = Dispatcher(
            self.database, RecordingNotifier(), clock=lambda: NOW + timedelta(minutes=25)
        ).dispatch_due()
        self.assertEqual(later.claimed, 0)


class DatabaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = RosterDatabase(Path(self.directory.name) / "roster.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_atomic_claim_allows_only_one_concurrent_owner(self):
        self.database.enqueue_alert(make_alert("atomic"))
        barrier = threading.Barrier(2)
        claimed = []
        errors = []

        def claim():
            try:
                barrier.wait()
                claimed.append(self.database.claim_due_alerts(NOW))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(sorted(len(batch) for batch in claimed), [0, 1])
        self.assertEqual(self.database.alert_record("atomic")["status"], "sending")

    def test_transaction_rollback_restores_pending_state(self):
        self.database.enqueue_alert(make_alert("rollback"))
        with closing(self.database._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE alerts SET status='sending' WHERE event_key='rollback'")
            connection.rollback()
        self.assertEqual(self.database.alert_record("rollback")["status"], "pending")

    def test_duplicate_event_key_is_rejected(self):
        self.database.enqueue_alert(make_alert("duplicate"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.enqueue_alert(make_alert("duplicate"))


if __name__ == "__main__":
    unittest.main()
