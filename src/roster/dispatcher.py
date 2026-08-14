from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from .database import RecoveryResult, RosterDatabase
from .models import AlertType
from .notifier import Notifier


GRACE_PERIODS = {
    AlertType.PREP_12H: timedelta(minutes=60),
    AlertType.PREP_3H: timedelta(minutes=30),
    AlertType.LANDING_1H: timedelta(minutes=20),
}
SENDING_LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (timedelta(minutes=5), timedelta(minutes=15))


@dataclass(frozen=True, slots=True)
class DispatchResult:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    recovered: int = 0
    missed: int = 0


class Dispatcher:
    def __init__(
        self,
        database: RosterDatabase,
        notifier: Notifier,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.notifier = notifier
        self.clock = clock or (lambda: datetime.now(UTC))

    def dispatch_due(self) -> DispatchResult:
        started = _aware(self.clock())
        recovery = RecoveryResult()
        claimed = []
        sent = 0
        failed = 0
        try:
            recovery = self.database.recover_alerts(
                started,
                grace_periods=GRACE_PERIODS,
                sending_lease=SENDING_LEASE,
            )
            claimed = self.database.claim_due_alerts(started)
            for alert in claimed:
                attempted_at = _aware(self.clock())
                if not self.database.is_current_claim(alert):
                    continue
                try:
                    notification_id = self.notifier.send(alert, sent_at=attempted_at)
                except Exception as exc:  # A notifier failure belongs in alert history.
                    retry_at = _retry_at(alert, attempted_at)
                    self.database.mark_dispatch_failed(
                        alert.event_key,
                        failed_at=attempted_at,
                        error=f"{type(exc).__name__}: {exc}",
                        retry_at=retry_at,
                    )
                    failed += 1
                    continue
                if self.database.mark_dispatch_sent(
                    alert.event_key,
                    sent_at=attempted_at,
                    notification_id=notification_id,
                ):
                    sent += 1
        finally:
            self.database.record_last_dispatch(_aware(self.clock()))
        return DispatchResult(
            claimed=len(claimed),
            sent=sent,
            failed=failed,
            recovered=recovery.recovered,
            missed=recovery.missed,
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("dispatcher clock must return a timezone-aware datetime")
    return value


def _retry_at(alert, failed_at: datetime) -> datetime | None:
    if alert.attempt_count >= MAX_ATTEMPTS:
        return None
    retry = failed_at + RETRY_BACKOFF[alert.attempt_count - 1]
    return retry if retry <= alert.due_utc + GRACE_PERIODS[alert.alert_type] else None
