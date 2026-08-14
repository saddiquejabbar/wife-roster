from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

from .models import Alert, AlertType, Roster
from .formatter import format_event_time


def calculate_alerts(
    roster: Roster,
    *,
    now: datetime | None = None,
    include_past: bool = False,
) -> list[Alert]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    alerts: list[Alert] = []
    for duty in roster.duties:
        if duty.valid_for_alerts and duty.reporting_airport is not None:
            first = duty.sectors[0]
            if first.valid and first.origin and first.destination and duty.rpt:
                route = f"{first.flight_number} {first.origin_iata} → {first.destination_iata}"
                rpt_display = format_event_time(duty.rpt, duty.reporting_airport)
                for alert_type, delta, heading in (
                    (AlertType.PREP_12H, timedelta(hours=12), "12h to flight"),
                    (AlertType.PREP_3H, timedelta(hours=3), "Time to get ready"),
                ):
                    due = duty.rpt.utc_datetime - delta
                    if include_past or due > current:
                        alerts.append(
                            Alert(
                                event_key=make_event_key(
                                    alert_type,
                                    first.flight_number,
                                    first.origin_iata,
                                    first.destination_iata,
                                    duty.rpt.utc_datetime,
                                ),
                                alert_type=alert_type,
                                due_utc=due,
                                message=f"{heading}\n{route}\nRPT {rpt_display}",
                                duty_id=duty.duty_id,
                            )
                        )
        for sector in duty.sectors:
            if not sector.valid or sector.sta is None or sector.destination is None:
                continue
            due = sector.sta.utc_datetime - timedelta(hours=1)
            if not include_past and due <= current:
                continue
            destination = sector.destination.display_name
            message = (
                f"{sector.flight_number} landing in {destination} in 1h\n"
                f"ETA {format_event_time(sector.sta, sector.destination)}"
            )
            alerts.append(
                Alert(
                    event_key=make_event_key(
                        AlertType.LANDING_1H,
                        sector.flight_number,
                        sector.origin_iata,
                        sector.destination_iata,
                        sector.sta.utc_datetime,
                    ),
                    alert_type=AlertType.LANDING_1H,
                    due_utc=due,
                    message=message,
                    duty_id=duty.duty_id,
                    sector_id=sector.sector_id,
                )
            )
    return sorted(alerts, key=lambda alert: (alert.due_utc, alert.event_key))


def make_event_key(
    alert_type: AlertType,
    flight_number: str,
    origin: str,
    destination: str,
    event_utc: datetime,
) -> str:
    if event_utc.tzinfo is None:
        raise ValueError("event UTC datetime must be timezone-aware")
    identity = "|".join(
        (
            alert_type.value,
            flight_number,
            origin,
            destination,
            event_utc.astimezone(UTC).isoformat(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
