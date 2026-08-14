from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Callable, Sequence
from uuid import uuid4

from .database import ApplyResult, RosterDatabase
from .extractor import hash_file_set
from .models import Alert, ReviewIssue, Roster


SUPPORTED_INBOUND_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".pdf": "application/pdf",
}
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_CANDIDATE_BYTES = 40 * 1024 * 1024
STATE_FILENAME = "review.json"
REVIEW_SCHEMA_VERSION = 2
REVIEW_EXPIRY = timedelta(hours=24)
REVIEW_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16}")


class InboundReviewError(RuntimeError):
    """A controlled inbound review or approval failure."""


class InboundAuthorizationError(InboundReviewError):
    pass


class NoAttachmentError(InboundReviewError):
    pass


class UnsupportedAttachmentError(InboundReviewError):
    pass


class NoPendingCandidateError(InboundReviewError):
    pass


class UnresolvedReviewError(InboundReviewError):
    pass


class CandidateMismatchError(InboundReviewError):
    pass


class ReviewInactiveError(InboundReviewError):
    pass


class ReviewExpiredError(ReviewInactiveError):
    pass


class CandidateAlreadyAppliedError(ReviewInactiveError):
    pass


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    filename: str
    content: bytes
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    group_id: str
    sender_id: str
    mentioned: bool
    attachments: tuple[InboundAttachment, ...]


@dataclass(frozen=True, slots=True)
class ReviewEvaluation:
    roster: Roster
    summary: str
    alerts: tuple[Alert, ...]
    issues: tuple[ReviewIssue, ...]
    evidence_digest: str = ""
    evidence: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ReviewResult:
    group_id: str
    sender_id: str
    ingestion_id: str
    ingestion_directory: Path
    source_paths: tuple[Path, ...]
    state_path: Path
    summary: str
    duty_count: int
    flight_count: int
    future_alert_count: int
    issues: tuple[ReviewIssue, ...]
    content_hash: str
    file_set_hash: str
    review_id: str
    expires_at: datetime
    approvable: bool


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    review_id: str
    activated: bool
    updated: bool
    unchanged: bool
    flight_count: int
    future_alert_count: int
    next_alert: Alert | None
    content_hash: str
    apply_result: ApplyResult


@dataclass(frozen=True, slots=True)
class RevisionResult:
    review_id: str
    status: str


Evaluator = Callable[[Sequence[Path]], ReviewEvaluation]


class InboundReviewService:
    """Private review state and deterministic approval gate for one group."""

    def __init__(
        self,
        *,
        inbox_root: str | Path,
        database: RosterDatabase,
        allowed_group_id: str,
        allowed_sender_ids: frozenset[str],
        evaluator: Evaluator,
        allowed_state_change_sender_ids: frozenset[str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.inbox_root = Path(inbox_root)
        self.database = database
        self.allowed_group_id = str(allowed_group_id).strip()
        self.allowed_sender_ids = frozenset(
            str(value).strip() for value in allowed_sender_ids if str(value).strip()
        )
        state_change_ids = (
            allowed_state_change_sender_ids
            if allowed_state_change_sender_ids is not None
            else allowed_sender_ids
        )
        self.allowed_state_change_sender_ids = frozenset(
            str(value).strip() for value in state_change_ids if str(value).strip()
        )
        self.evaluator = evaluator
        self.now = now or (lambda: datetime.now(UTC))
        if (
            not self.allowed_group_id
            or not self.allowed_sender_ids
            or not self.allowed_state_change_sender_ids
        ):
            raise ValueError("inbound group and sender allowlists are required")

    def review(self, request: ReviewRequest) -> ReviewResult | None:
        self._authorize_review(request.group_id, request.sender_id)
        if not request.mentioned:
            return None
        if not request.attachments:
            raise NoAttachmentError("Please attach the roster screenshot or PDF.")
        self._validate_attachments(request.attachments)

        ingestion_id = self._new_ingestion_id()
        review_id = self._new_review_id()
        ingestion_directory = self.inbox_root / ingestion_id
        self._make_private_directory(self.inbox_root)
        ingestion_directory.mkdir(mode=0o700)
        os.chmod(ingestion_directory, 0o700)
        try:
            source_paths = self._save_attachments(
                ingestion_directory,
                request.attachments,
            )
            file_set_hash, files = hash_file_set(source_paths)
            evaluation = self.evaluator(source_paths)
            evaluation.roster.file_set_hash = file_set_hash
            issues = _deduplicate_issues((*evaluation.issues, *evaluation.roster.issues))
            created_at = _aware_utc(self.now())
            expires_at = created_at + REVIEW_EXPIRY
            evidence_digest = evaluation.evidence_digest
            evidence_file: dict[str, object] | None = None
            if evaluation.evidence is not None:
                evidence_path = ingestion_directory / "transcription.json"
                _write_private_json(evidence_path, evaluation.evidence)
                evidence_digest = _sha256_file(evidence_path)
                evidence_file = {
                    "name": evidence_path.name,
                    "sha256": evidence_digest,
                    "size": evidence_path.stat().st_size,
                }
            state = {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "status": "pending",
                "review_id": review_id,
                "ingestion_id": ingestion_id,
                "group_id": request.group_id,
                "sender_id": request.sender_id,
                "created_at": _iso(created_at),
                "expires_at": _iso(expires_at),
                "sources": [
                    {
                        "name": path.name,
                        "sha256": digest,
                        "size": size,
                    }
                    for path, digest, size in files
                ],
                "file_set_hash": file_set_hash,
                "content_hash": evaluation.roster.content_hash,
                "coverage": evaluation.roster.coverage.value,
                "period_start": evaluation.roster.period_start.isoformat(),
                "period_end": evaluation.roster.period_end.isoformat(),
                "candidate_digest": _candidate_digest(evaluation.roster, issues),
                "evidence_digest": evidence_digest,
                "evidence_file": evidence_file,
                "summary": evaluation.summary,
                "duty_count": len(evaluation.roster.duties),
                "flight_count": len(evaluation.roster.sectors),
                "future_alert_count": len(evaluation.alerts),
                "issues": [_issue_to_dict(issue) for issue in issues],
            }
            self._publish_pending(
                request.group_id,
                state_path=ingestion_directory / STATE_FILENAME,
                state=state,
            )
            return ReviewResult(
                group_id=request.group_id,
                sender_id=request.sender_id,
                ingestion_id=ingestion_id,
                ingestion_directory=ingestion_directory,
                source_paths=source_paths,
                state_path=ingestion_directory / STATE_FILENAME,
                summary=evaluation.summary,
                duty_count=len(evaluation.roster.duties),
                flight_count=len(evaluation.roster.sectors),
                future_alert_count=len(evaluation.alerts),
                issues=issues,
                content_hash=evaluation.roster.content_hash,
                file_set_hash=file_set_hash,
                review_id=review_id,
                expires_at=expires_at,
                approvable=not issues,
            )
        except Exception:
            # Keep successfully written review evidence for diagnosis; remove only a
            # directory that never reached a manifest.
            if not (ingestion_directory / STATE_FILENAME).is_file():
                _remove_private_partial(ingestion_directory)
            raise

    def approve(
        self,
        *,
        group_id: str,
        sender_id: str,
        review_id: str | None = None,
        pre_apply_verifier: Callable[[], None] | None = None,
    ) -> ApprovalResult:
        self._authorize_state_change(group_id, sender_id)
        self._make_private_directory(self.inbox_root)
        lock_path = self.inbox_root / ".approval.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            with os.fdopen(descriptor, "r+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                state_path, state = self._resolve_pending_locked(
                    group_id,
                    review_id=review_id,
                )
                try:
                    source_paths, files = self._verify_sources(state_path.parent, state)
                    self._verify_evidence(state_path.parent, state)
                    evaluation = self.evaluator(source_paths)
                    evaluation.roster.file_set_hash = state["file_set_hash"]
                    issues = _deduplicate_issues(
                        (*evaluation.issues, *evaluation.roster.issues)
                    )
                    self._verify_evaluation(state, evaluation, issues)
                except CandidateMismatchError:
                    state["status"] = "superseded"
                    state["superseded_at"] = _iso(_aware_utc(self.now()))
                    _write_private_json(state_path, state)
                    raise
                if issues:
                    raise UnresolvedReviewError(
                        "Roster has unresolved NEEDS REVIEW items."
                    )
                active_before = self.database.active_roster()
                if self.database.has_file_set(str(state["file_set_hash"])) or (
                    active_before is not None
                    and active_before.content_hash == evaluation.roster.content_hash
                ):
                    state["status"] = "superseded"
                    state["superseded_at"] = _iso(_aware_utc(self.now()))
                    _write_private_json(state_path, state)
                    raise CandidateAlreadyAppliedError(
                        "Reviewed candidate has already been applied."
                    )
                if pre_apply_verifier is not None:
                    pre_apply_verifier()
                applied = self.database.apply_roster(
                    evaluation.roster,
                    files,
                    now=_aware_utc(self.now()),
                )
                state["status"] = "approved"
                state["approved_at"] = _iso(_aware_utc(self.now()))
                state["approved_by"] = str(sender_id)
                state["apply_unchanged"] = applied.unchanged
                _write_private_json(state_path, state)
                self._supersede_pending(group_id)
                active_after = self.database.active_roster()
                return ApprovalResult(
                    review_id=str(state["review_id"]),
                    activated=active_before is None and not applied.unchanged,
                    updated=active_before is not None and not applied.unchanged,
                    unchanged=applied.unchanged,
                    flight_count=len(evaluation.roster.sectors),
                    future_alert_count=len(self.database.pending_alerts()),
                    next_alert=self.database.next_pending_alert(),
                    content_hash=(
                        active_after.content_hash
                        if active_after is not None
                        else evaluation.roster.content_hash
                    ),
                    apply_result=applied,
                )
        except Exception:
            # os.fdopen owns the descriptor after successful construction.
            raise

    def revise(
        self,
        *,
        group_id: str,
        sender_id: str,
        review_id: str,
    ) -> RevisionResult:
        """Reject one exact pending review without changing production state."""

        self._authorize_state_change(group_id, sender_id)
        self._make_private_directory(self.inbox_root)
        lock_path = self.inbox_root / ".approval.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state_path, state = self._resolve_pending_locked(
                group_id,
                review_id=review_id,
            )
            state["status"] = "revision_requested"
            state["revision_requested_at"] = _iso(_aware_utc(self.now()))
            state["revision_requested_by"] = str(sender_id)
            _write_private_json(state_path, state)
            return RevisionResult(
                review_id=str(state["review_id"]),
                status="revision_requested",
            )

    def cancel_pending(
        self,
        *,
        group_id: str,
        sender_id: str,
        ingestion_id: str,
    ) -> str:
        """Invalidate one exact reviewed candidate without deleting its audit trail."""

        self._authorize_state_change(group_id, sender_id)
        expected_id = str(ingestion_id).strip()
        if not expected_id:
            raise CandidateMismatchError("Reviewed candidate identity is invalid.")
        self._make_private_directory(self.inbox_root)
        lock_path = self.inbox_root / ".approval.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._expire_pending_locked(group_id)
            pending = self._latest_pending(group_id)
            if pending is None:
                raise NoPendingCandidateError("No roster awaiting approval.")
            state_path, state = pending
            if state.get("ingestion_id") != expected_id:
                raise CandidateMismatchError("Reviewed candidate identity changed.")
            state["status"] = "superseded"
            state["superseded_at"] = _iso(_aware_utc(self.now()))
            _write_private_json(state_path, state)
            return expected_id

    def _authorize_group(self, group_id: str) -> None:
        if str(group_id) != self.allowed_group_id:
            raise InboundAuthorizationError("Inbound group is not authorized.")

    def _authorize_review(self, group_id: str, sender_id: str) -> None:
        self._authorize_group(group_id)
        if str(sender_id) not in self.allowed_sender_ids:
            raise InboundAuthorizationError("Inbound sender is not authorized.")

    def _authorize_state_change(self, group_id: str, sender_id: str) -> None:
        self._authorize_group(group_id)
        if str(sender_id) not in self.allowed_state_change_sender_ids:
            raise InboundAuthorizationError("Inbound sender is not authorized.")

    def _new_ingestion_id(self) -> str:
        timestamp = _aware_utc(self.now()).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{timestamp}-{uuid4().hex}"

    def _new_review_id(self) -> str:
        existing: set[str] = set()
        if self.inbox_root.is_dir():
            for state_path in self.inbox_root.glob(f"*/{STATE_FILENAME}"):
                try:
                    state = _read_state(state_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                value = state.get("review_id")
                if isinstance(value, str):
                    existing.add(value)
        for _ in range(10):
            candidate = secrets.token_urlsafe(12)
            if REVIEW_ID_PATTERN.fullmatch(candidate) and candidate not in existing:
                return candidate
        raise InboundReviewError("Could not allocate a roster review identity.")

    @staticmethod
    def _make_private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)

    @staticmethod
    def _validate_attachments(attachments: Sequence[InboundAttachment]) -> None:
        total = 0
        for value in attachments:
            suffix = Path(value.filename).suffix.lower()
            expected_type = SUPPORTED_INBOUND_TYPES.get(suffix)
            if expected_type is None:
                raise UnsupportedAttachmentError(
                    "Supported roster files are PDF, PNG, JPG, and JPEG."
                )
            content_type = (value.content_type or "").split(";", 1)[0].strip().lower()
            if content_type and content_type != expected_type:
                raise UnsupportedAttachmentError("Roster attachment type does not match its filename.")
            size = len(value.content)
            if size == 0 or size > MAX_ATTACHMENT_BYTES:
                raise UnsupportedAttachmentError("Roster attachment size is invalid.")
            total += size
        if total > MAX_CANDIDATE_BYTES:
            raise UnsupportedAttachmentError("Combined roster attachments are too large.")

    @staticmethod
    def _save_attachments(
        directory: Path,
        attachments: Sequence[InboundAttachment],
    ) -> tuple[Path, ...]:
        paths: list[Path] = []
        for index, value in enumerate(attachments, start=1):
            suffix = Path(value.filename).suffix.lower()
            path = directory / f"source-{index:03d}{suffix}"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, 0o600)
            paths.append(path)
        return tuple(paths)

    def _latest_pending(self, group_id: str) -> tuple[Path, dict[str, object]] | None:
        candidates: list[tuple[str, Path, dict[str, object]]] = []
        for state_path in self.inbox_root.glob(f"*/{STATE_FILENAME}"):
            try:
                state = _read_state(state_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if state.get("group_id") != group_id or state.get("status") != "pending":
                continue
            created_at = state.get("created_at")
            if isinstance(created_at, str):
                candidates.append((created_at, state_path, state))
        if not candidates:
            return None
        _, state_path, state = max(candidates, key=lambda item: (item[0], str(item[1])))
        return state_path, state

    def _resolve_pending_locked(
        self,
        group_id: str,
        *,
        review_id: str | None,
    ) -> tuple[Path, dict[str, object]]:
        self._expire_pending_locked(group_id)
        if review_id is None:
            pending = self._latest_pending(group_id)
            if pending is None:
                raise NoPendingCandidateError("No roster awaiting approval.")
            return pending

        expected_id = str(review_id).strip()
        if not REVIEW_ID_PATTERN.fullmatch(expected_id):
            raise ReviewInactiveError("Roster review is not active.")
        matches: list[tuple[Path, dict[str, object]]] = []
        for state_path in self.inbox_root.glob(f"*/{STATE_FILENAME}"):
            try:
                state = _read_state(state_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                state.get("group_id") == group_id
                and state.get("review_id") == expected_id
            ):
                matches.append((state_path, state))
        if len(matches) != 1:
            raise ReviewInactiveError("Roster review is not active.")
        state_path, state = matches[0]
        status = state.get("status")
        if status == "expired":
            raise ReviewExpiredError("Roster review has expired.")
        if status != "pending":
            raise ReviewInactiveError("Roster review is not active.")
        current = self._latest_pending(group_id)
        if current is None or current[1].get("review_id") != expected_id:
            state["status"] = "superseded"
            state["superseded_at"] = _iso(_aware_utc(self.now()))
            _write_private_json(state_path, state)
            raise ReviewInactiveError("Roster review is not active.")
        return state_path, state

    def _expire_pending_locked(self, group_id: str) -> None:
        current = _aware_utc(self.now())
        for state_path in self.inbox_root.glob(f"*/{STATE_FILENAME}"):
            try:
                state = _read_state(state_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if state.get("group_id") != group_id or state.get("status") != "pending":
                continue
            expires_at = state.get("expires_at")
            try:
                expires = _parse_iso(expires_at)
            except ValueError:
                expires = current
            if expires > current:
                continue
            state["status"] = "expired"
            state["expired_at"] = _iso(current)
            _write_private_json(state_path, state)

    def _publish_pending(
        self,
        group_id: str,
        *,
        state_path: Path,
        state: dict[str, object],
    ) -> None:
        lock_path = self.inbox_root / ".approval.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._expire_pending_locked(group_id)
            _write_private_json(state_path, state)
            for candidate_path in self.inbox_root.glob(f"*/{STATE_FILENAME}"):
                try:
                    candidate = _read_state(candidate_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if candidate.get("group_id") != group_id or candidate.get("status") != "pending":
                    continue
                if candidate_path == state_path:
                    continue
                candidate["status"] = "superseded"
                candidate["superseded_at"] = _iso(_aware_utc(self.now()))
                _write_private_json(candidate_path, candidate)

    def _supersede_pending(self, group_id: str) -> None:
        for state_path in self.inbox_root.glob(f"*/{STATE_FILENAME}"):
            try:
                state = _read_state(state_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if state.get("group_id") != group_id or state.get("status") != "pending":
                continue
            state["status"] = "superseded"
            state["superseded_at"] = _iso(_aware_utc(self.now()))
            _write_private_json(state_path, state)

    @staticmethod
    def _verify_sources(
        ingestion_directory: Path,
        state: dict[str, object],
    ) -> tuple[tuple[Path, ...], list[tuple[Path, str, int]]]:
        raw_sources = state.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise CandidateMismatchError("Reviewed candidate source metadata is invalid.")
        source_paths: list[Path] = []
        expected: list[tuple[str, int]] = []
        root = ingestion_directory.resolve()
        for raw in raw_sources:
            if not isinstance(raw, dict):
                raise CandidateMismatchError("Reviewed candidate source metadata is invalid.")
            name = raw.get("name")
            digest = raw.get("sha256")
            size = raw.get("size")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"source-\d{3}\.(?:jpg|jpeg|png|pdf)", name)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(size, int)
                or size <= 0
            ):
                raise CandidateMismatchError("Reviewed candidate source metadata is invalid.")
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
                raise CandidateMismatchError("Reviewed candidate source is unavailable.")
            source_paths.append(path)
            expected.append((digest, size))
        file_set_hash, files = hash_file_set(source_paths)
        actual = [(digest, size) for _, digest, size in files]
        if actual != expected or file_set_hash != state.get("file_set_hash"):
            raise CandidateMismatchError("Reviewed candidate sources have changed.")
        return tuple(source_paths), files

    @staticmethod
    def _verify_evidence(
        ingestion_directory: Path,
        state: dict[str, object],
    ) -> None:
        evidence = state.get("evidence_file")
        if evidence is None:
            return
        if not isinstance(evidence, dict):
            raise CandidateMismatchError("Reviewed transcription metadata is invalid.")
        name = evidence.get("name")
        digest = evidence.get("sha256")
        size = evidence.get("size")
        if (
            name != "transcription.json"
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise CandidateMismatchError("Reviewed transcription metadata is invalid.")
        root = ingestion_directory.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
            raise CandidateMismatchError("Reviewed transcription is unavailable.")
        if path.stat().st_size != size or _sha256_file(path) != digest:
            raise CandidateMismatchError("Reviewed transcription has changed.")

    @staticmethod
    def _verify_evaluation(
        state: dict[str, object],
        evaluation: ReviewEvaluation,
        issues: tuple[ReviewIssue, ...],
    ) -> None:
        roster = evaluation.roster
        checks = {
            "content_hash": roster.content_hash,
            "coverage": roster.coverage.value,
            "period_start": roster.period_start.isoformat(),
            "period_end": roster.period_end.isoformat(),
            "candidate_digest": _candidate_digest(roster, issues),
            "evidence_digest": evaluation.evidence_digest,
        }
        if any(state.get(key) != value for key, value in checks.items()):
            raise CandidateMismatchError("Reviewed candidate no longer matches approval state.")


def _deduplicate_issues(issues: Sequence[ReviewIssue]) -> tuple[ReviewIssue, ...]:
    unique: dict[tuple[object, ...], ReviewIssue] = {}
    for issue in issues:
        key = (
            issue.code,
            issue.message,
            issue.source_position,
            issue.duty_id,
            issue.sector_id,
        )
        unique[key] = issue
    return tuple(unique.values())


def _candidate_digest(roster: Roster, issues: Sequence[ReviewIssue]) -> str:
    payload = {
        "canonical": roster.canonical(),
        "port_local_notice_present": roster.port_local_notice_present,
        "issues": [_issue_to_dict(issue) for issue in issues],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _issue_to_dict(issue: ReviewIssue) -> dict[str, object]:
    return {
        "code": issue.code,
        "message": issue.message,
        "source_position": issue.source_position,
        "duty_id": issue.duty_id,
        "sector_id": issue.sector_id,
    }


def _read_state(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") not in {1, 2}:
        raise ValueError("invalid review state")
    return value


def _write_private_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_private_partial(directory: Path) -> None:
    if not directory.is_dir():
        return
    for child in directory.iterdir():
        if child.is_file() and not child.is_symlink():
            child.unlink()
    try:
        directory.rmdir()
    except OSError:
        pass


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware_utc(parsed)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
