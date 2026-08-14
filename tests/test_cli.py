from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roster.cli import NotifierSelectionError, _notifier_from_environment, main
from roster.console_notifier import ConsoleNotifier
from roster.database import RosterDatabase
from roster.models import Alert, AlertType
from roster.openclaw_notifier import OpenClawDeliveryError, OpenClawNotifier
from roster.telegram_notifier import TelegramNotifier


NOW = datetime(2037, 8, 10, 4, 0, tzinfo=UTC)


class CliTests(unittest.TestCase):
    def test_synthetic_dry_run(self):
        fixture = Path(__file__).parent / "fixtures" / "synthetic_roster.json"
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "--db",
                        str(Path(directory) / "not-created.db"),
                        "ingest",
                        str(fixture),
                        "--dry-run",
                        "--now",
                        "2037-08-01T00:00:00Z",
                        "--verbose",
                    ]
                )
        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("Singapore → Taipei, Taiwan", rendered)
        self.assertIn("Adelaide, Australia → Singapore", rendered)
        self.assertIn("Proposed alerts: 12", rendered)
        self.assertNotIn("NEEDS REVIEW", rendered)

    def test_ingest_cannot_activate_in_stage_one(self):
        fixture = Path(__file__).parent / "fixtures" / "synthetic_roster.json"
        output = StringIO()
        with redirect_stdout(output):
            result = main(["ingest", str(fixture)])
        self.assertEqual(result, 2)
        self.assertIn("requires --dry-run", output.getvalue())


class NotifierSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.temporary = Path(self.directory.name)
        self.missing_env = self.temporary / "missing.env"
        self.executable = self.temporary / "openclaw"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(0o700)

    def tearDown(self):
        self.directory.cleanup()

    def test_no_configuration_uses_console(self):
        notifier = _notifier_from_environment({}, env_file=self.missing_env)
        self.assertIsInstance(notifier, ConsoleNotifier)

    def test_explicit_console_uses_console(self):
        notifier = _notifier_from_environment(
            {"WIFE_ROSTER_NOTIFIER": "console"},
            env_file=self.missing_env,
        )
        self.assertIsInstance(notifier, ConsoleNotifier)

    def test_explicit_openclaw_uses_openclaw(self):
        notifier = _notifier_from_environment(
            {
                "WIFE_ROSTER_NOTIFIER": "openclaw",
                "WIFE_ROSTER_OPENCLAW_BIN": str(self.executable),
                "WIFE_ROSTER_OPENCLAW_ACCOUNT": "default",
                "WIFE_ROSTER_OPENCLAW_TARGET": "synthetic-target",
            },
            env_file=self.missing_env,
        )
        self.assertIsInstance(notifier, OpenClawNotifier)
        self.assertEqual(notifier.config.executable, self.executable)

    def test_explicit_telegram_uses_telegram(self):
        notifier = _notifier_from_environment(
            {
                "WIFE_ROSTER_NOTIFIER": "telegram",
                "TELEGRAM_BOT_TOKEN": "test-telegram-token",
                "TELEGRAM_CHAT_IDS": "1001",
            },
            env_file=self.missing_env,
        )
        self.assertIsInstance(notifier, TelegramNotifier)

    def test_credentials_do_not_select_a_notifier_automatically(self):
        notifier = _notifier_from_environment(
            {
                "TELEGRAM_BOT_TOKEN": "test-telegram-token",
                "TELEGRAM_CHAT_IDS": "1001",
                "WIFE_ROSTER_OPENCLAW_BIN": str(self.executable),
                "WIFE_ROSTER_OPENCLAW_ACCOUNT": "default",
                "WIFE_ROSTER_OPENCLAW_TARGET": "synthetic-target",
            },
            env_file=self.missing_env,
        )
        self.assertIsInstance(notifier, ConsoleNotifier)

    def test_notifier_selection_can_be_read_from_runtime_env(self):
        env_file = self.temporary / ".env"
        env_file.write_text("WIFE_ROSTER_NOTIFIER='console'\n", encoding="utf-8")
        notifier = _notifier_from_environment({}, env_file=env_file)
        self.assertIsInstance(notifier, ConsoleNotifier)

    def test_explicit_blank_selector_overrides_runtime_env_with_console(self):
        env_file = self.temporary / ".env"
        env_file.write_text("WIFE_ROSTER_NOTIFIER=openclaw\n", encoding="utf-8")
        notifier = _notifier_from_environment(
            {"WIFE_ROSTER_NOTIFIER": ""},
            env_file=env_file,
        )
        self.assertIsInstance(notifier, ConsoleNotifier)

    def test_invalid_value_fails_closed(self):
        with self.assertRaisesRegex(NotifierSelectionError, "console, openclaw, telegram"):
            _notifier_from_environment(
                {"WIFE_ROSTER_NOTIFIER": "automatic"},
                env_file=self.missing_env,
            )

    def test_invalid_value_is_a_controlled_cli_failure(self):
        output = StringIO()
        database = self.temporary / "not-created.db"
        with patch.dict(
            "os.environ",
            {
                "WIFE_ROSTER_NOTIFIER": "automatic",
                "WIFE_ROSTER_DEPLOY_ROOT": str(self.temporary),
            },
            clear=True,
        ), redirect_stdout(output):
            result = main(
                [
                    "--db",
                    str(database),
                    "dispatch-due",
                    "--now",
                    NOW.isoformat(),
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("NOTIFIER CONFIGURATION WARNING", output.getvalue())
        self.assertFalse(database.exists())

    def test_invalid_runtime_configuration_uses_bounded_retry_for_due_alert(self):
        database_path = self.temporary / "existing.db"
        database = RosterDatabase(database_path)
        database.enqueue_alert(
            Alert("config-retry", AlertType.PREP_3H, NOW, "configured alert")
        )
        output = StringIO()
        with patch.dict(
            "os.environ",
            {
                "WIFE_ROSTER_NOTIFIER": "automatic",
                "WIFE_ROSTER_DEPLOY_ROOT": str(self.temporary),
            },
            clear=True,
        ), redirect_stdout(output):
            result = main(
                ["--db", str(database_path), "dispatch-due", "--now", NOW.isoformat()]
            )
        record = database.alert_record("config-retry")
        self.assertEqual(result, 2)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(record["retry_not_before"], "2037-08-10T04:05:00Z")
        self.assertIn("entered bounded retry", output.getvalue())


class DispatchExitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "roster.db"
        self.database = RosterDatabase(self.database_path)

    def tearDown(self):
        self.directory.cleanup()

    def enqueue(self, key: str) -> None:
        self.database.enqueue_alert(
            Alert(
                event_key=key,
                alert_type=AlertType.PREP_3H,
                due_utc=NOW,
                message="Time to get ready\nZX421 ADL → SIN\nRPT 0810 (0640 SG)",
            )
        )

    def run_dispatch(self, notifier) -> tuple[int, str]:
        output = StringIO()
        with patch("roster.cli._notifier_from_environment", return_value=notifier), redirect_stdout(
            output
        ):
            result = main(
                [
                    "--db",
                    str(self.database_path),
                    "dispatch-due",
                    "--now",
                    NOW.isoformat(),
                ]
            )
        return result, output.getvalue()

    def test_controlled_delivery_failure_is_recorded_and_exits_cleanly(self):
        self.enqueue("controlled")

        class ControlledFailureNotifier:
            def send(self, alert, *, sent_at):
                raise OpenClawDeliveryError("controlled delivery failure")

        result, output = self.run_dispatch(ControlledFailureNotifier())
        record = self.database.alert_record("controlled")
        self.assertEqual(result, 0)
        self.assertEqual(record["status"], "failed")
        self.assertIn("controlled delivery failure", record["last_error"])
        self.assertIn("0 sent, 1 failed", output)

    def test_unexpected_notifier_failure_is_recorded_but_remains_nonzero(self):
        self.enqueue("unexpected-notifier")
        sensitive = "unexpected-sensitive-detail"

        class BrokenNotifier:
            def send(self, alert, *, sent_at):
                raise RuntimeError(sensitive)

        result, _ = self.run_dispatch(BrokenNotifier())
        record = self.database.alert_record("unexpected-notifier")
        self.assertEqual(result, 1)
        self.assertEqual(record["status"], "failed")
        self.assertIn("unexpected notifier failure", record["last_error"])
        self.assertNotIn(sensitive, record["last_error"])

    def test_unexpected_program_failure_remains_nonzero(self):
        output = StringIO()
        with patch(
            "roster.cli._notifier_from_environment",
            return_value=ConsoleNotifier(),
        ), patch(
            "roster.cli.Dispatcher.dispatch_due",
            side_effect=RuntimeError("synthetic program failure"),
        ), redirect_stdout(output):
            result = main(
                [
                    "--db",
                    str(self.database_path),
                    "dispatch-due",
                    "--now",
                    NOW.isoformat(),
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("unexpected program failure", output.getvalue())


if __name__ == "__main__":
    unittest.main()
