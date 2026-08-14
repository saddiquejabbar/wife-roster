from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, TextIO
from zoneinfo import ZoneInfo

from .alerts import calculate_alerts
from .database import RosterDatabase
from .deployment import DeploymentPaths
from .extractor import ExtractionError, hash_file_set
from .formatter import format_summary
from .inbound_review import (
    MAX_ATTACHMENT_BYTES,
    MAX_CANDIDATE_BYTES,
    CandidateAlreadyAppliedError,
    CandidateMismatchError,
    InboundAttachment,
    InboundAuthorizationError,
    InboundReviewError,
    InboundReviewService,
    NoAttachmentError,
    NoPendingCandidateError,
    ReviewExpiredError,
    ReviewInactiveError,
    ReviewEvaluation,
    ReviewRequest,
    UnresolvedReviewError,
    UnsupportedAttachmentError,
)
from .models import RawTranscription, ReviewIssue
from .normalize import NormalizationError, normalize_transcription
from .validator import validate_roster


class InboundConfigurationError(InboundReviewError):
    pass


class InboundOperationalError(InboundReviewError):
    pass


class InboundPreflightError(InboundReviewError):
    pass


def run_inbound_command(
    command: str,
    *,
    database: RosterDatabase,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
    runtime_preflight: Callable[[], None] | None = None,
    runtime_verifier: Callable[[object], None] | None = None,
) -> int:
    source = stdin or sys.stdin
    destination = stdout or sys.stdout
    try:
        request = _read_request(source)
        config = _load_config(environment)
        service = InboundReviewService(
            inbox_root=config.inbox_root,
            database=database,
            allowed_group_id=config.group_id,
            allowed_sender_ids=config.allowed_senders,
            allowed_state_change_sender_ids=config.state_change_senders,
            evaluator=_review_evaluator(
                request.get("transcription") if command == "review" else None,
                now=_parse_now(request.get("now")),
            ),
            now=lambda: _parse_now(request.get("now")) or datetime.now(UTC),
        )
        if command == "review":
            group_id = _required_string(request, "group_id")
            sender_id = _required_string(request, "sender_id")
            if group_id != config.group_id or sender_id not in config.allowed_senders:
                raise InboundAuthorizationError("Not authorized.")
            mentioned = request.get("mentioned")
            if not isinstance(mentioned, bool):
                raise ValueError("mentioned must be boolean")
            if not mentioned:
                return _emit(destination, {"handled": False})
            result = service.review(_review_request(request))
            if result is None:
                return _emit(destination, {"handled": False})
            return _emit(
                destination,
                {
                    "handled": True,
                    "ok": True,
                    "ingestion_id": result.ingestion_id,
                    "review_id": result.review_id,
                    "can_approve": result.approvable,
                    "reply": format_review_reply(result),
                    "duty_count": result.duty_count,
                    "flight_count": result.flight_count,
                    "future_alert_count": result.future_alert_count,
                    "issue_count": len(result.issues),
                },
            )
        if command == "approve":
            group_id = _required_string(request, "group_id")
            sender_id = _required_string(request, "sender_id")
            result = service.approve(
                group_id=group_id,
                sender_id=sender_id,
                review_id=_optional_string(request, "review_id"),
                pre_apply_verifier=runtime_preflight,
            )
            if runtime_verifier is not None:
                runtime_verifier(result)
            return _emit(
                destination,
                {
                    "handled": True,
                    "ok": True,
                    "reply": format_approval_reply(result),
                    "review_id": result.review_id,
                    "activated": result.activated,
                    "updated": result.updated,
                    "unchanged": result.unchanged,
                    "flight_count": result.flight_count,
                    "future_alert_count": result.future_alert_count,
                },
            )
        if command == "revise":
            result = service.revise(
                group_id=_required_string(request, "group_id"),
                sender_id=_required_string(request, "sender_id"),
                review_id=_required_string(request, "review_id"),
            )
            return _emit(
                destination,
                {
                    "handled": True,
                    "ok": True,
                    "reply": format_revision_reply(),
                    "review_id": result.review_id,
                    "status": result.status,
                },
            )
        if command == "cancel":
            group_id = _required_string(request, "group_id")
            sender_id = _required_string(request, "sender_id")
            ingestion_id = _required_string(request, "ingestion_id")
            service.cancel_pending(
                group_id=group_id,
                sender_id=sender_id,
                ingestion_id=ingestion_id,
            )
            return _emit(
                destination,
                {
                    "handled": True,
                    "ok": True,
                    "reply": "Roster review cancelled.",
                },
            )
        raise InboundConfigurationError("Unknown inbound command.")
    except NoAttachmentError as exc:
        return _emit_error(destination, str(exc), exit_code=2)
    except NoPendingCandidateError:
        return _emit_error(
            destination,
            "No roster awaiting approval.",
            exit_code=2,
            error_code="no_pending",
            terminal=True,
        )
    except ReviewExpiredError:
        return _emit_error(
            destination,
            "This roster review has expired. Please run the roster again.",
            exit_code=2,
            error_code="expired",
            terminal=True,
        )
    except CandidateAlreadyAppliedError:
        return _emit_error(
            destination,
            "This roster is already active.",
            exit_code=2,
            error_code="already_applied",
            terminal=True,
        )
    except ReviewInactiveError:
        return _emit_error(
            destination,
            "This roster review is no longer active.",
            exit_code=2,
            error_code="inactive",
            terminal=True,
        )
    except UnresolvedReviewError:
        return _emit_error(
            destination,
            "Roster has unresolved NEEDS REVIEW items.",
            exit_code=2,
            error_code="not_approvable",
        )
    except CandidateMismatchError:
        return _emit_error(
            destination,
            "Reviewed roster changed. Run wife-roster again.",
            exit_code=2,
            error_code="candidate_changed",
            terminal=True,
        )
    except InboundPreflightError:
        return _emit_error(
            destination,
            "Roster not activated because production checks failed.",
            exit_code=2,
            error_code="preflight",
        )
    except InboundOperationalError:
        return _emit_error(
            destination,
            "Roster activated, but operational verification failed.",
            exit_code=2,
            error_code="operational_verification",
            terminal=True,
        )
    except InboundAuthorizationError:
        # Do not reveal which authorization field failed.
        return _emit_error(
            destination,
            "Not authorized.",
            exit_code=3,
            error_code="unauthorized",
        )
    except (UnsupportedAttachmentError, InboundConfigurationError) as exc:
        return _emit_error(destination, str(exc), exit_code=2)
    except NormalizationError as exc:
        issues = tuple(exc.issues)
        return _emit(
            destination,
            {
                "handled": True,
                "ok": False,
                "reply": _format_normalization_failure(issues),
                "issue_count": len(issues),
            },
            exit_code=2,
        )
    except (ExtractionError, ValueError, OSError, json.JSONDecodeError):
        return _emit_error(
            destination,
            "Roster could not be reviewed safely.",
            exit_code=2,
        )


class _InboundConfig:
    def __init__(
        self,
        *,
        group_id: str,
        allowed_senders: frozenset[str],
        state_change_senders: frozenset[str],
        inbox_root: Path,
    ):
        self.group_id = group_id
        self.allowed_senders = allowed_senders
        self.state_change_senders = state_change_senders
        self.inbox_root = inbox_root


def _load_config(environment: Mapping[str, str] | None) -> _InboundConfig:
    values = dict(environment if environment is not None else os.environ)
    runtime = DeploymentPaths.default()
    env_file = runtime.root / ".env"
    if env_file.is_file():
        for name, value in _read_env_file(env_file).items():
            values.setdefault(name, value)
    group_id = values.get("WIFE_ROSTER_INBOUND_GROUP_ID", "").strip()
    allowed_senders = frozenset(
        item.strip()
        for item in values.get("WIFE_ROSTER_INBOUND_ALLOWED_SENDERS", "").split(",")
        if item.strip()
    )
    raw_state_change_senders = values.get(
        "WIFE_ROSTER_STATE_CHANGE_ALLOWED_SENDERS",
        "",
    )
    state_change_senders = frozenset(
        item.strip()
        for item in raw_state_change_senders.split(",")
        if item.strip()
    ) or allowed_senders
    if not group_id or not allowed_senders:
        raise InboundConfigurationError("Inbound wife-roster access is not configured.")
    inbox = values.get("WIFE_ROSTER_INBOX", "").strip()
    inbox_root = Path(inbox).expanduser() if inbox else runtime.private / "inbox"
    return _InboundConfig(
        group_id=group_id,
        allowed_senders=allowed_senders,
        state_change_senders=state_change_senders,
        inbox_root=inbox_root,
    )


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def _read_request(source: TextIO) -> dict[str, Any]:
    value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    return value


def _review_request(value: dict[str, Any]) -> ReviewRequest:
    raw_attachments = value.get("attachments")
    if raw_attachments is None:
        raw_attachments = []
    if not isinstance(raw_attachments, list):
        raise ValueError("attachments must be an array")
    pending: list[tuple[dict[str, Any], Path, int]] = []
    total_size = 0
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            raise ValueError("attachment must be an object")
        path = Path(_required_string(raw, "path"))
        try:
            info = path.lstat()
        except OSError as exc:
            raise ValueError("attachment path is invalid") from exc
        if path.is_symlink() or not path.is_file() or info.st_size < 1:
            raise ValueError("attachment path is invalid")
        if info.st_size > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment is too large")
        total_size += info.st_size
        if total_size > MAX_CANDIDATE_BYTES:
            raise ValueError("combined attachments are too large")
        pending.append((raw, path, info.st_size))
    attachments: list[InboundAttachment] = []
    for raw, path, expected_size in pending:
        content = path.read_bytes()
        if len(content) != expected_size:
            raise ValueError("attachment changed while being read")
        attachments.append(
            InboundAttachment(
                filename=str(raw.get("filename") or path.name),
                content=content,
                content_type=str(raw.get("content_type") or "") or None,
            )
        )
    mentioned = value.get("mentioned")
    if not isinstance(mentioned, bool):
        raise ValueError("mentioned must be boolean")
    return ReviewRequest(
        group_id=_required_string(value, "group_id"),
        sender_id=_required_string(value, "sender_id"),
        mentioned=mentioned,
        attachments=tuple(attachments),
    )


def _review_evaluator(
    initial_transcription: object,
    *,
    now: datetime | None,
):
    def evaluate(paths: list[Path] | tuple[Path, ...]) -> ReviewEvaluation:
        evidence_path = paths[0].parent / "transcription.json"
        if initial_transcription is None:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence_digest = _hash_file(evidence_path)
        else:
            evidence = initial_transcription
            evidence_digest = ""
        if not isinstance(evidence, dict):
            raise ValueError("transcription must be an object")
        transcription = RawTranscription.from_dict(evidence)
        file_set_hash, _ = hash_file_set(paths)
        roster = normalize_transcription(transcription, file_set_hash=file_set_hash)
        validate_roster(roster)
        alerts = tuple(calculate_alerts(roster, now=now))
        return ReviewEvaluation(
            roster=roster,
            summary=format_summary(roster),
            alerts=alerts,
            issues=tuple(roster.issues),
            evidence_digest=evidence_digest,
            evidence=evidence,
        )

    return evaluate


def format_review_reply(result) -> str:
    summary = result.summary.strip() or "No valid flying duties"
    lines = [
        "ROSTER",
        "",
        summary,
        "",
        f"Duties: {result.duty_count}",
        f"Flights: {result.flight_count}",
        f"Future alerts: {result.future_alert_count}",
        "",
    ]
    if result.issues:
        lines.append("NEEDS REVIEW")
        lines.extend(f"- {issue.display()}" for issue in result.issues)
    else:
        lines.append("Needs review: none")
    return "\n".join(lines)


def format_approval_reply(result) -> str:
    next_alert = result.next_alert
    next_display = (
        next_alert.due_utc.astimezone(ZoneInfo("Asia/Singapore")).strftime(
            "%Y-%m-%d %H%M SG"
        )
        if next_alert is not None
        else "none"
    )
    title = (
        "Roster unchanged"
        if result.unchanged
        else "Roster updated"
        if result.updated
        else "Roster activated"
    )
    return "\n".join(
        (
            title,
            "",
            f"Flights: {result.flight_count}",
            f"Future alerts: {result.future_alert_count}",
            f"Next alert: {next_display}",
        )
    )


def format_revision_reply() -> str:
    return "\n".join(
        (
            "Roster not activated.",
            "",
            "Send the corrected/updated roster with:",
            "/run_wife_roster@RosterDemoBot",
        )
    )


def _format_normalization_failure(issues: tuple[ReviewIssue, ...]) -> str:
    lines = ["ROSTER SUMMARY", "", "No valid flying duties", "", "Duties: 0", "Flights: 0", "Future alerts: 0", "", "NEEDS REVIEW"]
    lines.extend(f"- {issue.display()}" for issue in issues)
    return "\n".join(lines)


def _parse_now(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("now must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return parsed


def _required_string(value: Mapping[str, Any], name: str) -> str:
    raw = value.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{name} is required")
    return raw.strip()


def _optional_string(value: Mapping[str, Any], name: str) -> str | None:
    raw = value.get(name)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{name} must be a string")
    return raw.strip()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _emit(destination: TextIO, value: dict[str, object], *, exit_code: int = 0) -> int:
    json.dump(value, destination, sort_keys=True, separators=(",", ":"))
    destination.write("\n")
    return exit_code


def _emit_error(
    destination: TextIO,
    message: str,
    *,
    exit_code: int,
    error_code: str | None = None,
    terminal: bool = False,
) -> int:
    payload: dict[str, object] = {"handled": True, "ok": False, "reply": message}
    if error_code is not None:
        payload["error_code"] = error_code
    if terminal:
        payload["terminal"] = True
    return _emit(
        destination,
        payload,
        exit_code=exit_code,
    )
