from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class Coverage(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    UNCERTAIN = "UNCERTAIN"


class VersionStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class AlertStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    MISSED = "missed"
    FAILED = "failed"


class AlertType(StrEnum):
    PREP_12H = "prep_12h"
    PREP_3H = "prep_3h"
    LANDING_1H = "landing_1h"


@dataclass(frozen=True, slots=True)
class ReportHeader:
    period_from: str | None
    period_to: str | None
    port_local_notice_present: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReportHeader":
        return cls(
            period_from=_nullable_string(value.get("period_from")),
            period_to=_nullable_string(value.get("period_to")),
            port_local_notice_present=value.get("port_local_notice_present") is True,
        )


RAW_ROW_FIELDS = (
    "source_index",
    "row_index",
    "start_date",
    "day",
    "flight_number",
    "sector",
    "duty",
    "rpt",
    "std",
    "sta",
    "flight_time",
    "remarks",
    "unreadable",
)


@dataclass(frozen=True, slots=True)
class TranscribedRow:
    source_index: int
    row_index: int
    start_date: str | None = None
    day: str | None = None
    flight_number: str | None = None
    sector: str | None = None
    duty: str | None = None
    rpt: str | None = None
    std: str | None = None
    sta: str | None = None
    flight_time: str | None = None
    remarks: str | None = None
    unreadable: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TranscribedRow":
        unknown = set(value) - set(RAW_ROW_FIELDS)
        if unknown:
            raise ValueError(f"unknown transcription row fields: {', '.join(sorted(unknown))}")
        unreadable = value.get("unreadable") or []
        if not isinstance(unreadable, list) or not all(isinstance(item, str) for item in unreadable):
            raise ValueError("row unreadable must be a list of field names")
        return cls(
            source_index=_required_nonnegative_int(value.get("source_index"), "source_index"),
            row_index=_required_nonnegative_int(value.get("row_index"), "row_index"),
            start_date=_nullable_string(value.get("start_date")),
            day=_nullable_string(value.get("day")),
            flight_number=_nullable_string(value.get("flight_number")),
            sector=_nullable_string(value.get("sector")),
            duty=_nullable_string(value.get("duty")),
            rpt=_nullable_string(value.get("rpt")),
            std=_nullable_string(value.get("std")),
            sta=_nullable_string(value.get("sta")),
            flight_time=_nullable_string(value.get("flight_time")),
            remarks=_nullable_string(value.get("remarks")),
            unreadable=tuple(unreadable),
        )


@dataclass(frozen=True, slots=True)
class RawTranscription:
    report_header: ReportHeader
    rows: tuple[TranscribedRow, ...]
    coverage: Coverage = Coverage.UNCERTAIN
    schema_version: int = 1

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RawTranscription":
        if not isinstance(value, dict):
            raise ValueError("transcription must be a JSON object")
        unknown = set(value) - {"schema_version", "coverage", "report_header", "rows"}
        if unknown:
            raise ValueError(f"unknown transcription fields: {', '.join(sorted(unknown))}")
        schema_version = value.get("schema_version", 1)
        if schema_version != 1:
            raise ValueError(f"unsupported transcription schema_version: {schema_version!r}")
        header = value.get("report_header")
        rows = value.get("rows")
        if not isinstance(header, dict):
            raise ValueError("report_header must be an object")
        if not isinstance(rows, list):
            raise ValueError("rows must be an array")
        try:
            coverage = Coverage(str(value.get("coverage", "UNCERTAIN")).upper())
        except ValueError as exc:
            raise ValueError("coverage must be FULL, PARTIAL, or UNCERTAIN") from exc
        parsed_rows = tuple(TranscribedRow.from_dict(row) for row in rows)
        positions = [(row.source_index, row.row_index) for row in parsed_rows]
        if len(positions) != len(set(positions)):
            raise ValueError("source_index/row_index pairs must be unique")
        return cls(
            report_header=ReportHeader.from_dict(header),
            rows=parsed_rows,
            coverage=coverage,
            schema_version=1,
        )


@dataclass(frozen=True, slots=True)
class AttributedRow:
    raw: TranscribedRow
    row_date: date | None


@dataclass(frozen=True, slots=True)
class Airport:
    iata: str
    city: str
    country_full_name: str
    timezone: str

    @property
    def display_name(self) -> str:
        if self.country_full_name == "Singapore" and self.city == "Singapore":
            return "Singapore"
        return f"{self.city}, {self.country_full_name}"


@dataclass(frozen=True, slots=True)
class EventTime:
    port_local_datetime: datetime
    port_timezone: str
    singapore_datetime: datetime
    utc_datetime: datetime

    def canonical(self) -> dict[str, str]:
        return {
            "port_local_datetime": self.port_local_datetime.isoformat(),
            "port_timezone": self.port_timezone,
            "singapore_datetime": self.singapore_datetime.isoformat(),
            "utc_datetime": self.utc_datetime.isoformat().replace("+00:00", "Z"),
        }


@dataclass(slots=True)
class Sector:
    sector_id: str
    flight_number: str
    origin: Airport | None
    destination: Airport | None
    origin_iata: str
    destination_iata: str
    std: EventTime | None
    sta: EventTime | None
    std_printed: str | None
    sta_printed: str | None
    std_date: date | None
    sta_date: date | None
    flight_time: str | None
    source_positions: tuple[tuple[int, int], ...]
    unreadable: frozenset[str] = frozenset()
    validation_errors: list[str] = field(default_factory=list)
    duty_id: str | None = None

    @property
    def valid(self) -> bool:
        return not self.validation_errors

    def canonical(self) -> dict[str, Any]:
        return {
            "flight_number": self.flight_number,
            "origin": self.origin_iata,
            "destination": self.destination_iata,
            "std": None if self.std is None else self.std.utc_datetime.isoformat(),
            "sta": None if self.sta is None else self.sta.utc_datetime.isoformat(),
            "flight_time": self.flight_time,
        }

    @property
    def stable_slot_key(self) -> tuple[str, str]:
        event_date = self.std_date or self.sta_date
        return self.flight_number, event_date.isoformat() if event_date else ""


@dataclass(slots=True)
class Duty:
    duty_id: str
    rpt: EventTime | None
    rpt_printed: str | None
    rpt_date: date | None
    reporting_airport: Airport | None
    reporting_iata: str
    sectors: list[Sector]
    source_position: tuple[int, int]
    validation_errors: list[str] = field(default_factory=list)

    @property
    def valid_for_alerts(self) -> bool:
        return not self.validation_errors and self.rpt is not None and bool(self.sectors)

    def canonical(self) -> dict[str, Any]:
        return {
            "rpt": None if self.rpt is None else self.rpt.utc_datetime.isoformat(),
            "reporting_airport": self.reporting_iata,
            "sectors": [sector.canonical() for sector in self.sectors],
        }


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    code: str
    message: str
    source_position: tuple[int, int] | None = None
    duty_id: str | None = None
    sector_id: str | None = None

    def display(self) -> str:
        location = ""
        if self.source_position is not None:
            location = f" [source {self.source_position[0] + 1}, row {self.source_position[1]}]"
        return f"{self.message}{location}"


@dataclass(slots=True)
class Roster:
    period_start: date
    period_end: date
    coverage: Coverage
    duties: list[Duty]
    issues: list[ReviewIssue]
    port_local_notice_present: bool
    content_hash: str = ""
    file_set_hash: str = ""

    @property
    def sectors(self) -> list[Sector]:
        return [sector for duty in self.duties for sector in duty.sectors]

    def canonical(self) -> dict[str, Any]:
        duties = sorted(
            (duty.canonical() for duty in self.duties),
            key=lambda item: (
                item["rpt"] or "",
                item["sectors"][0]["std"] if item["sectors"] else "",
                item["sectors"][0]["flight_number"] if item["sectors"] else "",
            ),
        )
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "coverage": self.coverage.value,
            "duties": duties,
        }


@dataclass(frozen=True, slots=True)
class Alert:
    event_key: str
    alert_type: AlertType
    due_utc: datetime
    message: str
    duty_id: str | None = None
    sector_id: str | None = None
    status: AlertStatus = AlertStatus.PENDING
    attempt_count: int = 0
    schedule_generation: int = 0


@dataclass(frozen=True, slots=True)
class DiffChange:
    kind: str
    description: str


@dataclass(frozen=True, slots=True)
class RosterDiff:
    added: tuple[DiffChange, ...] = ()
    removed: tuple[DiffChange, ...] = ()
    changed: tuple[DiffChange, ...] = ()

    @property
    def unchanged(self) -> bool:
        return not (self.added or self.removed or self.changed)


def _nullable_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"expected string or null, got {type(value).__name__}")
    return value


def _required_nonnegative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
