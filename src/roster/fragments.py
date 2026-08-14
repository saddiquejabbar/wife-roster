from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re

from .models import AttributedRow, RawTranscription, ReviewIssue, TranscribedRow
from .timezones import parse_roster_date


@dataclass(slots=True)
class SectorFragment:
    flight_number: str
    origin_iata: str
    destination_iata: str
    rpt: str | None = None
    rpt_date: date | None = None
    std: str | None = None
    std_date: date | None = None
    sta: str | None = None
    sta_date: date | None = None
    flight_time: str | None = None
    positions: list[tuple[int, int]] = field(default_factory=list)
    position_dates: list[date | None] = field(default_factory=list)
    unreadable: set[str] = field(default_factory=set)

    @property
    def first_position(self) -> tuple[int, int]:
        return self.positions[0]

    @property
    def last_date(self) -> date | None:
        dated = [value for value in self.position_dates if value is not None]
        return dated[-1] if dated else None


def attribute_row_dates(
    transcription: RawTranscription,
) -> tuple[list[AttributedRow], list[ReviewIssue]]:
    rows = sorted(transcription.rows, key=lambda row: (row.source_index, row.row_index))
    attributed: list[AttributedRow] = []
    issues: list[ReviewIssue] = []
    nearest_above: date | None = None
    for row in rows:
        if row.start_date is not None:
            try:
                nearest_above = parse_roster_date(row.start_date)
            except ValueError as exc:
                nearest_above = None
                issues.append(
                    ReviewIssue(
                        "invalid_row_date",
                        str(exc),
                        (row.source_index, row.row_index),
                    )
                )
        attributed.append(AttributedRow(row, nearest_above))
        if _has_roster_time(row) and nearest_above is None:
            issues.append(
                ReviewIssue(
                    "missing_row_date",
                    "Timed row has no printed date above it",
                    (row.source_index, row.row_index),
                )
            )
    return attributed, issues


def merge_flight_fragments(
    rows: list[AttributedRow],
) -> tuple[list[SectorFragment], list[ReviewIssue]]:
    fragments: list[SectorFragment] = []
    issues: list[ReviewIssue] = []
    for attributed in rows:
        row = attributed.raw
        if row.duty not in (None, "", "FLY"):
            # Exact FLY whitelist: timed and unknown non-FLY rows are ignored.
            continue
        if row.duty != "FLY" and not _looks_like_continuation(row):
            continue
        flight = normalize_flight_number(row.flight_number)
        origin, destination = normalize_sector(row.sector)
        if row.duty == "FLY":
            candidate = _merge_candidate(fragments, flight, origin, destination, attributed)
            if candidate is not None and _complementary(candidate, row):
                _merge_into(candidate, attributed)
            else:
                fragments.append(_new_fragment(attributed, flight, origin, destination))
            continue
        # A blank-duty fragment can never create a sector. It may only add
        # complementary cells to a preceding identifiable FLY sector.
        candidate = _merge_candidate(fragments, flight, origin, destination, attributed)
        if candidate is not None and _complementary(candidate, row):
            _merge_into(candidate, attributed)
        elif any((row.rpt, row.std, row.sta)):
            issues.append(
                ReviewIssue(
                    "orphan_fragment",
                    "Blank-duty flight fragment could not be matched to a preceding FLY row",
                    (row.source_index, row.row_index),
                )
            )
    return fragments, issues


def normalize_flight_number(value: str | None) -> str:
    if not value:
        return ""
    normalized = re.sub(r"[\s-]+", "", value.upper())
    if not re.fullmatch(r"[A-Z0-9]{2,3}\d{1,4}[A-Z]?", normalized):
        return ""
    return normalized


def normalize_sector(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    match = re.fullmatch(
        r"\s*([A-Za-z]{3})\s*(?:-|–|—|→|/)\s*([A-Za-z]{3})\s*",
        value,
    )
    if match is None:
        return "", ""
    return match.group(1).upper(), match.group(2).upper()


def _new_fragment(
    attributed: AttributedRow,
    flight: str,
    origin: str,
    destination: str,
) -> SectorFragment:
    row = attributed.raw
    return SectorFragment(
        flight_number=flight,
        origin_iata=origin,
        destination_iata=destination,
        rpt=row.rpt,
        rpt_date=attributed.row_date if row.rpt else None,
        std=row.std,
        std_date=attributed.row_date if row.std else None,
        sta=row.sta,
        sta_date=attributed.row_date if row.sta else None,
        flight_time=row.flight_time,
        positions=[(row.source_index, row.row_index)],
        position_dates=[attributed.row_date],
        unreadable=set(row.unreadable),
    )


def _merge_into(fragment: SectorFragment, attributed: AttributedRow) -> None:
    row = attributed.raw
    for name in ("rpt", "std", "sta"):
        value = getattr(row, name)
        if value is not None and getattr(fragment, name) is None:
            setattr(fragment, name, value)
            setattr(fragment, f"{name}_date", attributed.row_date)
    if row.flight_time is not None and fragment.flight_time is None:
        fragment.flight_time = row.flight_time
    fragment.positions.append((row.source_index, row.row_index))
    fragment.position_dates.append(attributed.row_date)
    fragment.unreadable.update(row.unreadable)


def _merge_candidate(
    fragments: list[SectorFragment],
    flight: str,
    origin: str,
    destination: str,
    attributed: AttributedRow,
) -> SectorFragment | None:
    if not flight or not origin or not destination:
        return None
    for fragment in reversed(fragments):
        if (
            fragment.flight_number == flight
            and fragment.origin_iata == origin
            and fragment.destination_iata == destination
        ):
            if fragment.last_date is None or attributed.row_date is None:
                return fragment
            delta = (attributed.row_date - fragment.last_date).days
            return fragment if 0 <= delta <= 2 else None
    return None


def _complementary(fragment: SectorFragment, row: TranscribedRow) -> bool:
    supplied = {
        "rpt": row.rpt,
        "std": row.std,
        "sta": row.sta,
        "flight_time": row.flight_time,
    }
    if not any(value is not None for value in supplied.values()):
        return False
    fills_blank = False
    for name, value in supplied.items():
        if value is None:
            continue
        existing = getattr(fragment, name)
        if existing is None:
            fills_blank = True
        elif existing != value:
            return False
    return fills_blank


def _looks_like_continuation(row: TranscribedRow) -> bool:
    return bool(row.flight_number and row.sector and any((row.rpt, row.std, row.sta, row.flight_time)))


def _has_roster_time(row: TranscribedRow) -> bool:
    return any((row.rpt, row.std, row.sta))
