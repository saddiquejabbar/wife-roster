from __future__ import annotations

from datetime import timedelta
import hashlib
import json

from .airports import AirportCatalog
from .fragments import SectorFragment, attribute_row_dates, merge_flight_fragments
from .models import Duty, RawTranscription, ReviewIssue, Roster, Sector
from .timezones import TimeResolutionError, parse_roster_date, resolve_event


class NormalizationError(ValueError):
    def __init__(self, issues: list[ReviewIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


def normalize_transcription(
    transcription: RawTranscription,
    *,
    airports: AirportCatalog | None = None,
    file_set_hash: str = "",
) -> Roster:
    catalog = airports or AirportCatalog()
    fatal: list[ReviewIssue] = []
    header = transcription.report_header
    if header.period_from is None:
        fatal.append(ReviewIssue("missing_period_start", "Report period start is missing"))
    if header.period_to is None:
        fatal.append(ReviewIssue("missing_period_end", "Report period end is missing"))
    if fatal:
        raise NormalizationError(fatal)
    try:
        period_start = parse_roster_date(header.period_from or "")
        period_end = parse_roster_date(header.period_to or "")
    except ValueError as exc:
        raise NormalizationError([ReviewIssue("invalid_report_period", str(exc))]) from exc
    if period_end <= period_start:
        raise NormalizationError(
            [ReviewIssue("invalid_report_period", "Report period end must be after its start")]
        )

    attributed, issues = attribute_row_dates(transcription)
    fragments, fragment_issues = merge_flight_fragments(attributed)
    issues.extend(fragment_issues)
    sectors = [_build_sector(fragment, catalog, issues) for fragment in fragments]
    duties = _group_duties(sectors, fragments, issues)

    if not header.port_local_notice_present:
        issues.append(
            ReviewIssue(
                "missing_port_local_notice",
                "Roster header does not confirm that times are port local",
            )
        )
        for duty in duties:
            duty.validation_errors.append("port-local time basis is unconfirmed")
    if transcription.coverage.value == "UNCERTAIN":
        issues.append(
            ReviewIssue(
                "uncertain_coverage",
                "Roster coverage is UNCERTAIN; existing flights must not be removed",
            )
        )

    roster = Roster(
        period_start=period_start,
        period_end=period_end,
        coverage=transcription.coverage,
        duties=duties,
        issues=issues,
        port_local_notice_present=header.port_local_notice_present,
        file_set_hash=file_set_hash,
    )
    roster.content_hash = calculate_content_hash(roster)
    return roster


def calculate_content_hash(roster: Roster) -> str:
    payload = {
        "period_start": roster.period_start.isoformat(),
        "period_end": roster.period_end.isoformat(),
        "duties": sorted(
            (duty.canonical() for duty in roster.duties),
            key=lambda item: (
                item["rpt"] or "",
                item["sectors"][0]["std"] if item["sectors"] else "",
                item["sectors"][0]["flight_number"] if item["sectors"] else "",
            ),
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_sector(
    fragment: SectorFragment,
    catalog: AirportCatalog,
    issues: list[ReviewIssue],
) -> Sector:
    origin = catalog.lookup(fragment.origin_iata) if fragment.origin_iata else None
    destination = catalog.lookup(fragment.destination_iata) if fragment.destination_iata else None
    errors: list[str] = []
    position = fragment.first_position

    def reject(code: str, message: str) -> None:
        errors.append(message)
        issues.append(ReviewIssue(code, message, position))

    if not fragment.flight_number:
        reject("invalid_flight_number", "FLY row has a missing or invalid flight number")
    if not fragment.origin_iata or not fragment.destination_iata:
        reject("invalid_route", "FLY row has a missing or invalid origin/destination sector")
    if fragment.origin_iata and origin is None:
        reject("unknown_origin", f"Unknown airport {fragment.origin_iata}; cannot resolve timezone")
    if fragment.destination_iata and destination is None:
        reject(
            "unknown_destination",
            f"Unknown airport {fragment.destination_iata}; cannot resolve timezone",
        )
    if fragment.std is None:
        reject("missing_std", f"{fragment.flight_number or 'FLY row'} is missing STD")
    if fragment.sta is None:
        reject("missing_sta", f"{fragment.flight_number or 'FLY row'} is missing STA")
    if fragment.std is not None and fragment.std_date is None:
        reject("missing_std_date", f"{fragment.flight_number or 'FLY row'} STD has no attributed date")
    if fragment.sta is not None and fragment.sta_date is None:
        reject("missing_sta_date", f"{fragment.flight_number or 'FLY row'} STA has no attributed date")
    critical = {"flight_number", "sector", "std", "sta", "start_date"}
    doubtful = critical.intersection(fragment.unreadable)
    if doubtful:
        reject(
            "critical_unreadable",
            f"{fragment.flight_number or 'FLY row'} has unreadable critical fields: "
            + ", ".join(sorted(doubtful)),
        )

    std = None
    sta = None
    if origin and fragment.std and fragment.std_date:
        try:
            std = resolve_event(fragment.std_date, fragment.std, origin)
        except (ValueError, TimeResolutionError) as exc:
            reject("invalid_std", f"{fragment.flight_number or 'FLY row'} STD: {exc}")
    if destination and fragment.sta and fragment.sta_date:
        try:
            sta = resolve_event(fragment.sta_date, fragment.sta, destination)
        except (ValueError, TimeResolutionError) as exc:
            reject("invalid_sta", f"{fragment.flight_number or 'FLY row'} STA: {exc}")

    sector_identity = "|".join(
        (
            fragment.flight_number,
            fragment.origin_iata,
            fragment.destination_iata,
            std.utc_datetime.isoformat() if std else str(position),
            sta.utc_datetime.isoformat() if sta else "",
        )
    )
    sector_id = "sector-" + hashlib.sha256(sector_identity.encode()).hexdigest()[:16]
    return Sector(
        sector_id=sector_id,
        flight_number=fragment.flight_number,
        origin=origin,
        destination=destination,
        origin_iata=fragment.origin_iata,
        destination_iata=fragment.destination_iata,
        std=std,
        sta=sta,
        std_printed=fragment.std,
        sta_printed=fragment.sta,
        std_date=fragment.std_date,
        sta_date=fragment.sta_date,
        flight_time=fragment.flight_time,
        source_positions=tuple(fragment.positions),
        unreadable=frozenset(fragment.unreadable),
        validation_errors=errors,
    )


def _group_duties(
    sectors: list[Sector],
    fragments: list[SectorFragment],
    issues: list[ReviewIssue],
) -> list[Duty]:
    duties: list[Duty] = []
    active: Duty | None = None
    for sector, fragment in zip(sectors, fragments, strict=True):
        if fragment.rpt is not None:
            active = _new_duty(sector, fragment, issues)
            duties.append(active)
            continue
        if active is not None and _continuous(active.sectors[-1], sector):
            active.sectors.append(sector)
            sector.duty_id = active.duty_id
            continue
        message = f"{sector.flight_number or 'FLY row'} has no printed RPT opening its duty"
        issues.append(ReviewIssue("missing_duty_rpt", message, fragment.first_position, sector_id=sector.sector_id))
        duty_id = "duty-orphan-" + hashlib.sha256(str(fragment.first_position).encode()).hexdigest()[:12]
        active = Duty(
            duty_id=duty_id,
            rpt=None,
            rpt_printed=None,
            rpt_date=None,
            reporting_airport=sector.origin,
            reporting_iata=sector.origin_iata,
            sectors=[sector],
            source_position=fragment.first_position,
            validation_errors=[message],
        )
        sector.duty_id = duty_id
        duties.append(active)
    return duties


def _new_duty(
    sector: Sector,
    fragment: SectorFragment,
    issues: list[ReviewIssue],
) -> Duty:
    errors: list[str] = []
    rpt = None
    if "rpt" in fragment.unreadable or "start_date" in fragment.unreadable:
        message = f"{sector.flight_number or 'FLY row'} has an unreadable RPT or RPT date"
        errors.append(message)
        issues.append(ReviewIssue("critical_unreadable_rpt", message, fragment.first_position))
    elif sector.origin is None:
        errors.append("reporting airport is unknown")
    elif fragment.rpt_date is None:
        message = f"{sector.flight_number or 'FLY row'} RPT has no attributed date"
        errors.append(message)
        issues.append(ReviewIssue("missing_rpt_date", message, fragment.first_position))
    else:
        try:
            rpt = resolve_event(fragment.rpt_date, fragment.rpt or "", sector.origin)
        except (ValueError, TimeResolutionError) as exc:
            message = f"{sector.flight_number or 'FLY row'} RPT: {exc}"
            errors.append(message)
            issues.append(ReviewIssue("invalid_rpt", message, fragment.first_position))
    identity = "|".join(
        (
            sector.flight_number,
            sector.origin_iata,
            sector.destination_iata,
            rpt.utc_datetime.isoformat() if rpt else str(fragment.first_position),
        )
    )
    duty_id = "duty-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    sector.duty_id = duty_id
    return Duty(
        duty_id=duty_id,
        rpt=rpt,
        rpt_printed=fragment.rpt,
        rpt_date=fragment.rpt_date,
        reporting_airport=sector.origin,
        reporting_iata=sector.origin_iata,
        sectors=[sector],
        source_position=fragment.first_position,
        validation_errors=errors,
    )


def _continuous(previous: Sector, following: Sector) -> bool:
    if not previous.destination_iata or previous.destination_iata != following.origin_iata:
        return False
    if previous.sta is None or following.std is None:
        return False
    gap = following.std.utc_datetime - previous.sta.utc_datetime
    return timedelta(minutes=-20) <= gap <= timedelta(hours=18)
