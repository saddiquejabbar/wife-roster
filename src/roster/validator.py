from __future__ import annotations

from datetime import timedelta
import re

from .models import Duty, ReviewIssue, Roster, Sector


FLIGHT_TIME_TOLERANCE = timedelta(minutes=20)
MIN_FLIGHT_DURATION = timedelta(minutes=20)
MAX_FLIGHT_DURATION = timedelta(hours=24)


def validate_roster(roster: Roster) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    seen: dict[tuple[str, str, str, str, str], Sector] = {}
    for duty in roster.duties:
        _validate_duty(duty, issues)
        for sector in duty.sectors:
            _validate_sector(sector, roster, issues)
            identity = _sector_identity(sector)
            if identity is not None:
                if identity in seen:
                    _sector_error(
                        sector,
                        issues,
                        "duplicate_sector",
                        f"Duplicate sector {sector.flight_number} {sector.origin_iata}-{sector.destination_iata}",
                    )
                else:
                    seen[identity] = sector
        if any(not sector.valid for sector in duty.sectors):
            _duty_error(duty, "duty contains an invalid sector")
    roster.issues.extend(issues)
    return issues


def _validate_duty(duty: Duty, issues: list[ReviewIssue]) -> None:
    if duty.rpt is None or not duty.sectors:
        return
    first = duty.sectors[0]
    if first.std is not None:
        lead = first.std.utc_datetime - duty.rpt.utc_datetime
        if lead <= timedelta(0) or lead > timedelta(hours=12):
            message = f"{first.flight_number} STD is not plausibly after Duty RPT"
            _duty_error(duty, message)
            issues.append(
                ReviewIssue(
                    "invalid_rpt_sequence",
                    message,
                    duty.source_position,
                    duty_id=duty.duty_id,
                    sector_id=first.sector_id,
                )
            )
    for previous, following in zip(duty.sectors, duty.sectors[1:]):
        if previous.destination_iata != following.origin_iata:
            message = (
                f"Duty route is discontinuous: {previous.destination_iata} does not connect "
                f"to {following.origin_iata}"
            )
            _duty_error(duty, message)
            issues.append(
                ReviewIssue(
                    "route_discontinuity",
                    message,
                    following.source_positions[0],
                    duty_id=duty.duty_id,
                    sector_id=following.sector_id,
                )
            )
        if previous.sta is not None and following.std is not None:
            gap = following.std.utc_datetime - previous.sta.utc_datetime
            if gap < timedelta(minutes=-20) or gap > timedelta(hours=18):
                message = f"Duty sector timing is not continuous before {following.flight_number}"
                _duty_error(duty, message)
                issues.append(
                    ReviewIssue(
                        "duty_timing_discontinuity",
                        message,
                        following.source_positions[0],
                        duty_id=duty.duty_id,
                        sector_id=following.sector_id,
                    )
                )


def _validate_sector(sector: Sector, roster: Roster, issues: list[ReviewIssue]) -> None:
    if sector.std_date is not None and not (roster.period_start <= sector.std_date < roster.period_end):
        _sector_error(
            sector,
            issues,
            "outside_report_period",
            f"{sector.flight_number} departure is outside the report period",
        )
    if sector.std is None or sector.sta is None:
        return
    elapsed = sector.sta.utc_datetime - sector.std.utc_datetime
    if elapsed < MIN_FLIGHT_DURATION or elapsed > MAX_FLIGHT_DURATION:
        _sector_error(
            sector,
            issues,
            "impossible_duration",
            f"{sector.flight_number} has an implausible elapsed flight duration",
        )
        return
    if sector.flight_time:
        printed = _parse_flight_duration(sector.flight_time)
        if printed is None:
            _sector_error(
                sector,
                issues,
                "invalid_flight_time",
                f"{sector.flight_number} has invalid printed Flight Time {sector.flight_time!r}",
            )
        elif abs(elapsed - printed) > FLIGHT_TIME_TOLERANCE:
            _sector_error(
                sector,
                issues,
                "flight_time_mismatch",
                f"{sector.flight_number} elapsed time differs materially from printed Flight Time",
            )


def _parse_flight_duration(value: str) -> timedelta | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if minutes > 59 or hours > 24:
        return None
    return timedelta(hours=hours, minutes=minutes)


def _sector_identity(sector: Sector) -> tuple[str, str, str, str, str] | None:
    if sector.std is None or sector.sta is None:
        return None
    return (
        sector.flight_number,
        sector.origin_iata,
        sector.destination_iata,
        sector.std.utc_datetime.isoformat(),
        sector.sta.utc_datetime.isoformat(),
    )


def _sector_error(
    sector: Sector,
    issues: list[ReviewIssue],
    code: str,
    message: str,
) -> None:
    if message not in sector.validation_errors:
        sector.validation_errors.append(message)
        issues.append(
            ReviewIssue(
                code,
                message,
                sector.source_positions[0] if sector.source_positions else None,
                duty_id=sector.duty_id,
                sector_id=sector.sector_id,
            )
        )


def _duty_error(duty: Duty, message: str) -> None:
    if message not in duty.validation_errors:
        duty.validation_errors.append(message)
