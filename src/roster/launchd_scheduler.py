from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Sequence

from .scheduler import SchedulerStatus


LEGACY_LABEL = "org.example.wife-roster"
PLANNER_LABEL = f"{LEGACY_LABEL}.planner"
DISPATCHER_LABEL = f"{LEGACY_LABEL}.dispatcher"
PLIST_FILENAME = f"{LEGACY_LABEL}.plist"
PLANNER_PLIST_FILENAME = f"{PLANNER_LABEL}.plist"
DISPATCHER_PLIST_FILENAME = f"{DISPATCHER_LABEL}.plist"


class SchedulerError(RuntimeError):
    pass


class LaunchdScheduler:
    """Manage a persistent planner and one calendar-armed dispatcher.

    SQLite is authoritative. The planner rewrites the dispatcher plist to hold
    only the next due/retry/lease-recovery time, eliminating periodic polling.
    """

    def __init__(
        self,
        *,
        database_path: str | Path,
        workflow_dir: str | Path | None = None,
        plist_path: str | Path | None = None,
        command: Sequence[str] | None = None,
        python_paths: Sequence[str | Path] | None = None,
        log_path: str | Path | None = None,
        signal_path: str | Path | None = None,
        uid: int | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        **legacy_options: object,
    ) -> None:
        # Accept the retired interval argument during a rolling upgrade, but it
        # intentionally has no effect on the calendar scheduler.
        legacy_options.pop("interval_seconds", None)
        if legacy_options:
            raise TypeError(f"unexpected scheduler options: {', '.join(legacy_options)}")
        self.workflow_dir = Path(workflow_dir or Path(__file__).resolve().parents[2]).resolve()
        self.database_path = Path(database_path).resolve()
        legacy_path = Path(
            plist_path or Path.home() / "Library" / "LaunchAgents" / PLIST_FILENAME
        )
        if legacy_path.name not in {
            PLIST_FILENAME,
            PLANNER_PLIST_FILENAME,
            DISPATCHER_PLIST_FILENAME,
        }:
            raise ValueError("LaunchdScheduler plist path must be a wife-roster plist")
        self.plist_directory = legacy_path.parent
        self.legacy_plist_path = self.plist_directory / PLIST_FILENAME
        self.planner_plist_path = self.plist_directory / PLANNER_PLIST_FILENAME
        self.dispatcher_plist_path = self.plist_directory / DISPATCHER_PLIST_FILENAME
        # Compatibility for callers that displayed the old path.
        self.plist_path = self.planner_plist_path
        self.uid = os.getuid() if uid is None else uid
        self.runner = runner or subprocess.run
        self.command = tuple(command or _default_command(self.workflow_dir))
        if python_paths is None:
            deployed_paths = (self.workflow_dir / "app" / "src", self.workflow_dir / "app" / "vendor")
            development_path = self.workflow_dir / "src"
            python_paths = deployed_paths if deployed_paths[0].is_dir() else (development_path,)
        self.python_paths = tuple(Path(path).resolve() for path in python_paths)
        self._log_path = Path(log_path).resolve() if log_path else None
        self.signal_path = Path(
            signal_path or self.workflow_dir / "runtime" / "private" / "schedule.request"
        ).resolve()

    @property
    def domain_name(self) -> str:
        return f"gui/{self.uid}"

    def service_name(self, label: str) -> str:
        return f"{self.domain_name}/{label}"

    @property
    def log_path(self) -> Path:
        if self._log_path is not None:
            return self._log_path
        deployed_logs = self.workflow_dir / "runtime" / "logs"
        if (self.workflow_dir / "app").is_dir():
            return deployed_logs / "dispatcher.log"
        return self.workflow_dir / "runtime" / "private" / "dispatcher.log"

    @property
    def planner_log_path(self) -> Path:
        return self.log_path.with_name("planner.log")

    def generate_planner_plist(self) -> bytes:
        value = self._base_plist(PLANNER_LABEL, "plan-next", self.planner_log_path)
        value.update({"RunAtLoad": True, "WatchPaths": [str(self.signal_path)]})
        return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)

    def generate_dispatcher_plist(self, when: datetime, *, generation: int) -> bytes:
        exact_utc = _require_aware(when).astimezone(UTC)
        armed = exact_utc.astimezone()
        if armed.second or armed.microsecond:
            armed = (armed + timedelta(minutes=1)).replace(second=0, microsecond=0)
        value = self._base_plist(DISPATCHER_LABEL, "dispatch-due", self.log_path)
        value["EnvironmentVariables"]["WIFE_ROSTER_SCHEDULE_GENERATION"] = str(generation)
        value["EnvironmentVariables"]["WIFE_ROSTER_ARMED_FOR_UTC"] = (
            exact_utc.isoformat().replace("+00:00", "Z")
        )
        value["StartCalendarInterval"] = {
            "Month": armed.month,
            "Day": armed.day,
            "Hour": armed.hour,
            "Minute": armed.minute,
        }
        return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)

    # Retained as a clear error for integrations compiled against the poller API.
    def generate_plist(self) -> bytes:
        return self.generate_planner_plist()

    def install(self) -> SchedulerStatus:
        self._require_workflow()
        self._require_launchd_workflow_access()
        self._prepare_private_paths()
        self._write_plist(self.planner_plist_path, self.generate_planner_plist(), "planner")
        if not self._is_loaded(PLANNER_LABEL):
            self._launchctl("bootstrap", self.domain_name, str(self.planner_plist_path))
        self._remove_legacy_agent()
        return self.status()

    def refresh(self) -> SchedulerStatus:
        self.install()
        if self._is_loaded(PLANNER_LABEL):
            self._launchctl("bootout", self.service_name(PLANNER_LABEL))
        self._launchctl("bootstrap", self.domain_name, str(self.planner_plist_path))
        from .database import RosterDatabase
        from .planner import SchedulePlanner

        SchedulePlanner(RosterDatabase(self.database_path), self).plan()
        return self.status()

    def request_replan(self) -> None:
        self._prepare_private_paths()
        self.signal_path.touch(exist_ok=True)
        self.signal_path.chmod(0o600)

    def arm(self, when: datetime, *, generation: int) -> None:
        self._require_workflow()
        self._prepare_private_paths()
        payload = self.generate_dispatcher_plist(when, generation=generation)
        self._write_plist(self.dispatcher_plist_path, payload, "dispatcher")
        if self._is_loaded(DISPATCHER_LABEL):
            self._launchctl("bootout", self.service_name(DISPATCHER_LABEL))
        self._launchctl("bootstrap", self.domain_name, str(self.dispatcher_plist_path))

    def disarm(self) -> None:
        if self._is_loaded(DISPATCHER_LABEL):
            self._launchctl("bootout", self.service_name(DISPATCHER_LABEL))
        self.dispatcher_plist_path.unlink(missing_ok=True)

    def remove(self) -> SchedulerStatus:
        self.disarm()
        if self._is_loaded(PLANNER_LABEL):
            self._launchctl("bootout", self.service_name(PLANNER_LABEL))
        self.planner_plist_path.unlink(missing_ok=True)
        self._remove_legacy_agent()
        return self.status()

    def status(self) -> SchedulerStatus:
        planner_installed = self.planner_plist_path.is_file()
        planner_valid = self._plist_is_valid(self.planner_plist_path, "planner") if planner_installed else False
        planner_loaded = self._is_loaded(PLANNER_LABEL)
        dispatcher_armed = self.dispatcher_plist_path.is_file()
        dispatcher_valid = (
            self._plist_is_valid(self.dispatcher_plist_path, "dispatcher")
            if dispatcher_armed
            else True
        )
        dispatcher_loaded = self._is_loaded(DISPATCHER_LABEL) if dispatcher_armed else False
        armed_for, armed_generation = self._read_arm_metadata()
        planner_state = self._run_launchctl("print", self.service_name(PLANNER_LABEL))
        exit_match = re.search(r"last exit code\s*=\s*(\d+)", planner_state.stdout)
        last_exit_code = int(exit_match.group(1)) if exit_match else None
        workflow_available = self.workflow_dir.is_dir() and not (
            last_exit_code == 126 or self._log_reports_unavailable_workflow()
        )
        warnings: list[str] = []
        if not planner_installed:
            warnings.append("planner launchd agent is missing")
        elif not planner_valid:
            warnings.append("planner launchd plist is invalid")
        if planner_installed and not planner_loaded:
            warnings.append("planner launchd agent is not loaded")
        if dispatcher_armed and not dispatcher_valid:
            warnings.append("dispatcher launchd plist is invalid")
        if dispatcher_armed and not dispatcher_loaded:
            warnings.append("dispatcher calendar wake is not loaded")
        if self.legacy_plist_path.exists() or self._is_loaded(LEGACY_LABEL):
            warnings.append("legacy five-minute scheduler is still present")
        if not workflow_available:
            warnings.append("workflow directory is unavailable")
        return SchedulerStatus(
            installed=planner_installed,
            loaded=planner_loaded,
            valid=planner_valid and dispatcher_valid,
            workflow_available=workflow_available,
            warnings=tuple(warnings),
            planner_installed=planner_installed,
            planner_loaded=planner_loaded,
            dispatcher_armed=dispatcher_armed and dispatcher_loaded,
            armed_for_utc=armed_for,
            armed_generation=armed_generation,
        )

    def _base_plist(self, label: str, subcommand: str, log_path: Path) -> dict[str, object]:
        environment = {"WIFE_ROSTER_DB": str(self.database_path)}
        if self.python_paths:
            environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in self.python_paths)
        return {
            "Label": label,
            "ProgramArguments": [*self.command, subcommand],
            "WorkingDirectory": str(self.workflow_dir),
            "EnvironmentVariables": environment,
            "ProcessType": "Background",
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
            "Umask": 0o077,
        }

    def _prepare_private_paths(self) -> None:
        self.plist_directory.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.signal_path.parent.mkdir(parents=True, exist_ok=True)
        self.signal_path.parent.chmod(0o700)
        for log in (self.log_path, self.planner_log_path):
            if log.exists():
                log.chmod(0o600)

    def _write_plist(self, path: Path, payload: bytes, kind: str) -> None:
        self._validate_plist(plistlib.loads(payload), kind)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.chmod(0o600)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _plist_is_valid(self, path: Path, kind: str) -> bool:
        try:
            self._validate_plist(plistlib.loads(path.read_bytes()), kind)
        except (OSError, plistlib.InvalidFileException, SchedulerError):
            return False
        return True

    def _validate_plist(self, value: object, kind: str) -> None:
        if not isinstance(value, dict):
            raise SchedulerError("plist root must be a dictionary")
        label = PLANNER_LABEL if kind == "planner" else DISPATCHER_LABEL
        command = "plan-next" if kind == "planner" else "dispatch-due"
        if value.get("Label") != label:
            raise SchedulerError(f"{kind} plist label is invalid")
        if value.get("ProgramArguments") != [*self.command, command]:
            raise SchedulerError(f"{kind} command does not match wife-roster")
        if value.get("WorkingDirectory") != str(self.workflow_dir):
            raise SchedulerError("plist workflow directory does not match wife-roster")
        environment = value.get("EnvironmentVariables")
        if not isinstance(environment, dict) or environment.get("WIFE_ROSTER_DB") != str(self.database_path):
            raise SchedulerError("plist database does not match wife-roster")
        expected_pythonpath = os.pathsep.join(str(path) for path in self.python_paths)
        if expected_pythonpath and environment.get("PYTHONPATH") != expected_pythonpath:
            raise SchedulerError("plist application path does not match wife-roster")
        if value.get("Umask") != 0o077:
            raise SchedulerError("plist umask is not private")
        if kind == "planner":
            if value.get("RunAtLoad") is not True or value.get("WatchPaths") != [str(self.signal_path)]:
                raise SchedulerError("planner wake configuration is invalid")
            if "StartInterval" in value or "StartCalendarInterval" in value:
                raise SchedulerError("planner must not poll")
        else:
            calendar = value.get("StartCalendarInterval")
            if not isinstance(calendar, dict) or set(calendar) != {"Month", "Day", "Hour", "Minute"}:
                raise SchedulerError("dispatcher calendar wake is invalid")
            if "StartInterval" in value or value.get("RunAtLoad") is True:
                raise SchedulerError("dispatcher must be calendar-only")

    def _read_arm_metadata(self) -> tuple[str | None, int | None]:
        if not self.dispatcher_plist_path.is_file():
            return None, None
        try:
            value = plistlib.loads(self.dispatcher_plist_path.read_bytes())
            environment = value["EnvironmentVariables"]
            generation = int(environment["WIFE_ROSTER_SCHEDULE_GENERATION"])
            return str(environment["WIFE_ROSTER_ARMED_FOR_UTC"]), generation
        except (KeyError, OSError, TypeError, ValueError, plistlib.InvalidFileException):
            return None, None

    def _is_loaded(self, label: str) -> bool:
        return self._run_launchctl("print", self.service_name(label)).returncode == 0

    def _remove_legacy_agent(self) -> None:
        if self._is_loaded(LEGACY_LABEL):
            self._launchctl("bootout", self.service_name(LEGACY_LABEL))
        self.legacy_plist_path.unlink(missing_ok=True)

    def _launchctl(self, *arguments: str) -> None:
        completed = self._run_launchctl(*arguments)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown launchctl error"
            raise SchedulerError(f"launchctl {arguments[0]} failed: {detail}")

    def _run_launchctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(["/bin/launchctl", *arguments], capture_output=True, text=True, check=False)
        except OSError as exc:
            return subprocess.CompletedProcess(["/bin/launchctl", *arguments], 127, "", str(exc))

    def _require_workflow(self) -> None:
        if not self.workflow_dir.is_dir():
            raise SchedulerError("workflow directory is unavailable")

    def _require_launchd_workflow_access(self) -> None:
        if self._log_reports_unavailable_workflow():
            raise SchedulerError("workflow directory is unavailable to launchd")

    def _log_reports_unavailable_workflow(self) -> bool:
        for path in (self.planner_log_path, self.log_path):
            try:
                lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
            except OSError:
                continue
            last_line = next((line for line in reversed(lines) if line), "")
            if "Operation not permitted" in last_line and str(self.workflow_dir) in last_line:
                return True
        return False


def _default_command(workflow_dir: Path) -> list[str]:
    configured = os.environ.get("WIFE_ROSTER_COMMAND", "").strip()
    if configured:
        return shlex.split(configured)
    project_executable = workflow_dir / ".venv" / "bin" / "roster"
    if project_executable.is_file():
        return [str(project_executable)]
    executable = shutil.which("roster")
    if executable:
        return [executable]
    return [sys.executable, "-m", "roster.cli"]


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    return value
