from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence

from .alerts import calculate_alerts
from .diff import diff_rosters, merge_rosters
from .models import (
    Alert,
    AlertStatus,
    AlertType,
    Airport,
    Coverage,
    Duty,
    EventTime,
    ReviewIssue,
    Roster,
    RosterDiff,
    Sector,
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ingestions (
    id INTEGER PRIMARY KEY,
    file_set_hash TEXT NOT NULL UNIQUE,
    input_classification TEXT NOT NULL CHECK (input_classification IN ('FULL','PARTIAL','UNCERTAIN')),
    source_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_files (
    id INTEGER PRIMARY KEY,
    ingestion_id INTEGER NOT NULL REFERENCES ingestions(id),
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    UNIQUE (ingestion_id, sha256, byte_size)
);

CREATE TABLE IF NOT EXISTS roster_versions (
    id INTEGER PRIMARY KEY,
    ingestion_id INTEGER REFERENCES ingestions(id),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate','active','superseded')),
    normalized_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_roster_versions_status ON roster_versions(status);
CREATE INDEX IF NOT EXISTS idx_roster_versions_hash ON roster_versions(content_hash);

CREATE TABLE IF NOT EXISTS duties (
    id INTEGER PRIMARY KEY,
    roster_version_id INTEGER NOT NULL REFERENCES roster_versions(id),
    duty_id TEXT NOT NULL,
    reporting_airport TEXT,
    rpt_port_local TEXT,
    rpt_timezone TEXT,
    rpt_singapore TEXT,
    rpt_utc TEXT,
    UNIQUE (roster_version_id, duty_id)
);

CREATE TABLE IF NOT EXISTS sectors (
    id INTEGER PRIMARY KEY,
    roster_version_id INTEGER NOT NULL REFERENCES roster_versions(id),
    duty_row_id INTEGER NOT NULL REFERENCES duties(id),
    sector_id TEXT NOT NULL,
    flight_number TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    std_port_local TEXT,
    std_timezone TEXT,
    std_singapore TEXT,
    std_utc TEXT,
    sta_port_local TEXT,
    sta_timezone TEXT,
    sta_singapore TEXT,
    sta_utc TEXT,
    UNIQUE (roster_version_id, sector_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    roster_version_id INTEGER REFERENCES roster_versions(id),
    event_key TEXT NOT NULL UNIQUE,
    alert_type TEXT NOT NULL,
    due_utc TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','sending','sent','cancelled','superseded','missed','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    schedule_generation INTEGER NOT NULL DEFAULT 0,
    retry_not_before TEXT,
    sent_at TEXT,
    notification_id TEXT,
    telegram_message_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_due_status ON alerts(status, due_utc);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL REFERENCES alerts(event_key),
    schedule_generation INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('sending','sent','failed','abandoned')),
    notification_id TEXT,
    error TEXT,
    UNIQUE (event_key, schedule_generation, attempt_number)
);

CREATE TABLE IF NOT EXISTS workflow_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MAX_DELIVERY_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ApplyResult:
    unchanged: bool
    reason: str
    version_id: int | None
    roster_diff: RosterDiff
    preserved_alerts: int = 0
    created_alerts: int = 0
    superseded_alerts: int = 0


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    recovered: int = 0
    missed: int = 0


class RosterDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Never change a caller-owned broad parent such as /tmp. Production
        # directory hardening belongs to Deployer; a newly created DB directory
        # is private from birth.
        if not parent_existed:
            self.path.parent.chmod(0o700)
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)
        self.path.chmod(0o600)

    def exists(self) -> bool:
        return self.path.is_file()

    def has_file_set(self, file_set_hash: str) -> bool:
        if not self.exists():
            return False
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT 1 FROM ingestions WHERE file_set_hash = ?",
                (file_set_hash,),
            ).fetchone()
        return row is not None

    def active_roster(self) -> Roster | None:
        if not self.exists():
            return None
        with closing(self._connect()) as connection, connection:
            return self._active_roster(connection)

    def latest_two_rosters(self) -> tuple[Roster | None, Roster | None]:
        if not self.exists():
            return None, None
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT normalized_json FROM roster_versions "
                "ORDER BY created_at DESC, id DESC LIMIT 2"
            ).fetchall()
        latest = _roster_from_dict(json.loads(rows[0][0])) if rows else None
        previous = _roster_from_dict(json.loads(rows[1][0])) if len(rows) > 1 else None
        return previous, latest

    def pending_alerts(self) -> list[Alert]:
        if not self.exists():
            return []
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT event_key, alert_type, due_utc, message, status, attempt_count, "
                "schedule_generation "
                "FROM alerts WHERE status = 'pending' ORDER BY due_utc, event_key"
            ).fetchall()
        return [
            Alert(
                event_key=row[0],
                alert_type=AlertType(row[1]),
                due_utc=_datetime(row[2]),
                message=row[3],
                status=AlertStatus(row[4]),
                attempt_count=row[5],
                schedule_generation=row[6],
            )
            for row in rows
        ]

    def enqueue_alert(self, alert: Alert, *, created_at: datetime | None = None) -> None:
        """Persist an already-calculated alert; event_key remains the idempotency guard."""
        self.initialize()
        timestamp = _require_aware(created_at or datetime.now(UTC), "created_at")
        with closing(self._connect()) as connection, connection:
            self._insert_alert(connection, None, alert, timestamp)

    def claim_due_alerts(self, now: datetime, *, limit: int = 100) -> list[Alert]:
        """Atomically move eligible alerts from pending to sending."""
        current = _require_aware(now, "now").astimezone(UTC)
        if limit < 1:
            raise ValueError("limit must be positive")
        self.initialize()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT event_key,alert_type,due_utc,message,attempt_count,schedule_generation "
                "FROM alerts WHERE status='pending' AND attempt_count<? AND due_utc<=? "
                "ORDER BY due_utc,event_key LIMIT ?",
                (MAX_DELIVERY_ATTEMPTS, _iso(current), limit),
            ).fetchall()
            keys = [row[0] for row in rows]
            if keys:
                connection.executemany(
                    "UPDATE alerts SET status='sending',attempt_count=attempt_count+1,"
                    "retry_not_before=NULL,updated_at=? "
                    "WHERE event_key=? AND status='pending'",
                    [(_iso(current), key) for key in keys],
                )
                connection.executemany(
                    "INSERT INTO delivery_attempts "
                    "(event_key,schedule_generation,attempt_number,started_at,outcome) "
                    "VALUES (?,?,?,?,'sending')",
                    [(row[0], row[5], row[4] + 1, _iso(current)) for row in rows],
                )
            connection.commit()
        return [
            Alert(
                event_key=row[0],
                alert_type=AlertType(row[1]),
                due_utc=_datetime(row[2]),
                message=row[3],
                status=AlertStatus.SENDING,
                attempt_count=row[4] + 1,
                schedule_generation=row[5],
            )
            for row in rows
        ]

    def recover_alerts(
        self,
        now: datetime,
        *,
        grace_periods: dict[AlertType, timedelta],
        sending_lease: timedelta,
    ) -> RecoveryResult:
        """Recover retryable alerts and mark stale alerts missed in one transaction."""
        current = _require_aware(now, "now").astimezone(UTC)
        self.initialize()
        recovered = 0
        missed = 0
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT event_key,alert_type,due_utc,status,updated_at,retry_not_before,"
                "attempt_count,schedule_generation FROM alerts "
                "WHERE status IN ('pending','failed','sending') AND due_utc<=?",
                (_iso(current),),
            ).fetchall()
            for (
                event_key,
                raw_type,
                raw_due,
                status,
                raw_updated,
                raw_retry,
                attempts,
                generation,
            ) in rows:
                alert_type = AlertType(raw_type)
                due = _datetime(raw_due).astimezone(UTC)
                deadline = due + grace_periods[alert_type]
                if current > deadline:
                    connection.execute(
                        "UPDATE alerts SET status='missed',retry_not_before=NULL,updated_at=?,"
                        "last_error=? WHERE event_key=?",
                        (_iso(current), "alert exceeded recovery grace period", event_key),
                    )
                    if status == AlertStatus.SENDING.value:
                        self._finish_attempt(
                            connection,
                            event_key,
                            generation,
                            attempts,
                            current,
                            "abandoned",
                            error="sending lease expired after recovery grace period",
                        )
                    missed += 1
                    continue
                should_recover = (
                    status == AlertStatus.FAILED.value
                    and raw_retry is not None
                    and _datetime(raw_retry).astimezone(UTC) <= current
                )
                if status == AlertStatus.SENDING.value:
                    updated = _datetime(raw_updated).astimezone(UTC)
                    should_recover = current - updated >= sending_lease
                    if should_recover:
                        self._finish_attempt(
                            connection,
                            event_key,
                            generation,
                            attempts,
                            current,
                            "abandoned",
                            error="sending lease expired",
                        )
                        if attempts >= MAX_DELIVERY_ATTEMPTS:
                            connection.execute(
                                "UPDATE alerts SET status='failed',retry_not_before=NULL,"
                                "updated_at=?,last_error=? WHERE event_key=?",
                                (_iso(current), "delivery attempts exhausted", event_key),
                            )
                            should_recover = False
                if should_recover:
                    connection.execute(
                        "UPDATE alerts SET status='pending',retry_not_before=NULL,updated_at=? "
                        "WHERE event_key=?",
                        (_iso(current), event_key),
                    )
                    recovered += 1
            connection.commit()
        return RecoveryResult(recovered=recovered, missed=missed)

    def mark_dispatch_sent(
        self,
        event_key: str,
        *,
        sent_at: datetime,
        notification_id: str,
    ) -> bool:
        timestamp = _require_aware(sent_at, "sent_at").astimezone(UTC)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE alerts SET status='sent',sent_at=?,notification_id=?,last_error=NULL,"
                "retry_not_before=NULL,updated_at=? "
                "WHERE event_key=? AND status='sending'",
                (_iso(timestamp), notification_id, _iso(timestamp), event_key),
            )
            if cursor.rowcount == 1:
                row = connection.execute(
                    "SELECT attempt_count,schedule_generation FROM alerts WHERE event_key=?",
                    (event_key,),
                ).fetchone()
                self._finish_attempt(
                    connection,
                    event_key,
                    row[1],
                    row[0],
                    timestamp,
                    "sent",
                    notification_id=notification_id,
                )
        return cursor.rowcount == 1

    def mark_dispatch_failed(
        self,
        event_key: str,
        *,
        failed_at: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> bool:
        timestamp = _require_aware(failed_at, "failed_at").astimezone(UTC)
        retry = _require_aware(retry_at, "retry_at").astimezone(UTC) if retry_at else None
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE alerts SET status='failed',last_error=?,retry_not_before=?,updated_at=? "
                "WHERE event_key=? AND status='sending'",
                (error[:2000], _iso(retry) if retry else None, _iso(timestamp), event_key),
            )
            if cursor.rowcount == 1:
                row = connection.execute(
                    "SELECT attempt_count,schedule_generation FROM alerts WHERE event_key=?",
                    (event_key,),
                ).fetchone()
                self._finish_attempt(
                    connection,
                    event_key,
                    row[1],
                    row[0],
                    timestamp,
                    "failed",
                    error=error[:2000],
                )
        return cursor.rowcount == 1

    def record_last_dispatch(self, timestamp: datetime) -> None:
        current = _require_aware(timestamp, "timestamp").astimezone(UTC)
        self.initialize()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO workflow_state(key,value) VALUES('last_dispatch',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_iso(current),),
            )

    def last_dispatch(self) -> datetime | None:
        if not self.exists():
            return None
        self.initialize()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT value FROM workflow_state WHERE key='last_dispatch'"
            ).fetchone()
        return _datetime(row[0]) if row else None

    def next_pending_alert(self) -> Alert | None:
        if not self.exists():
            return None
        self.initialize()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT event_key,alert_type,due_utc,message,status FROM alerts "
                "WHERE status='pending' ORDER BY due_utc,event_key LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return Alert(
            event_key=row[0],
            alert_type=AlertType(row[1]),
            due_utc=_datetime(row[2]),
            message=row[3],
            status=AlertStatus(row[4]),
        )

    def next_dispatch_at(self, now: datetime, *, sending_lease: timedelta) -> datetime | None:
        """Return the next database-driven wake time for the one-shot dispatcher."""
        current = _require_aware(now, "now").astimezone(UTC)
        self.initialize()
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT status,due_utc,retry_not_before,updated_at FROM alerts "
                "WHERE status IN ('pending','failed','sending')"
            ).fetchall()
        candidates: list[datetime] = []
        for status, raw_due, raw_retry, raw_updated in rows:
            if status == AlertStatus.PENDING.value:
                candidates.append(max(current, _datetime(raw_due).astimezone(UTC)))
            elif status == AlertStatus.FAILED.value and raw_retry:
                candidates.append(max(current, _datetime(raw_retry).astimezone(UTC)))
            elif status == AlertStatus.SENDING.value:
                lease_end = _datetime(raw_updated).astimezone(UTC) + sending_lease
                candidates.append(max(current, lease_end))
        return min(candidates) if candidates else None

    def schedule_state(self) -> dict[str, int | str | None]:
        self.initialize()
        with closing(self._connect()) as connection, connection:
            values = dict(
                connection.execute(
                    "SELECT key,value FROM workflow_state WHERE key IN "
                    "('schedule_generation','planned_schedule_generation','planned_for_utc')"
                ).fetchall()
            )
        return {
            "schedule_generation": int(values.get("schedule_generation", "0")),
            "planned_schedule_generation": int(
                values.get("planned_schedule_generation", "0")
            ),
            "planned_for_utc": values.get("planned_for_utc") or None,
        }

    def record_planned_schedule(self, generation: int, planned_for: datetime | None) -> None:
        value = _iso(_require_aware(planned_for, "planned_for").astimezone(UTC)) if planned_for else ""
        self.initialize()
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT INTO workflow_state(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    ("planned_schedule_generation", str(generation)),
                    ("planned_for_utc", value),
                ),
            )

    def is_current_claim(self, alert: Alert) -> bool:
        """Fail closed when an edit superseded a claim before network delivery."""
        if not self.exists():
            return False
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT status,schedule_generation FROM alerts WHERE event_key=?",
                (alert.event_key,),
            ).fetchone()
        return row == (AlertStatus.SENDING.value, alert.schedule_generation)

    def attempt_history(self, event_key: str | None = None) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        self.initialize()
        with closing(self._connect()) as connection, connection:
            connection.row_factory = sqlite3.Row
            if event_key is None:
                rows = connection.execute(
                    "SELECT * FROM delivery_attempts ORDER BY id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM delivery_attempts WHERE event_key=? ORDER BY attempt_number",
                    (event_key,),
                ).fetchall()
        return [dict(row) for row in rows]

    def alert_record(self, event_key: str) -> dict[str, Any] | None:
        if not self.exists():
            return None
        self.initialize()
        with closing(self._connect()) as connection, connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM alerts WHERE event_key=?",
                (event_key,),
            ).fetchone()
        return dict(row) if row else None

    def status_counts(self) -> dict[str, int | str | None]:
        if not self.exists():
            return {
                "active_content_hash": None,
                "roster_versions": 0,
                "ingestions": 0,
                "pending_alerts": 0,
                "sent_alerts": 0,
                "schedule_generation": 0,
                "planned_schedule_generation": 0,
                "planned_for_utc": None,
                "delivery_attempts": 0,
                "failed_attempts": 0,
                "last_successful_delivery": None,
            }
        with closing(self._connect()) as connection, connection:
            active = connection.execute(
                "SELECT content_hash FROM roster_versions WHERE status = 'active' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            alert_counts = dict(
                connection.execute("SELECT status,COUNT(*) FROM alerts GROUP BY status").fetchall()
            )
            state = dict(connection.execute("SELECT key,value FROM workflow_state").fetchall())
            counts = {
                "active_content_hash": active[0] if active else None,
                "roster_versions": connection.execute("SELECT COUNT(*) FROM roster_versions").fetchone()[0],
                "ingestions": connection.execute("SELECT COUNT(*) FROM ingestions").fetchone()[0],
                **{f"{name}_alerts": alert_counts.get(name, 0) for name in AlertStatus},
                "schedule_generation": int(state.get("schedule_generation", "0")),
                "planned_schedule_generation": int(
                    state.get("planned_schedule_generation", "0")
                ),
                "planned_for_utc": state.get("planned_for_utc") or None,
                "delivery_attempts": connection.execute(
                    "SELECT COUNT(*) FROM delivery_attempts"
                ).fetchone()[0],
                "failed_attempts": connection.execute(
                    "SELECT COUNT(*) FROM delivery_attempts WHERE outcome IN ('failed','abandoned')"
                ).fetchone()[0],
                "last_successful_delivery": connection.execute(
                    "SELECT MAX(value) FROM ("
                    "SELECT finished_at AS value FROM delivery_attempts WHERE outcome='sent' "
                    "UNION ALL SELECT sent_at AS value FROM alerts WHERE status='sent'"
                    ")"
                ).fetchone()[0],
            }
        return counts

    def apply_roster(
        self,
        candidate: Roster,
        files: Sequence[tuple[Path, str, int]],
        *,
        now: datetime | None = None,
    ) -> ApplyResult:
        """Transactional reconciliation for a later explicit-approval wrapper.

        Stage 1's public CLI never calls this method.
        """
        self.initialize()
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM ingestions WHERE file_set_hash = ?",
                (candidate.file_set_hash,),
            ).fetchone():
                connection.rollback()
                return ApplyResult(True, "exact file set already processed", None, RosterDiff())
            active = self._active_roster(connection)
            roster_diff = diff_rosters(active, candidate)
            effective = merge_rosters(active, candidate)
            ingestion_id = self._insert_ingestion(connection, candidate, files, current, "processed")
            if active is not None and effective.content_hash == active.content_hash:
                connection.execute(
                    "UPDATE ingestions SET status = 'unchanged' WHERE id = ?",
                    (ingestion_id,),
                )
                connection.commit()
                return ApplyResult(True, "normalized content unchanged", None, RosterDiff())

            connection.execute(
                "UPDATE roster_versions SET status = 'superseded' WHERE status = 'active'"
            )
            version_id = self._insert_version(connection, effective, ingestion_id, current)
            generation = self._bump_schedule_generation(connection)
            desired = calculate_alerts(effective, now=current)
            desired_keys = {alert.event_key for alert in desired}
            active_rows = connection.execute(
                "SELECT event_key,status,attempt_count,schedule_generation FROM alerts"
            ).fetchall()
            existing = {row[0]: (row[1], row[2], row[3]) for row in active_rows}
            preserved = 0
            created = 0
            for alert in desired:
                record = existing.get(alert.event_key)
                if record is None:
                    self._insert_alert(
                        connection, version_id, alert, current, generation=generation
                    )
                    created += 1
                    continue
                status, attempts, _old_generation = record
                if status in (AlertStatus.SUPERSEDED.value, AlertStatus.CANCELLED.value):
                    connection.execute(
                        "UPDATE alerts SET roster_version_id=?, due_utc=?, message=?, status='pending', "
                        "schedule_generation=?,attempt_count=0,retry_not_before=NULL,sent_at=NULL,"
                        "notification_id=NULL,telegram_message_id=NULL,updated_at=?,last_error=NULL "
                        "WHERE event_key=?",
                        (
                            version_id,
                            _iso(alert.due_utc),
                            alert.message,
                            generation,
                            _iso(current),
                            alert.event_key,
                        ),
                    )
                else:
                    # An unchanged event already crossing the network boundary
                    # must retain its claim. Requeueing it here could duplicate
                    # a Telegram message that succeeded just before the edit.
                    if status == AlertStatus.SENDING.value:
                        preserved += 1
                        continue
                    new_status = (
                        AlertStatus.PENDING.value
                        if status == AlertStatus.FAILED.value
                        else status
                    )
                    reset_attempts = (
                        0 if status == AlertStatus.FAILED.value else attempts
                    )
                    connection.execute(
                        "UPDATE alerts SET roster_version_id=?,due_utc=?,message=?,status=?,"
                        "schedule_generation=?,attempt_count=?,retry_not_before=NULL,updated_at=?,"
                        "last_error=NULL WHERE event_key=?",
                        (
                            version_id,
                            _iso(alert.due_utc),
                            alert.message,
                            new_status,
                            generation,
                            reset_attempts,
                            _iso(current),
                            alert.event_key,
                        ),
                    )
                preserved += 1
            obsolete = [
                key
                for key, (status, _, _) in existing.items()
                if status in {
                    AlertStatus.PENDING.value,
                    AlertStatus.FAILED.value,
                    AlertStatus.SENDING.value,
                }
                and key not in desired_keys
            ]
            if obsolete:
                sending = connection.execute(
                    "SELECT event_key,schedule_generation,attempt_count FROM alerts "
                    "WHERE status='sending' AND "
                    f"event_key IN ({','.join('?' for _ in obsolete)})",
                    obsolete,
                ).fetchall()
                for event_key, sending_generation, attempt_number in sending:
                    self._finish_attempt(
                        connection,
                        event_key,
                        sending_generation,
                        attempt_number,
                        current,
                        "abandoned",
                        error="superseded by an approved roster edit",
                    )
                connection.executemany(
                    "UPDATE alerts SET status='superseded',retry_not_before=NULL,updated_at=?,"
                    "last_error='superseded by an approved roster edit' WHERE event_key=?",
                    [(_iso(current), key) for key in obsolete],
                )
            connection.commit()
            return ApplyResult(
                False,
                "roster activated",
                version_id,
                roster_diff,
                preserved_alerts=preserved,
                created_alerts=created,
                superseded_alerts=len(obsolete),
            )

    def mark_alert_sent(
        self,
        event_key: str,
        *,
        sent_at: datetime,
        telegram_message_id: str | None = None,
    ) -> None:
        if sent_at.tzinfo is None:
            raise ValueError("sent_at must be timezone-aware")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE alerts SET status='sent', sent_at=?, telegram_message_id=?, updated_at=? "
                "WHERE event_key=?",
                (_iso(sent_at), telegram_message_id, _iso(sent_at), event_key),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(alerts)")}
        if "notification_id" not in columns:
            connection.execute("ALTER TABLE alerts ADD COLUMN notification_id TEXT")
        if "schedule_generation" not in columns:
            connection.execute(
                "ALTER TABLE alerts ADD COLUMN schedule_generation INTEGER NOT NULL DEFAULT 0"
            )
        if "retry_not_before" not in columns:
            connection.execute("ALTER TABLE alerts ADD COLUMN retry_not_before TEXT")
        attempt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(delivery_attempts)")
        }
        if attempt_columns and "schedule_generation" not in attempt_columns:
            connection.executescript(
                """
                ALTER TABLE delivery_attempts RENAME TO delivery_attempts_legacy;
                CREATE TABLE delivery_attempts (
                    id INTEGER PRIMARY KEY,
                    event_key TEXT NOT NULL REFERENCES alerts(event_key),
                    schedule_generation INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('sending','sent','failed','abandoned')
                    ),
                    notification_id TEXT,
                    error TEXT,
                    UNIQUE (event_key, schedule_generation, attempt_number)
                );
                INSERT INTO delivery_attempts
                    (id,event_key,schedule_generation,attempt_number,started_at,
                     finished_at,outcome,notification_id,error)
                SELECT id,event_key,0,attempt_number,started_at,finished_at,outcome,
                       notification_id,error
                FROM delivery_attempts_legacy;
                DROP TABLE delivery_attempts_legacy;
                CREATE INDEX IF NOT EXISTS idx_delivery_attempts_event
                ON delivery_attempts(event_key, schedule_generation, attempt_number);
                """
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_attempts_event "
            "ON delivery_attempts(event_key,schedule_generation,attempt_number)"
        )

    @staticmethod
    def _bump_schedule_generation(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM workflow_state WHERE key='schedule_generation'"
        ).fetchone()
        generation = int(row[0]) + 1 if row else 1
        connection.execute(
            "INSERT INTO workflow_state(key,value) VALUES('schedule_generation',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(generation),),
        )
        return generation

    @staticmethod
    def _finish_attempt(
        connection: sqlite3.Connection,
        event_key: str,
        schedule_generation: int,
        attempt_number: int,
        finished_at: datetime,
        outcome: str,
        *,
        notification_id: str | None = None,
        error: str | None = None,
    ) -> None:
        connection.execute(
            "UPDATE delivery_attempts SET finished_at=?,outcome=?,notification_id=?,error=? "
            "WHERE event_key=? AND schedule_generation=? AND attempt_number=? "
            "AND outcome='sending'",
            (
                _iso(finished_at),
                outcome,
                notification_id,
                error,
                event_key,
                schedule_generation,
                attempt_number,
            ),
        )

    def _active_roster(self, connection: sqlite3.Connection) -> Roster | None:
        row = connection.execute(
            "SELECT normalized_json FROM roster_versions WHERE status = 'active' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _roster_from_dict(json.loads(row[0])) if row else None

    @staticmethod
    def _insert_ingestion(
        connection: sqlite3.Connection,
        roster: Roster,
        files: Sequence[tuple[Path, str, int]],
        now: datetime,
        status: str,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO ingestions "
            "(file_set_hash,input_classification,source_count,created_at,status) VALUES (?,?,?,?,?)",
            (roster.file_set_hash, roster.coverage.value, len(files), _iso(now), status),
        )
        ingestion_id = int(cursor.lastrowid)
        connection.executemany(
            "INSERT INTO ingestion_files (ingestion_id,source_path,sha256,byte_size) VALUES (?,?,?,?)",
            [(ingestion_id, str(path), digest, size) for path, digest, size in files],
        )
        return ingestion_id

    @staticmethod
    def _insert_version(
        connection: sqlite3.Connection,
        roster: Roster,
        ingestion_id: int,
        now: datetime,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO roster_versions "
            "(ingestion_id,period_start,period_end,content_hash,created_at,status,normalized_json) "
            "VALUES (?,?,?,?,?,'active',?)",
            (
                ingestion_id,
                roster.period_start.isoformat(),
                roster.period_end.isoformat(),
                roster.content_hash,
                _iso(now),
                json.dumps(_roster_to_dict(roster), sort_keys=True, separators=(",", ":")),
            ),
        )
        version_id = int(cursor.lastrowid)
        for duty in roster.duties:
            duty_cursor = connection.execute(
                "INSERT INTO duties "
                "(roster_version_id,duty_id,reporting_airport,rpt_port_local,rpt_timezone,rpt_singapore,rpt_utc) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    version_id,
                    duty.duty_id,
                    duty.reporting_iata,
                    _event_value(duty.rpt, "port"),
                    duty.rpt.port_timezone if duty.rpt else None,
                    _event_value(duty.rpt, "sg"),
                    _event_value(duty.rpt, "utc"),
                ),
            )
            duty_row_id = int(duty_cursor.lastrowid)
            for sector in duty.sectors:
                connection.execute(
                    "INSERT INTO sectors "
                    "(roster_version_id,duty_row_id,sector_id,flight_number,origin,destination,"
                    "std_port_local,std_timezone,std_singapore,std_utc,sta_port_local,sta_timezone,sta_singapore,sta_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        version_id,
                        duty_row_id,
                        sector.sector_id,
                        sector.flight_number,
                        sector.origin_iata,
                        sector.destination_iata,
                        _event_value(sector.std, "port"),
                        sector.std.port_timezone if sector.std else None,
                        _event_value(sector.std, "sg"),
                        _event_value(sector.std, "utc"),
                        _event_value(sector.sta, "port"),
                        sector.sta.port_timezone if sector.sta else None,
                        _event_value(sector.sta, "sg"),
                        _event_value(sector.sta, "utc"),
                    ),
                )
        return version_id

    @staticmethod
    def _insert_alert(
        connection: sqlite3.Connection,
        version_id: int | None,
        alert: Alert,
        now: datetime,
        *,
        generation: int = 0,
    ) -> None:
        connection.execute(
            "INSERT INTO alerts "
            "(roster_version_id,event_key,alert_type,due_utc,message,status,attempt_count,"
            "schedule_generation,created_at,updated_at) VALUES (?,?,?,?,?,'pending',0,?,?,?)",
            (
                version_id,
                alert.event_key,
                alert.alert_type.value,
                _iso(_require_aware(alert.due_utc, "alert due_utc").astimezone(UTC)),
                alert.message,
                generation,
                _iso(now),
                _iso(now),
            ),
        )


def _roster_to_dict(roster: Roster) -> dict[str, Any]:
    return {
        "period_start": roster.period_start.isoformat(),
        "period_end": roster.period_end.isoformat(),
        "coverage": roster.coverage.value,
        "port_local_notice_present": roster.port_local_notice_present,
        "content_hash": roster.content_hash,
        "file_set_hash": roster.file_set_hash,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "source_position": issue.source_position,
                "duty_id": issue.duty_id,
                "sector_id": issue.sector_id,
            }
            for issue in roster.issues
        ],
        "duties": [_duty_to_dict(duty) for duty in roster.duties],
    }


def _duty_to_dict(duty: Duty) -> dict[str, Any]:
    return {
        "duty_id": duty.duty_id,
        "rpt": _event_to_dict(duty.rpt),
        "rpt_printed": duty.rpt_printed,
        "rpt_date": duty.rpt_date.isoformat() if duty.rpt_date else None,
        "reporting_airport": _airport_to_dict(duty.reporting_airport),
        "reporting_iata": duty.reporting_iata,
        "source_position": duty.source_position,
        "validation_errors": duty.validation_errors,
        "sectors": [_sector_to_dict(sector) for sector in duty.sectors],
    }


def _sector_to_dict(sector: Sector) -> dict[str, Any]:
    return {
        "sector_id": sector.sector_id,
        "flight_number": sector.flight_number,
        "origin": _airport_to_dict(sector.origin),
        "destination": _airport_to_dict(sector.destination),
        "origin_iata": sector.origin_iata,
        "destination_iata": sector.destination_iata,
        "std": _event_to_dict(sector.std),
        "sta": _event_to_dict(sector.sta),
        "std_printed": sector.std_printed,
        "sta_printed": sector.sta_printed,
        "std_date": sector.std_date.isoformat() if sector.std_date else None,
        "sta_date": sector.sta_date.isoformat() if sector.sta_date else None,
        "flight_time": sector.flight_time,
        "source_positions": sector.source_positions,
        "unreadable": sorted(sector.unreadable),
        "validation_errors": sector.validation_errors,
        "duty_id": sector.duty_id,
    }


def _roster_from_dict(value: dict[str, Any]) -> Roster:
    roster = Roster(
        period_start=date.fromisoformat(value["period_start"]),
        period_end=date.fromisoformat(value["period_end"]),
        coverage=Coverage(value["coverage"]),
        duties=[_duty_from_dict(item) for item in value["duties"]],
        issues=[
            ReviewIssue(
                item["code"],
                item["message"],
                tuple(item["source_position"]) if item["source_position"] else None,
                item.get("duty_id"),
                item.get("sector_id"),
            )
            for item in value.get("issues", [])
        ],
        port_local_notice_present=value["port_local_notice_present"],
        content_hash=value["content_hash"],
        file_set_hash=value.get("file_set_hash", ""),
    )
    return roster


def _duty_from_dict(value: dict[str, Any]) -> Duty:
    return Duty(
        duty_id=value["duty_id"],
        rpt=_event_from_dict(value["rpt"]),
        rpt_printed=value["rpt_printed"],
        rpt_date=date.fromisoformat(value["rpt_date"]) if value["rpt_date"] else None,
        reporting_airport=_airport_from_dict(value["reporting_airport"]),
        reporting_iata=value["reporting_iata"],
        sectors=[_sector_from_dict(item) for item in value["sectors"]],
        source_position=tuple(value["source_position"]),
        validation_errors=list(value["validation_errors"]),
    )


def _sector_from_dict(value: dict[str, Any]) -> Sector:
    return Sector(
        sector_id=value["sector_id"],
        flight_number=value["flight_number"],
        origin=_airport_from_dict(value["origin"]),
        destination=_airport_from_dict(value["destination"]),
        origin_iata=value["origin_iata"],
        destination_iata=value["destination_iata"],
        std=_event_from_dict(value["std"]),
        sta=_event_from_dict(value["sta"]),
        std_printed=value["std_printed"],
        sta_printed=value["sta_printed"],
        std_date=date.fromisoformat(value["std_date"]) if value["std_date"] else None,
        sta_date=date.fromisoformat(value["sta_date"]) if value["sta_date"] else None,
        flight_time=value["flight_time"],
        source_positions=tuple(tuple(item) for item in value["source_positions"]),
        unreadable=frozenset(value["unreadable"]),
        validation_errors=list(value["validation_errors"]),
        duty_id=value["duty_id"],
    )


def _airport_to_dict(value: Airport | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {
        "iata": value.iata,
        "city": value.city,
        "country_full_name": value.country_full_name,
        "timezone": value.timezone,
    }


def _airport_from_dict(value: dict[str, str] | None) -> Airport | None:
    return Airport(**value) if value else None


def _event_to_dict(value: EventTime | None) -> dict[str, str] | None:
    return value.canonical() if value else None


def _event_from_dict(value: dict[str, str] | None) -> EventTime | None:
    if value is None:
        return None
    return EventTime(
        port_local_datetime=_datetime(value["port_local_datetime"]),
        port_timezone=value["port_timezone"],
        singapore_datetime=_datetime(value["singapore_datetime"]),
        utc_datetime=_datetime(value["utc_datetime"]),
    )


def _event_value(event: EventTime | None, kind: str) -> str | None:
    if event is None:
        return None
    if kind == "port":
        return _iso(event.port_local_datetime)
    if kind == "sg":
        return _iso(event.singapore_datetime)
    return _iso(event.utc_datetime)


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value
