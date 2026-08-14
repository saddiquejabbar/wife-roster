from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta

from .airports import human_route
from .formatter import format_event_time
from .models import Coverage, DiffChange, Duty, Roster, RosterDiff, Sector
from .normalize import calculate_content_hash


def diff_rosters(active: Roster | None, candidate: Roster) -> RosterDiff:
    if active is None:
        return RosterDiff(
            added=tuple(DiffChange("added", _added_removed_text(sector)) for sector in candidate.sectors)
        )
    covered_months = _covered_months(candidate.period_start, candidate.period_end)
    relevant_old = [
        sector
        for sector in active.sectors
        if sector.std_date is not None
        and (
            (sector.std_date.year, sector.std_date.month) in covered_months
            if candidate.coverage == Coverage.FULL
            else candidate.period_start <= sector.std_date < candidate.period_end
        )
    ]
    pairs, unmatched_old, unmatched_new = _match_sectors(relevant_old, candidate.sectors)
    changed: list[DiffChange] = []
    seen_descriptions: set[str] = set()
    active_duties = _duty_by_sector(active)
    candidate_duties = _duty_by_sector(candidate)
    for old, new in pairs:
        for description in _compare_sector(
            old,
            new,
            active_duties.get(old.sector_id),
            candidate_duties.get(new.sector_id),
        ):
            if description not in seen_descriptions:
                changed.append(DiffChange("changed", description))
                seen_descriptions.add(description)
    added = tuple(DiffChange("added", _added_removed_text(sector)) for sector in unmatched_new)
    removed: tuple[DiffChange, ...] = ()
    if candidate.coverage == Coverage.FULL:
        removed = tuple(DiffChange("removed", _added_removed_text(sector)) for sector in unmatched_old)
    return RosterDiff(added=added, removed=removed, changed=tuple(changed))


def merge_rosters(active: Roster | None, candidate: Roster) -> Roster:
    """Build the effective immutable snapshot used by transactional apply.

    FULL replaces every duty in the calendar month(s) represented by the
    candidate. This deliberately treats an amended monthly roster as the new
    source of truth even when its printed period ends on the month's last day.
    PARTIAL/UNCERTAIN only replace duties matched by candidate sectors and never
    remove unmatched duties.
    """
    if active is None:
        return deepcopy(candidate)
    old = deepcopy(active)
    new = deepcopy(candidate)
    if candidate.coverage == Coverage.FULL:
        covered_months = _covered_months(candidate.period_start, candidate.period_end)
        retained = [
            duty
            for duty in old.duties
            if _duty_month(duty) not in covered_months
        ]
    else:
        pairs, _, _ = _match_sectors(old.sectors, new.sectors)
        matched_ids = {old_sector.sector_id for old_sector, _ in pairs}
        retained = []
        for duty in old.duties:
            if not any(sector.sector_id in matched_ids for sector in duty.sectors):
                retained.append(duty)
                continue
            remaining = [sector for sector in duty.sectors if sector.sector_id not in matched_ids]
            if remaining:
                duty.sectors = remaining
                retained.append(duty)
    combined = Roster(
        period_start=min(old.period_start, new.period_start),
        period_end=max(old.period_end, new.period_end),
        coverage=old.coverage if candidate.coverage != Coverage.FULL else Coverage.FULL,
        duties=retained + new.duties,
        issues=list(new.issues),
        port_local_notice_present=(
            old.port_local_notice_present and new.port_local_notice_present
        ),
        file_set_hash=new.file_set_hash,
    )
    combined.duties.sort(key=_duty_sort_key)
    combined.content_hash = calculate_content_hash(combined)
    return combined


def _match_sectors(
    old_sectors: list[Sector],
    new_sectors: list[Sector],
) -> tuple[list[tuple[Sector, Sector]], list[Sector], list[Sector]]:
    remaining_old = list(old_sectors)
    pairs: list[tuple[Sector, Sector]] = []
    remaining_new: list[Sector] = []
    for new in new_sectors:
        old = _take_match(remaining_old, new)
        if old is None:
            remaining_new.append(new)
        else:
            pairs.append((old, new))
    return pairs, remaining_old, remaining_new


def _take_match(old_sectors: list[Sector], new: Sector) -> Sector | None:
    matchers = (
        lambda old: _event_identity(old) == _event_identity(new),
        lambda old: old.flight_number == new.flight_number and _event_date(old) == _event_date(new),
        lambda old: (
            old.origin_iata == new.origin_iata
            and old.destination_iata == new.destination_iata
            and _event_date(old) == _event_date(new)
        ),
    )
    for matcher in matchers:
        for index, old in enumerate(old_sectors):
            if matcher(old):
                return old_sectors.pop(index)
    return None


def _compare_sector(old: Sector, new: Sector, old_duty: Duty | None, new_duty: Duty | None) -> list[str]:
    label = new.flight_number or old.flight_number
    changes: list[str] = []
    if old.flight_number != new.flight_number:
        changes.append(f"Flight {old.flight_number} → {new.flight_number}")
    if (old.origin_iata, old.destination_iata) != (new.origin_iata, new.destination_iata):
        if old.origin and old.destination and new.origin and new.destination:
            changes.append(
                f"{label} route {human_route(old.origin, old.destination)} → "
                f"{human_route(new.origin, new.destination)}"
            )
        else:
            changes.append(
                f"{label} route {old.origin_iata}-{old.destination_iata} → "
                f"{new.origin_iata}-{new.destination_iata}"
            )
    if old_duty and new_duty and _event_changed(old_duty.rpt, new_duty.rpt):
        if old_duty.rpt and old_duty.reporting_airport and new_duty.rpt and new_duty.reporting_airport:
            changes.append(
                f"{label} RPT {format_event_time(old_duty.rpt, old_duty.reporting_airport)} → "
                f"{format_event_time(new_duty.rpt, new_duty.reporting_airport)}"
            )
    if _event_changed(old.std, new.std) and old.std and old.origin and new.std and new.origin:
        changes.append(
            f"{label} DEP {format_event_time(old.std, old.origin)} → "
            f"{format_event_time(new.std, new.origin)}"
        )
    if _event_changed(old.sta, new.sta) and old.sta and old.destination and new.sta and new.destination:
        changes.append(
            f"{label} ARR {format_event_time(old.sta, old.destination)} → "
            f"{format_event_time(new.sta, new.destination)}"
        )
    if old_duty and new_duty and _duty_signature(old_duty) != _duty_signature(new_duty):
        changes.append(f"{label} duty grouping changed")
    return changes


def _event_changed(old, new) -> bool:
    if old is None or new is None:
        return old is not new
    return old.utc_datetime != new.utc_datetime


def _event_identity(sector: Sector) -> tuple[str, str, str, str, str]:
    return (
        sector.flight_number,
        sector.origin_iata,
        sector.destination_iata,
        sector.std.utc_datetime.isoformat() if sector.std else "",
        sector.sta.utc_datetime.isoformat() if sector.sta else "",
    )


def _event_date(sector: Sector) -> date | None:
    return sector.std_date or sector.sta_date


def _duty_signature(duty: Duty) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (sector.flight_number, sector.origin_iata, sector.destination_iata)
        for sector in duty.sectors
    )


def _duty_by_sector(roster: Roster) -> dict[str, Duty]:
    return {sector.sector_id: duty for duty in roster.duties for sector in duty.sectors}


def _added_removed_text(sector: Sector) -> str:
    if sector.origin and sector.destination:
        return f"{sector.flight_number} {human_route(sector.origin, sector.destination)}"
    return f"{sector.flight_number} {sector.origin_iata}-{sector.destination_iata}"


def _duty_in_period(duty: Duty, start: date, end: date) -> bool:
    first_date = duty.sectors[0].std_date if duty.sectors else duty.rpt_date
    return first_date is not None and start <= first_date < end


def _duty_month(duty: Duty) -> tuple[int, int] | None:
    first_date = duty.sectors[0].std_date if duty.sectors else duty.rpt_date
    return (first_date.year, first_date.month) if first_date is not None else None


def _covered_months(start: date, end: date) -> set[tuple[int, int]]:
    """Return calendar months touched by an inclusive or exclusive roster period."""
    final = end - timedelta(days=1) if end.day == 1 and end > start else end
    cursor = date(start.year, start.month, 1)
    last = date(final.year, final.month, 1)
    months: set[tuple[int, int]] = set()
    while cursor <= last:
        months.add((cursor.year, cursor.month))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return months


def _duty_sort_key(duty: Duty):
    if duty.rpt is not None:
        return duty.rpt.utc_datetime.isoformat()
    if duty.sectors and duty.sectors[0].std is not None:
        return duty.sectors[0].std.utc_datetime.isoformat()
    return ""
