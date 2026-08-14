from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Airport, EventTime


SINGAPORE_ZONE = ZoneInfo("Asia/Singapore")


class TimeResolutionError(ValueError):
    pass


def parse_roster_date(value: str) -> date:
    cleaned = value.strip()
    for pattern in ("%d%b%y", "%d%b%Y"):
        try:
            return datetime.strptime(cleaned.title(), pattern).date()
        except ValueError:
            pass
    raise ValueError(f"invalid roster date {value!r}; expected DDMonYY")


def parse_hhmm(value: str) -> tuple[int, int]:
    cleaned = value.strip()
    if len(cleaned) != 4 or not cleaned.isdigit():
        raise ValueError(f"invalid roster time {value!r}; expected HHMM")
    hour, minute = int(cleaned[:2]), int(cleaned[2:])
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid roster time {value!r}; expected HHMM")
    return hour, minute


def resolve_event(event_date: date, hhmm: str, airport: Airport) -> EventTime:
    hour, minute = parse_hhmm(hhmm)
    naive = datetime(event_date.year, event_date.month, event_date.day, hour, minute)
    try:
        zone = ZoneInfo(airport.timezone)
    except ZoneInfoNotFoundError as exc:
        raise TimeResolutionError(f"timezone data unavailable for {airport.iata}") from exc
    local = _localize_unambiguous(naive, zone, airport.iata)
    utc = local.astimezone(UTC)
    return EventTime(
        port_local_datetime=local,
        port_timezone=airport.timezone,
        singapore_datetime=utc.astimezone(SINGAPORE_ZONE),
        utc_datetime=utc,
    )


def _localize_unambiguous(naive: datetime, zone: ZoneInfo, iata: str) -> datetime:
    candidates: list[datetime] = []
    seen_utc: set[datetime] = set()
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        utc = local.astimezone(UTC)
        roundtrip = utc.astimezone(zone).replace(tzinfo=None)
        if roundtrip == naive and utc not in seen_utc:
            candidates.append(local)
            seen_utc.add(utc)
    if not candidates:
        raise TimeResolutionError(f"nonexistent local time {naive:%d%b%y %H%M} at {iata}")
    if len(candidates) > 1:
        raise TimeResolutionError(f"ambiguous local time {naive:%d%b%y %H%M} at {iata}")
    return candidates[0]


def hhmm(value: datetime) -> str:
    return value.strftime("%H%M")
