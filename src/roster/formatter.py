from __future__ import annotations

from collections import defaultdict
from datetime import date

from .airports import human_route
from .models import Alert, Airport, Duty, EventTime, ReviewIssue, Roster, RosterDiff, Sector
from .timezones import hhmm


def format_summary(roster: Roster) -> str:
    entries: list[tuple[date, Duty, Sector, bool]] = []
    for duty in roster.duties:
        for index, sector in enumerate(duty.sectors):
            if not sector.valid or sector.std_date is None:
                continue
            entries.append((sector.std_date, duty, sector, index == 0))
    entries.sort(
        key=lambda item: (
            item[0],
            item[2].std.utc_datetime if item[2].std else _minimum_sort_key(),
            item[2].flight_number,
        )
    )
    lines: list[str] = []
    current_month: tuple[int, int] | None = None
    current_day: date | None = None
    for event_date, duty, sector, first_in_duty in entries:
        month_key = (event_date.year, event_date.month)
        if month_key != current_month:
            if lines:
                lines.append("")
            lines.extend((event_date.strftime("%B").upper(), ""))
            current_month = month_key
            current_day = None
        if event_date != current_day:
            if current_day is not None:
                lines.append("")
            lines.extend((f"{event_date.day} {event_date:%a}", ""))
            current_day = event_date
        elif lines and lines[-1] != "":
            lines.append("")
        lines.extend(_format_sector_summary(duty, sector, first_in_duty))
    return "\n".join(lines).rstrip()


def format_event_time(event: EventTime, airport: Airport) -> str:
    primary = hhmm(event.port_local_datetime)
    if airport.iata == "SIN":
        return primary
    return f"{primary} ({hhmm(event.singapore_datetime)} SG)"


def format_issues(issues: list[ReviewIssue]) -> str:
    if not issues:
        return "NEEDS REVIEW\nNone"
    unique: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        rendered = issue.display()
        if rendered not in seen:
            unique.append(rendered)
            seen.add(rendered)
    return "NEEDS REVIEW\n" + "\n".join(f"- {item}" for item in unique)


def format_diff(diff: RosterDiff) -> str:
    if diff.unchanged:
        return "Roster unchanged"
    lines = ["Roster updated"]
    for heading, changes in (
        ("Changed", diff.changed),
        ("Added", diff.added),
        ("Removed", diff.removed),
    ):
        if changes:
            lines.extend(("", heading))
            lines.extend(change.description for change in changes)
    lines.extend(("", "Alerts updated"))
    return "\n".join(lines)


def format_alert_listing(alerts: list[Alert]) -> str:
    if not alerts:
        return "No future alerts"
    groups: dict[str, list[Alert]] = defaultdict(list)
    for alert in alerts:
        groups[alert.due_utc.isoformat().replace("+00:00", "Z")].append(alert)
    lines: list[str] = []
    for due in sorted(groups):
        for alert in groups[due]:
            if lines:
                lines.append("")
            lines.append(due)
            lines.extend(alert.message.splitlines())
    return "\n".join(lines)


def _format_sector_summary(duty: Duty, sector: Sector, first_in_duty: bool) -> list[str]:
    if sector.origin is None or sector.destination is None or sector.std is None or sector.sta is None:
        return []
    lines = [
        sector.flight_number,
        human_route(sector.origin, sector.destination),
    ]
    if (
        first_in_duty
        and duty.rpt is not None
        and duty.reporting_airport is not None
        and duty.rpt_printed is not None
    ):
        lines.append(f"RPT {format_event_time(duty.rpt, duty.reporting_airport)}")
    lines.extend(
        (
            f"DEP {format_event_time(sector.std, sector.origin)}",
            f"ARR {format_event_time(sector.sta, sector.destination)}",
        )
    )
    return lines


def _minimum_sort_key():
    from datetime import UTC, datetime

    return datetime.min.replace(tzinfo=UTC)
