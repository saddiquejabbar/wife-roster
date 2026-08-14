from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roster.cli import main
from roster.database import RosterDatabase
from roster.dispatcher import Dispatcher
from roster.models import Alert, AlertType
from roster.telegram_notifier import (
    TelegramConfig,
    TelegramConfigurationError,
    TelegramDeliveryError,
    TelegramNotifier,
)


NOW = datetime(2037, 8, 10, 4, 0, tzinfo=UTC)
TOKEN = "test-telegram-token"
MESSAGE = "Time to get ready\nZX421 ADL → SIN\nRPT 0810 (0640 SG)"


class FakeTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, payload, *, timeout):
        self.calls.append((url, dict(payload), timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def alert(key="telegram-alert", *, due=NOW):
    return Alert(
        event_key=key,
        alert_type=AlertType.PREP_3H,
        due_utc=due,
        message=MESSAGE,
    )


class TelegramNotifierTests(unittest.TestCase):
    def notifier(self, outcomes, *, chat_ids=("test-chat-a",), sleeper=None):
        transport = FakeTransport(outcomes)
        sleeps = []
        notifier = TelegramNotifier(
            TelegramConfig(TOKEN, chat_ids),
            transport=transport,
            sleeper=sleeper or sleeps.append,
        )
        return notifier, transport, sleeps

    def test_correct_api_payload_and_exact_message_body(self):
        notifier, transport, _ = self.notifier(
            [{"ok": True, "result": {"message_id": 41}}]
        )
        notification_id = notifier.send(alert(), sent_at=NOW)
        self.assertEqual(notification_id, "telegram:41")
        self.assertEqual(len(transport.calls), 1)
        url, payload, timeout = transport.calls[0]
        self.assertEqual(url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(payload, {"chat_id": "test-chat-a", "text": MESSAGE})
        self.assertEqual(timeout, 15.0)

    def test_multiple_recipients_are_supported(self):
        notifier, transport, _ = self.notifier(
            [
                {"ok": True, "result": {"message_id": 41}},
                {"ok": True, "result": {"message_id": 52}},
            ],
            chat_ids=("test-chat-a", "test-chat-b"),
        )
        self.assertEqual(notifier.send(alert(), sent_at=NOW), "telegram:41,52")
        self.assertEqual(
            [call[1]["chat_id"] for call in transport.calls],
            ["test-chat-a", "test-chat-b"],
        )

    def test_failed_response_raises_controlled_error_without_retry(self):
        notifier, transport, sleeps = self.notifier(
            [{"ok": False, "error_code": 400, "description": f"bad {TOKEN}"}]
        )
        with self.assertRaisesRegex(TelegramDeliveryError, "delivery was rejected") as raised:
            notifier.send(alert(), sent_at=NOW)
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(sleeps)
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_timeout_retries_at_10_and_30_seconds_then_fails(self):
        notifier, transport, sleeps = self.notifier(
            [TimeoutError(TOKEN), TimeoutError(TOKEN), TimeoutError(TOKEN)]
        )
        with self.assertRaisesRegex(TelegramDeliveryError, "after 3 attempts") as raised:
            notifier.send(alert(), sent_at=NOW)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleeps, [10.0, 30.0])
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_retryable_api_failure_can_recover(self):
        notifier, transport, sleeps = self.notifier(
            [
                {"ok": False, "error_code": 429, "description": "rate limited"},
                {"ok": True, "result": {"message_id": 91}},
            ]
        )
        self.assertEqual(notifier.send(alert(), sent_at=NOW), "telegram:91")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeps, [10.0])

    def test_missing_token_and_chat_ids_are_rejected(self):
        with self.assertRaisesRegex(TelegramConfigurationError, "BOT_TOKEN"):
            TelegramConfig.from_environment(
                {"TELEGRAM_CHAT_IDS": "test-chat-a"}, env_file="/missing"
            )
        with self.assertRaisesRegex(TelegramConfigurationError, "CHAT_IDS"):
            TelegramConfig.from_environment({"TELEGRAM_BOT_TOKEN": TOKEN}, env_file="/missing")

    def test_configuration_reads_external_env_file_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                f"TELEGRAM_BOT_TOKEN='{TOKEN}'\n"
                "TELEGRAM_CHAT_IDS=test-chat-a, test-chat-b,test-chat-a\n"
                "IGNORED=x\n",
                encoding="utf-8",
            )
            config = TelegramConfig.from_environment({}, env_file=path)
        self.assertEqual(config.bot_token, TOKEN)
        self.assertEqual(config.chat_ids, ("test-chat-a", "test-chat-b"))

    def test_token_never_appears_in_output_or_exception(self):
        notifier, _, _ = self.notifier(
            [RuntimeError(TOKEN), RuntimeError(TOKEN), RuntimeError(TOKEN)]
        )
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(TelegramDeliveryError) as raised:
                notifier.send(alert(), sent_at=NOW)
        rendered = stdout.getvalue() + stderr.getvalue() + str(raised.exception)
        self.assertNotIn(TOKEN, rendered)


class TelegramDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = RosterDatabase(Path(self.directory.name) / "roster.db")

    def tearDown(self):
        self.directory.cleanup()

    def notifier(self, outcomes):
        return TelegramNotifier(
            TelegramConfig(TOKEN, ("test-chat-a",)),
            transport=FakeTransport(outcomes),
            sleeper=lambda delay: None,
        )

    def test_telegram_success_marks_sent(self):
        self.database.enqueue_alert(alert("success", due=NOW - timedelta(minutes=1)))
        result = Dispatcher(
            self.database,
            self.notifier([{"ok": True, "result": {"message_id": 77}}]),
            clock=lambda: NOW,
        ).dispatch_due()
        record = self.database.alert_record("success")
        self.assertEqual(result.sent, 1)
        self.assertEqual(record["status"], "sent")
        self.assertEqual(record["notification_id"], "telegram:77")

    def test_telegram_failure_does_not_mark_sent(self):
        self.database.enqueue_alert(alert("failure", due=NOW - timedelta(minutes=1)))
        result = Dispatcher(
            self.database,
            self.notifier([{"ok": False, "error_code": 400}]),
            clock=lambda: NOW,
        ).dispatch_due()
        record = self.database.alert_record("failure")
        self.assertEqual(result.failed, 1)
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["sent_at"])

    def test_duplicate_dispatch_does_not_resend(self):
        self.database.enqueue_alert(alert("once", due=NOW - timedelta(minutes=1)))
        transport = FakeTransport([{"ok": True, "result": {"message_id": 88}}])
        notifier = TelegramNotifier(
            TelegramConfig(TOKEN, ("1001",)),
            transport=transport,
            sleeper=lambda delay: None,
        )
        first = Dispatcher(self.database, notifier, clock=lambda: NOW).dispatch_due()
        second = Dispatcher(self.database, notifier, clock=lambda: NOW).dispatch_due()
        self.assertEqual((first.sent, second.sent), (1, 0))
        self.assertEqual(len(transport.calls), 1)


class TelegramCliTests(unittest.TestCase):
    def test_telegram_test_does_not_create_database_alert(self):
        sent = []

        class FakeNotifier:
            def send(self, candidate, *, sent_at):
                sent.append(candidate)
                return "telegram:1"

        output = StringIO()
        with tempfile.TemporaryDirectory() as directory, patch(
            "roster.cli.TelegramNotifier.from_environment",
            return_value=FakeNotifier(),
        ), redirect_stdout(output):
            database = Path(directory) / "must-not-exist.db"
            result = main(["--db", str(database), "telegram-test"])
            self.assertFalse(database.exists())
        self.assertEqual(result, 0)
        self.assertEqual(sent[0].message, "Telegram test successful")
        self.assertEqual(output.getvalue(), "Telegram test successful\n")

    def test_telegram_test_missing_configuration_is_safe(self):
        output = StringIO()
        with patch(
            "roster.cli.TelegramNotifier.from_environment",
            side_effect=TelegramConfigurationError("TELEGRAM_BOT_TOKEN is required"),
        ), redirect_stdout(output):
            result = main(["telegram-test"])
        self.assertEqual(result, 2)
        self.assertNotIn(TOKEN, output.getvalue())


if __name__ == "__main__":
    unittest.main()
