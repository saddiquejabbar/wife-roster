from __future__ import annotations

from copy import deepcopy
from typing import Any

from roster.models import RawTranscription
from roster.normalize import normalize_transcription
from roster.validator import validate_roster


def transcription(
    rows: list[dict[str, Any]],
    *,
    period_from: str = "01Aug37",
    period_to: str = "01Oct37",
    coverage: str = "FULL",
) -> RawTranscription:
    complete_rows = []
    for index, row in enumerate(rows):
        value = {
            "source_index": 0,
            "row_index": index,
            "start_date": None,
            "day": None,
            "flight_number": None,
            "sector": None,
            "duty": None,
            "rpt": None,
            "std": None,
            "sta": None,
            "flight_time": None,
            "remarks": None,
            "unreadable": [],
        }
        value.update(deepcopy(row))
        complete_rows.append(value)
    return RawTranscription.from_dict(
        {
            "schema_version": 1,
            "coverage": coverage,
            "report_header": {
                "period_from": period_from,
                "period_to": period_to,
                "port_local_notice_present": True,
            },
            "rows": complete_rows,
        }
    )


def build(rows: list[dict[str, Any]], **kwargs):
    roster = normalize_transcription(transcription(rows, **kwargs))
    validate_roster(roster)
    return roster


def fly(
    date: str,
    flight: str,
    route: str,
    rpt: str | None,
    std: str,
    sta: str,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "start_date": date,
        "flight_number": flight,
        "sector": route,
        "duty": "FLY",
        "rpt": rpt,
        "std": std,
        "sta": sta,
    }
    row.update(extra)
    return row
