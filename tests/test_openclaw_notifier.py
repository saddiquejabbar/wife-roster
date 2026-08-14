from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from roster.models import Alert, AlertType
from roster.openclaw_notifier import (
    DEFAULT_TIMEOUT_SECONDS,
    OpenClawConfig,
    OpenClawConfigurationError,
    OpenClawDeliveryError,
    OpenClawNotifier,
)


NOW = datetime(2037, 8, 10, 4, 0, tzinfo=UTC)
MESSAGE = "Time to get ready\nZX421 ADL → SIN\nRPT 0810 (0640 SG)"
ACCOUNT = "synthetic-private-account"
TARGET = "test-telegram-target"
SENSITIVE = "synthetic-sensitive-value"


def alert() -> Alert:
    return Alert(
        event_key="openclaw-alert",
        alert_type=AlertType.PREP_3H,
        due_utc=NOW,
        message=MESSAGE,
    )


class FakeRunner:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = []

    def __call__(self, arguments, **kwargs):
        self.calls.append((list(arguments), dict(kwargs)))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class OpenClawNotifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.executable = Path(self.directory.name) / "openclaw"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(0o700)

    def tearDown(self):
        self.directory.cleanup()

    def config(self, **overrides) -> OpenClawConfig:
        values = {
            "executable": self.executable,
            "account": ACCOUNT,
            "target": TARGET,
            "timeout_seconds": 12.5,
        }
        values.update(overrides)
        return OpenClawConfig(**values)

    def notifier(self, outcome):
        runner = FakeRunner(outcome)
        return OpenClawNotifier(self.config(), runner=runner), runner

    def test_default_timeout_allows_confirmed_production_delivery_window(self):
        config = OpenClawConfig(self.executable, ACCOUNT, TARGET)
        self.assertEqual(config.timeout_seconds, DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(config.timeout_seconds, 30.0)

    def test_success_uses_direct_command_and_exact_alert_body(self):
        completed = subprocess.CompletedProcess(
            [str(self.executable)],
            0,
            stdout=json.dumps(
                {
                    "action": "send",
                    "channel": "telegram",
                    "dryRun": False,
                    "messageId": 481,
                }
            ),
            stderr="",
        )
        notifier, runner = self.notifier(completed)

        notification_id = notifier.send(alert(), sent_at=NOW)

        self.assertEqual(notification_id, "openclaw:telegram:481")
        self.assertEqual(len(runner.calls), 1)
        arguments, options = runner.calls[0]
        self.assertEqual(
            arguments,
            [
                str(self.executable),
                "message",
                "send",
                "--channel",
                "telegram",
                "--account",
                ACCOUNT,
                "--target",
                TARGET,
                "--message",
                MESSAGE,
                "--json",
            ],
        )
        self.assertEqual(arguments[arguments.index("--message") + 1], MESSAGE)
        self.assertEqual(
            options,
            {
                "stdin": subprocess.DEVNULL,
                "capture_output": True,
                "text": True,
                "timeout": 12.5,
                "shell": False,
                "check": False,
            },
        )
        self.assertEqual(arguments[1:3], ["message", "send"])
        self.assertNotIn("agent", arguments)
        self.assertNotIn("model", arguments)

    def test_numeric_string_message_id_is_accepted(self):
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                '{"action":"send","channel":"telegram",'
                '"dryRun":false,"messageId":"902"}'
            ),
            stderr="",
        )
        notifier, _ = self.notifier(completed)
        self.assertEqual(
            notifier.send(alert(), sent_at=NOW),
            "openclaw:telegram:902",
        )

    def test_timeout_is_controlled_and_does_not_leak_configuration(self):
        timeout = subprocess.TimeoutExpired(
            cmd=[str(self.executable), ACCOUNT, TARGET, SENSITIVE],
            timeout=12.5,
            output=SENSITIVE,
        )
        notifier, _ = self.notifier(timeout)
        with self.assertRaisesRegex(OpenClawDeliveryError, "timed out") as raised:
            notifier.send(alert(), sent_at=NOW)
        rendered = str(raised.exception)
        for private_value in (ACCOUNT, TARGET, SENSITIVE, MESSAGE):
            self.assertNotIn(private_value, rendered)

    def test_nonzero_exit_is_controlled_and_does_not_leak_output(self):
        completed = subprocess.CompletedProcess(
            [], 7, stdout=f"bad {TARGET}", stderr=f"bad {SENSITIVE}"
        )
        notifier, _ = self.notifier(completed)
        with self.assertRaisesRegex(OpenClawDeliveryError, "command failed") as raised:
            notifier.send(alert(), sent_at=NOW)
        rendered = str(raised.exception)
        for private_value in (ACCOUNT, TARGET, SENSITIVE, MESSAGE):
            self.assertNotIn(private_value, rendered)

    def test_malformed_json_is_rejected_without_echoing_response(self):
        completed = subprocess.CompletedProcess(
            [], 0, stdout=f"not json {SENSITIVE}", stderr=""
        )
        notifier, _ = self.notifier(completed)
        with self.assertRaisesRegex(OpenClawDeliveryError, "invalid response") as raised:
            notifier.send(alert(), sent_at=NOW)
        self.assertNotIn(SENSITIVE, str(raised.exception))

    def test_missing_or_invalid_message_id_is_rejected(self):
        confirmed = {
            "action": "send",
            "channel": "telegram",
            "dryRun": False,
        }
        responses = (
            confirmed,
            {**confirmed, "messageId": None},
            {**confirmed, "messageId": True},
            {**confirmed, "messageId": 0},
            {**confirmed, "messageId": "not-a-number"},
            {**confirmed, "result": {"messageId": 7}},
        )
        for response in responses:
            with self.subTest(response=response):
                completed = subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(response), stderr=""
                )
                notifier, _ = self.notifier(completed)
                with self.assertRaisesRegex(
                    OpenClawDeliveryError, "did not confirm delivery"
                ):
                    notifier.send(alert(), sent_at=NOW)

    def test_wrong_channel_or_dry_run_is_not_confirmed_delivery(self):
        responses = (
            {
                "action": "send",
                "channel": "discord",
                "dryRun": False,
                "messageId": 7,
            },
            {
                "action": "send",
                "channel": "telegram",
                "dryRun": True,
                "messageId": 7,
            },
            {
                "action": "inspect",
                "channel": "telegram",
                "dryRun": False,
                "messageId": 7,
            },
        )
        for response in responses:
            with self.subTest(response=response):
                completed = subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(response), stderr=""
                )
                notifier, _ = self.notifier(completed)
                with self.assertRaisesRegex(
                    OpenClawDeliveryError, "did not confirm delivery"
                ):
                    notifier.send(alert(), sent_at=NOW)

    def test_missing_executable_is_rejected_without_leaking_configuration(self):
        missing = Path(self.directory.name) / SENSITIVE
        with self.assertRaises(OpenClawConfigurationError) as raised:
            OpenClawConfig(missing, ACCOUNT, TARGET)
        rendered = str(raised.exception)
        for private_value in (ACCOUNT, TARGET, SENSITIVE):
            self.assertNotIn(private_value, rendered)

    def test_non_executable_file_is_rejected(self):
        self.executable.chmod(0o600)
        with self.assertRaisesRegex(OpenClawConfigurationError, "executable file"):
            self.config()

    def test_missing_account_is_rejected_without_leaking_target(self):
        with self.assertRaisesRegex(OpenClawConfigurationError, "ACCOUNT") as raised:
            self.config(account="  ")
        self.assertNotIn(TARGET, str(raised.exception))

    def test_missing_target_is_rejected_without_leaking_account(self):
        with self.assertRaisesRegex(OpenClawConfigurationError, "TARGET") as raised:
            self.config(target="  ")
        self.assertNotIn(ACCOUNT, str(raised.exception))

    def test_explicit_blank_environment_does_not_fall_back_to_env_file(self):
        env_file = Path(self.directory.name) / ".env"
        env_file.write_text(
            "WIFE_ROSTER_OPENCLAW_ACCOUNT=default\n"
            "WIFE_ROSTER_OPENCLAW_TARGET=stored-target\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(OpenClawConfigurationError, "ACCOUNT"):
            OpenClawConfig.from_environment(
                {
                    "WIFE_ROSTER_OPENCLAW_BIN": str(self.executable),
                    "WIFE_ROSTER_OPENCLAW_ACCOUNT": "",
                },
                env_file=env_file,
            )

    def test_environment_mapping_controls_default_runtime_env_path(self):
        deploy_root = Path(self.directory.name) / "runtime-root"
        deploy_root.mkdir()
        (deploy_root / ".env").write_text(
            f"WIFE_ROSTER_OPENCLAW_BIN={self.executable}\n"
            "WIFE_ROSTER_OPENCLAW_ACCOUNT=default\n"
            "WIFE_ROSTER_OPENCLAW_TARGET=stored-target\n",
            encoding="utf-8",
        )
        config = OpenClawConfig.from_environment(
            {"WIFE_ROSTER_DEPLOY_ROOT": str(deploy_root)}
        )
        self.assertEqual(config.target, "stored-target")

    def test_relative_executable_is_rejected(self):
        with self.assertRaisesRegex(OpenClawConfigurationError, "absolute"):
            OpenClawConfig(Path("openclaw"), ACCOUNT, TARGET)

    def test_os_error_is_controlled(self):
        notifier, _ = self.notifier(OSError(SENSITIVE))
        with self.assertRaisesRegex(OpenClawDeliveryError, "could not run") as raised:
            notifier.send(alert(), sent_at=NOW)
        self.assertNotIn(SENSITIVE, str(raised.exception))

    def test_nul_input_is_rejected_before_subprocess(self):
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        notifier, runner = self.notifier(completed)
        candidate = Alert(
            event_key="nul-alert",
            alert_type=AlertType.PREP_3H,
            due_utc=NOW,
            message=f"{MESSAGE}\x00{SENSITIVE}",
        )
        with self.assertRaisesRegex(OpenClawDeliveryError, "input was invalid") as raised:
            notifier.send(candidate, sent_at=NOW)
        self.assertFalse(runner.calls)
        self.assertNotIn(SENSITIVE, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
