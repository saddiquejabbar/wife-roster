from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from roster.cli import main
from roster.database import RosterDatabase
from roster.launchd_scheduler import (
    DISPATCHER_LABEL,
    LEGACY_LABEL,
    PLANNER_LABEL,
    PLIST_FILENAME,
    LaunchdScheduler,
    SchedulerError,
)
from roster.models import Alert, AlertType
from roster.planner import SchedulePlanner


class FakeLaunchctl:
    def __init__(self):
        self.loaded: set[str] = set()
        self.commands = []
        self.print_output = ""

    def __call__(self, arguments, **kwargs):
        self.commands.append(tuple(arguments))
        action = arguments[1]
        if action == "print":
            label = arguments[2].rsplit("/", 1)[-1]
            return subprocess.CompletedProcess(
                arguments, 0 if label in self.loaded else 113, self.print_output, ""
            )
        if action == "bootstrap":
            value = plistlib.loads(Path(arguments[3]).read_bytes())
            self.loaded.add(value["Label"])
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if action == "bootout":
            self.loaded.discard(arguments[2].rsplit("/", 1)[-1])
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(arguments, 1, "", "unexpected command")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "src").mkdir()
        self.plist_path = self.root / "LaunchAgents" / PLIST_FILENAME
        self.database_path = self.root / "runtime" / "private" / "roster.db"
        self.launchctl = FakeLaunchctl()
        self.scheduler = LaunchdScheduler(
            database_path=self.database_path,
            workflow_dir=self.root,
            plist_path=self.plist_path,
            command=["/opt/wife-roster/bin/roster"],
            uid=501,
            runner=self.launchctl,
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_planner_is_watch_driven_and_never_polls(self):
        value = plistlib.loads(self.scheduler.generate_planner_plist())
        self.assertEqual(value["Label"], PLANNER_LABEL)
        self.assertEqual(value["ProgramArguments"], ["/opt/wife-roster/bin/roster", "plan-next"])
        self.assertEqual(value["WatchPaths"], [str(self.scheduler.signal_path)])
        self.assertTrue(value["RunAtLoad"])
        self.assertNotIn("StartInterval", value)
        self.assertNotIn("StartCalendarInterval", value)
        self.assertEqual(value["Umask"], 0o077)

    def test_dispatcher_has_exactly_one_calendar_wake(self):
        due = datetime(2037, 8, 29, 3, 40, tzinfo=UTC)
        value = plistlib.loads(
            self.scheduler.generate_dispatcher_plist(due, generation=7)
        )
        self.assertEqual(value["Label"], DISPATCHER_LABEL)
        self.assertEqual(value["ProgramArguments"][-1], "dispatch-due")
        self.assertEqual(set(value["StartCalendarInterval"]), {"Month", "Day", "Hour", "Minute"})
        self.assertNotIn("StartInterval", value)
        self.assertNotIn("RunAtLoad", value)
        self.assertEqual(value["EnvironmentVariables"]["WIFE_ROSTER_SCHEDULE_GENERATION"], "7")
        self.assertEqual(value["EnvironmentVariables"]["WIFE_ROSTER_ARMED_FOR_UTC"], "2037-08-29T03:40:00Z")

    def test_plist_only_manages_wife_roster(self):
        rendered = self.scheduler.generate_planner_plist().decode("utf-8")
        self.assertIn("org.example.wife-roster", rendered)
        self.assertNotIn("Telegram", rendered)
        self.assertNotIn("OpenClaw", rendered)
        self.assertNotIn("pmset", rendered)
        with self.assertRaises(ValueError):
            LaunchdScheduler(
                database_path=self.database_path,
                workflow_dir=self.root,
                plist_path=self.root / "LaunchAgents" / "other-job.plist",
            )

    def test_refresh_loads_planner_and_arms_next_alert(self):
        database = RosterDatabase(self.database_path)
        database.enqueue_alert(
            Alert("next", AlertType.PREP_3H, datetime.now(UTC) + timedelta(hours=3), "next")
        )
        status = self.scheduler.refresh()
        self.assertTrue(status.planner_loaded)
        self.assertTrue(status.dispatcher_armed)
        self.assertTrue(status.valid)
        self.assertIn(PLANNER_LABEL, self.launchctl.loaded)
        self.assertIn(DISPATCHER_LABEL, self.launchctl.loaded)

    def test_refresh_removes_legacy_five_minute_agent(self):
        self.plist_path.parent.mkdir(parents=True)
        self.plist_path.write_text("legacy", encoding="utf-8")
        self.launchctl.loaded.add(LEGACY_LABEL)
        self.scheduler.refresh()
        self.assertFalse(self.plist_path.exists())
        self.assertNotIn(LEGACY_LABEL, self.launchctl.loaded)

    def test_edit_rearms_dispatcher_to_new_time_and_generation(self):
        database = RosterDatabase(self.database_path)
        first = datetime.now(UTC) + timedelta(hours=4)
        database.enqueue_alert(Alert("first", AlertType.PREP_3H, first, "first"))
        SchedulePlanner(database, self.scheduler).plan()
        first_value = plistlib.loads(self.scheduler.dispatcher_plist_path.read_bytes())
        with database._connect() as connection, connection:
            connection.execute("UPDATE alerts SET status='superseded' WHERE event_key='first'")
            connection.execute(
                "INSERT INTO workflow_state(key,value) VALUES('schedule_generation','2') "
                "ON CONFLICT(key) DO UPDATE SET value='2'"
            )
        second = datetime.now(UTC) + timedelta(hours=2)
        database.enqueue_alert(Alert("second", AlertType.PREP_3H, second, "second"))
        SchedulePlanner(database, self.scheduler).plan()
        second_value = plistlib.loads(self.scheduler.dispatcher_plist_path.read_bytes())
        self.assertNotEqual(
            first_value["EnvironmentVariables"]["WIFE_ROSTER_ARMED_FOR_UTC"],
            second_value["EnvironmentVariables"]["WIFE_ROSTER_ARMED_FOR_UTC"],
        )
        self.assertEqual(second_value["EnvironmentVariables"]["WIFE_ROSTER_SCHEDULE_GENERATION"], "2")

    def test_no_alerts_disarms_dispatcher(self):
        self.scheduler.install()
        self.scheduler.arm(datetime.now(UTC) + timedelta(hours=1), generation=1)
        database = RosterDatabase(self.database_path)
        SchedulePlanner(database, self.scheduler).plan()
        self.assertFalse(self.scheduler.dispatcher_plist_path.exists())
        self.assertNotIn(DISPATCHER_LABEL, self.launchctl.loaded)

    def test_status_reports_missing_planner(self):
        status = self.scheduler.status()
        self.assertFalse(status.installed)
        self.assertFalse(status.loaded)
        self.assertIn("planner launchd agent is missing", status.warnings)

    def test_status_detects_launchd_workflow_permission_failure(self):
        self.scheduler.install()
        self.launchctl.print_output = "runs = 2\nlast exit code = 126\n"
        status = self.scheduler.status()
        self.assertFalse(status.workflow_available)
        self.assertIn("workflow directory is unavailable", status.warnings)

    def test_refresh_does_not_reload_known_unavailable_workflow(self):
        self.scheduler.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.scheduler.log_path.write_text(
            f"/bin/sh: {self.scheduler.workflow_dir}/.venv/bin/roster: Operation not permitted\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SchedulerError, "unavailable to launchd"):
            self.scheduler.refresh()
        self.assertFalse(self.launchctl.loaded)

    def test_scheduler_refresh_command(self):
        output = StringIO()
        with patch("roster.cli._scheduler", return_value=self.scheduler), redirect_stdout(output):
            result = main(["--db", str(self.database_path), "scheduler-refresh"])
        self.assertEqual(result, 0)
        self.assertIn("planner installed: yes", output.getvalue())
        self.assertIn("planner launchd: loaded", output.getvalue())

    def test_scheduler_status_command_shows_dispatch_and_next_alert(self):
        self.scheduler.install()
        database = RosterDatabase(self.database_path)
        due = datetime(2037, 8, 29, 3, 40, tzinfo=UTC)
        database.enqueue_alert(
            Alert(
                event_key="next",
                alert_type=AlertType.PREP_3H,
                due_utc=due,
                message="Time to get ready\nZX421 ADL → SIN\nRPT 0810 (0640 SG)",
            ),
            created_at=due - timedelta(hours=1),
        )
        database.record_last_dispatch(datetime(2037, 8, 10, 4, 0, tzinfo=UTC))
        output = StringIO()
        with patch("roster.cli._scheduler", return_value=self.scheduler), redirect_stdout(output):
            result = main(["--db", str(self.database_path), "scheduler-status"])
        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("last dispatch: 2037-08-10 1200 SG", rendered)
        self.assertIn("3h alert\nZX421\n2037-08-29 1140 SG", rendered)


if __name__ == "__main__":
    unittest.main()
