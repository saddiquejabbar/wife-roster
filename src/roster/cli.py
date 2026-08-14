from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from .alerts import calculate_alerts
from .console_notifier import ConsoleNotifier
from .database import RosterDatabase
from .deployment import Deployer, DeploymentError, DeploymentPaths
from .diff import diff_rosters
from .dispatcher import Dispatcher
from .extractor import ExtractionError, extract_candidate, hash_file_set
from .formatter import (
    format_alert_listing,
    format_diff,
    format_issues,
    format_summary,
)
from .normalize import NormalizationError, normalize_transcription
from .launchd_scheduler import LaunchdScheduler, SchedulerError
from .models import AlertType
from .models import Alert
from .notifier import Notifier
from .openclaw_notifier import (
    OpenClawConfigurationError,
    OpenClawError,
    OpenClawNotifier,
    default_runtime_env_path,
)
from .telegram_notifier import (
    TelegramConfig,
    TelegramConfigurationError,
    TelegramError,
    TelegramNotifier,
)
from .validator import validate_roster


class NotifierSelectionError(RuntimeError):
    """A controlled notifier-selection error."""


class _UnexpectedNotifierFailure(RuntimeError):
    pass


class _UnavailableNotifier:
    """Turn runtime configuration loss into the normal bounded retry path."""

    def send(self, alert: Alert, *, sent_at: datetime) -> str:
        raise RuntimeError("notifier configuration unavailable")


class _DeliveryFailureGuard:
    """Preserve clean exits only for controlled, recorded delivery failures."""

    def __init__(self, notifier: Notifier) -> None:
        self.notifier = notifier
        self.unexpected_failure = False

    def send(self, alert: Alert, *, sent_at: datetime) -> str:
        try:
            return self.notifier.send(alert, sent_at=sent_at)
        except (OpenClawError, TelegramError):
            raise
        except Exception:
            self.unexpected_failure = True
            raise _UnexpectedNotifierFailure("unexpected notifier failure") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roster", description="Local deterministic roster workflow")
    parser.add_argument(
        "--db",
        default=os.environ.get("WIFE_ROSTER_DB", str(DeploymentPaths.default().database)),
        help="private SQLite database path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="transcribe and evaluate one candidate roster")
    ingest.add_argument("files", nargs="+", help="JPG, JPEG, PNG, PDF, or transcription JSON")
    ingest.add_argument("--dry-run", action="store_true", help="evaluate without activation")
    ingest.add_argument("--transcription", help="pinned raw transcription JSON")
    ingest.add_argument("--now", help="timezone-aware ISO time for deterministic testing")
    ingest.add_argument("--verbose", action="store_true")

    subparsers.add_parser("summary", help="show the active roster")
    subparsers.add_parser("diff", help="show the latest stored roster diff")
    subparsers.add_parser("alerts", help="show pending calculated alerts")
    subparsers.add_parser("status", help="show private workflow status")
    dispatch = subparsers.add_parser("dispatch-due", help="claim and deliver due alerts, then exit")
    dispatch.add_argument("--now", help="timezone-aware ISO time for deterministic validation")
    recover = subparsers.add_parser("recover", help="recover recent overdue alerts, dispatch, then exit")
    recover.add_argument("--now", help="timezone-aware ISO time for deterministic validation")
    subparsers.add_parser("scheduler-status", help="show wife-roster launchd status")
    subparsers.add_parser("scheduler-refresh", help="install or reload only the wife-roster agent")
    subparsers.add_parser("plan-next", help=argparse.SUPPRESS)
    subparsers.add_parser("deploy", help="deploy the runtime under Application Support")
    subparsers.add_parser("telegram-test", help="send one explicit Telegram test message")
    subparsers.add_parser(
        "inbound-review",
        help=argparse.SUPPRESS,
    )
    subparsers.add_parser(
        "inbound-approve",
        help=argparse.SUPPRESS,
    )
    subparsers.add_parser(
        "inbound-revise",
        help=argparse.SUPPRESS,
    )
    subparsers.add_parser(
        "inbound-cancel",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = RosterDatabase(Path(args.db))
    if args.command == "ingest":
        return _ingest(args, database)
    if args.command == "summary":
        roster = database.active_roster()
        print(format_summary(roster) if roster else "No active roster")
        return 0
    if args.command == "diff":
        previous, latest = database.latest_two_rosters()
        if latest is None:
            print("No roster versions")
        elif previous is None:
            print(format_diff(diff_rosters(None, latest)))
        else:
            print(format_diff(diff_rosters(previous, latest)))
        return 0
    if args.command == "alerts":
        print(format_alert_listing(database.pending_alerts()))
        return 0
    if args.command == "status":
        status = database.status_counts()
        deployment = DeploymentPaths.default()
        scheduler_status = _scheduler(database).status()
        print(f"Runtime path: {deployment.root}")
        print(f"Database: {'available' if database.exists() else 'unavailable'}")
        print(f"Planner: {'loaded' if scheduler_status.planner_loaded else 'not loaded'}")
        print(
            "Calendar dispatcher: "
            + ("armed" if scheduler_status.dispatcher_armed else "not armed")
        )
        print(f"Active roster: {'yes' if status['active_content_hash'] else 'no'}")
        print(f"Roster versions: {status['roster_versions']}")
        print(f"Ingestions: {status['ingestions']}")
        print(f"Pending alerts: {status['pending_alerts']}")
        print(f"Sending alerts: {status['sending_alerts']}")
        print(f"Failed alerts: {status['failed_alerts']}")
        print(f"Missed alerts: {status['missed_alerts']}")
        print(f"Superseded alerts: {status['superseded_alerts']}")
        print(f"Sent alerts: {status['sent_alerts']}")
        print(
            f"Schedule generation: {status['schedule_generation']} "
            f"(planned {status['planned_schedule_generation']})"
        )
        print(f"Delivery attempts: {status['delivery_attempts']}")
        print(f"Failed attempts: {status['failed_attempts']}")
        print(f"Last successful delivery: {status['last_successful_delivery'] or 'never'}")
        return 0
    if args.command in ("dispatch-due", "recover"):
        now = _parse_now(args.now)
        if now is None:
            print("NEEDS REVIEW\n- --now must be a timezone-aware ISO datetime")
            return 2
        configuration_error: Exception | None = None
        try:
            notifier = _notifier_from_environment()
        except (
            NotifierSelectionError,
            OpenClawConfigurationError,
            TelegramConfigurationError,
        ) as exc:
            if not database.exists():
                print(f"NOTIFIER CONFIGURATION WARNING\n- {exc}")
                return 2
            configuration_error = exc
            notifier = _UnavailableNotifier()
        guarded_notifier = (
            _DeliveryFailureGuard(notifier) if configuration_error is None else None
        )
        dispatcher = Dispatcher(
            database,
            guarded_notifier or notifier,
            clock=lambda: now,
        )
        scheduler = _scheduler(database)
        try:
            result = dispatcher.dispatch_due()
        except Exception:
            print("DISPATCH FAILED\n- unexpected program failure")
            return 1
        finally:
            try:
                scheduler.request_replan()
            except (OSError, SchedulerError):
                pass
        if configuration_error is not None:
            print(f"NOTIFIER CONFIGURATION WARNING\n- {configuration_error}")
            if result.failed:
                print(f"Dispatch deferred: {result.failed} alert(s) entered bounded retry")
            return 2
        if result.claimed == 0 and result.missed == 0 and result.recovered == 0:
            print("No due alerts")
        else:
            print(
                f"Dispatch complete: {result.sent} sent, {result.failed} failed, "
                f"{result.missed} missed"
            )
        return 1 if guarded_notifier and guarded_notifier.unexpected_failure else 0
    if args.command == "scheduler-status":
        scheduler = _scheduler(database)
        print(_format_scheduler_status(scheduler, database))
        return 0
    if args.command == "plan-next":
        from .planner import SchedulePlanner

        scheduler = _scheduler(database)
        try:
            result = SchedulePlanner(database, scheduler).plan()
        except (OSError, SchedulerError, ValueError) as exc:
            print(f"PLANNER WARNING\n- {exc}")
            return 2
        if result.armed_for is None:
            print(f"Schedule generation {result.generation}: no wake required")
        else:
            print(
                f"Schedule generation {result.generation}: armed for "
                f"{result.armed_for.astimezone(UTC).isoformat()}"
            )
        return 0
    if args.command == "deploy":
        workspace = Path(__file__).resolve().parents[2]
        try:
            result = Deployer(workspace=workspace).deploy()
        except DeploymentError as exc:
            print(f"DEPLOYMENT WARNING\n- {exc}")
            return 2
        print("Deployment complete")
        print(f"Runtime path: {result.paths.root}")
        print(f"Database: {result.paths.database}")
        print("Verification: passed")
        return 0
    if args.command == "telegram-test":
        now = datetime.now(UTC)
        try:
            notifier = TelegramNotifier.from_environment()
            notifier.send(
                Alert(
                    event_key="telegram-test",
                    alert_type=AlertType.PREP_3H,
                    due_utc=now,
                    message="Telegram test successful",
                ),
                sent_at=now,
            )
        except TelegramError as exc:
            print(f"TELEGRAM TEST FAILED\n- {exc}")
            return 2
        print("Telegram test successful")
        return 0
    if args.command in (
        "inbound-review",
        "inbound-approve",
        "inbound-revise",
        "inbound-cancel",
    ):
        from .inbound_cli import run_inbound_command

        command = {
            "inbound-review": "review",
            "inbound-approve": "approve",
            "inbound-revise": "revise",
            "inbound-cancel": "cancel",
        }[args.command]
        verifier = (
            (lambda result: _verify_inbound_activation(database, result))
            if command == "approve"
            else None
        )
        preflight = (
            (lambda: _verify_inbound_runtime_ready(database))
            if command == "approve"
            else None
        )
        return run_inbound_command(
            command,
            database=database,
            runtime_preflight=preflight,
            runtime_verifier=verifier,
        )
    if args.command == "scheduler-refresh":
        scheduler = _scheduler(database)
        try:
            scheduler.refresh()
        except SchedulerError as exc:
            print(f"SCHEDULER WARNING\n- {exc}")
            return 2
        print(_format_scheduler_status(scheduler, database))
        return 0
    return 2


def _ingest(args: argparse.Namespace, database: RosterDatabase) -> int:
    if not args.dry_run:
        print("NEEDS REVIEW\n- Stage 1 ingest requires --dry-run; activation awaits explicit approval")
        return 2
    try:
        file_set_hash, _ = hash_file_set(args.files)
    except OSError as exc:
        print(f"NEEDS REVIEW\n- Could not read roster source: {exc}")
        return 2
    if database.has_file_set(file_set_hash):
        print("Roster unchanged")
        return 0
    try:
        transcription = extract_candidate(
            args.files,
            transcription_path=args.transcription,
        )
        roster = normalize_transcription(transcription, file_set_hash=file_set_hash)
    except ExtractionError as exc:
        print(f"NEEDS REVIEW\n- {exc}")
        return 2
    except NormalizationError as exc:
        print(format_issues(exc.issues))
        return 2
    validate_roster(roster)
    active = database.active_roster()
    roster_diff = diff_rosters(active, roster)
    if active is not None and roster_diff.unchanged:
        print("Roster unchanged")
        return 0
    now = _parse_now(args.now)
    if now is None and args.now:
        print("NEEDS REVIEW\n- --now must be a timezone-aware ISO datetime")
        return 2
    alerts = calculate_alerts(roster, now=now)
    summary = format_summary(roster)
    if summary:
        print(summary)
    else:
        print("No valid flying duties")
    if roster.issues:
        print()
        print(format_issues(roster.issues))
    if args.verbose:
        print()
        print(format_diff(roster_diff))
        print(f"Candidate content hash: {roster.content_hash}")
        print(f"Proposed alerts: {len(alerts)}")
    return 2 if roster.issues else 0


def _parse_now(value: str | None) -> datetime | None:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _notifier_from_environment(
    environment: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
) -> Notifier:
    values = dict(environment if environment is not None else os.environ)
    runtime_env = (
        Path(env_file).expanduser()
        if env_file
        else default_runtime_env_path(values)
    )
    if "WIFE_ROSTER_NOTIFIER" in values:
        selected = values["WIFE_ROSTER_NOTIFIER"].strip()
    else:
        selected = _read_notifier_from_env(runtime_env)
    selected = selected or "console"
    if selected == "console":
        return ConsoleNotifier()
    if selected == "openclaw":
        return OpenClawNotifier.from_environment(values, env_file=runtime_env)
    if selected == "telegram":
        return TelegramNotifier(
            TelegramConfig.from_environment(values, env_file=runtime_env)
        )
    raise NotifierSelectionError(
        "WIFE_ROSTER_NOTIFIER must be one of: console, openclaw, telegram"
    )


def _read_notifier_from_env(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise NotifierSelectionError(
            "wife-roster environment file could not be read"
        ) from None
    selected = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != "WIFE_ROSTER_NOTIFIER":
            continue
        selected = value.strip()
        if len(selected) >= 2 and selected[0] == selected[-1] and selected[0] in {'"', "'"}:
            selected = selected[1:-1]
    return selected.strip()


def _scheduler(database: RosterDatabase) -> LaunchdScheduler:
    deployment = DeploymentPaths.default()
    return LaunchdScheduler(
        database_path=deployment.database,
        workflow_dir=deployment.root,
        command=[str(deployment.python), "-m", "roster.cli"],
        python_paths=deployment.python_paths,
        log_path=deployment.dispatcher_log,
    )


def _verify_inbound_runtime_ready(database: RosterDatabase) -> None:
    from .inbound_cli import (
        InboundConfigurationError,
        InboundPreflightError,
        _load_config,
    )

    try:
        notifier = _notifier_from_environment()
        inbound_config = _load_config(None)
        scheduler_status = _scheduler(database).status()
    except (
        InboundConfigurationError,
        NotifierSelectionError,
        OpenClawConfigurationError,
        SchedulerError,
        TelegramConfigurationError,
    ) as exc:
        raise InboundPreflightError() from exc
    if (
        not isinstance(notifier, OpenClawNotifier)
        or notifier.config.target != inbound_config.group_id
        or not scheduler_status.installed
        or not scheduler_status.loaded
    ):
        raise InboundPreflightError()


def _verify_inbound_activation(database: RosterDatabase, result: object) -> None:
    from .inbound_cli import InboundOperationalError, InboundPreflightError

    content_hash = getattr(result, "content_hash", None)
    active = database.active_roster()
    pending = database.pending_alerts()
    scheduler = _scheduler(database)
    try:
        scheduler.refresh()
        scheduler_status = scheduler.status()
        _verify_inbound_runtime_ready(database)
    except (SchedulerError, InboundOperationalError, InboundPreflightError) as exc:
        raise InboundOperationalError() from exc
    if (
        not isinstance(content_hash, str)
        or active is None
        or active.content_hash != content_hash
        or not scheduler_status.installed
        or not scheduler_status.loaded
        or len({alert.event_key for alert in pending}) != len(pending)
    ):
        raise InboundOperationalError()


def _format_scheduler_status(
    scheduler: LaunchdScheduler,
    database: RosterDatabase,
) -> str:
    status = scheduler.status()
    singapore = ZoneInfo("Asia/Singapore")
    last_dispatch = database.last_dispatch()
    next_alert = database.next_pending_alert()
    lines = [
        "Scheduler:",
        f"planner installed: {'yes' if status.planner_installed else 'no'}",
        f"planner launchd: {'loaded' if status.planner_loaded else 'not loaded'}",
        f"calendar dispatcher: {'armed' if status.dispatcher_armed else 'not armed'}",
        "last dispatch: "
        + (last_dispatch.astimezone(singapore).strftime("%Y-%m-%d %H%M SG") if last_dispatch else "never"),
        "next pending alert:",
    ]
    schedule = database.schedule_state()
    lines.insert(
        4,
        "generation: "
        f"desired {schedule['schedule_generation']}, "
        f"planned {schedule['planned_schedule_generation']}",
    )
    lines.insert(5, f"armed for UTC: {status.armed_for_utc or 'none'}")
    if next_alert is None:
        lines.append("none")
    else:
        lines.extend(
            (
                _alert_type_display(next_alert.alert_type),
                _flight_from_message(next_alert.message),
                next_alert.due_utc.astimezone(singapore).strftime("%Y-%m-%d %H%M SG"),
            )
        )
    if status.warnings:
        lines.extend(("", "SCHEDULER WARNING"))
        lines.extend(f"- {warning}" for warning in status.warnings)
    return "\n".join(lines)


def _alert_type_display(alert_type: AlertType) -> str:
    return {
        AlertType.PREP_12H: "12h alert",
        AlertType.PREP_3H: "3h alert",
        AlertType.LANDING_1H: "landing alert",
    }[alert_type]


def _flight_from_message(message: str) -> str:
    match = re.search(r"\b[A-Z0-9]{2,3}\d{1,4}[A-Z]?\b", message)
    return match.group(0) if match else "unknown flight"


if __name__ == "__main__":
    sys.exit(main())
