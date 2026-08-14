from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    installed: bool
    loaded: bool
    valid: bool
    workflow_available: bool
    warnings: tuple[str, ...] = ()
    planner_installed: bool = False
    planner_loaded: bool = False
    dispatcher_armed: bool = False
    armed_for_utc: str | None = None
    armed_generation: int | None = None


class Scheduler(Protocol):
    def install(self) -> SchedulerStatus: ...

    def refresh(self) -> SchedulerStatus: ...

    def remove(self) -> SchedulerStatus: ...

    def status(self) -> SchedulerStatus: ...


class N8nScheduler(Scheduler, Protocol):
    """Future scheduler boundary; Stage 2 contains no n8n implementation."""
