from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Protocol

from .database import RosterDatabase
from .dispatcher import SENDING_LEASE


class CalendarScheduler(Protocol):
    def arm(self, when: datetime, *, generation: int) -> None: ...

    def disarm(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PlanResult:
    generation: int
    armed_for: datetime | None


class SchedulePlanner:
    """Translate authoritative database state into one launchd calendar wake."""

    def __init__(
        self,
        database: RosterDatabase,
        scheduler: CalendarScheduler,
        *,
        clock: Callable[[], datetime] | None = None,
        sending_lease: timedelta = SENDING_LEASE,
    ) -> None:
        self.database = database
        self.scheduler = scheduler
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sending_lease = sending_lease

    def plan(self) -> PlanResult:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("planner clock must return a timezone-aware datetime")
        state = self.database.schedule_state()
        generation = int(state["schedule_generation"])
        next_wake = self.database.next_dispatch_at(now, sending_lease=self.sending_lease)
        if next_wake is None:
            self.scheduler.disarm()
        else:
            self.scheduler.arm(next_wake, generation=generation)
        self.database.record_planned_schedule(generation, next_wake)
        return PlanResult(generation, next_wake)
