from __future__ import annotations

from datetime import datetime
import sys
from typing import TextIO
from zoneinfo import ZoneInfo

from .models import Alert


SINGAPORE = ZoneInfo("Asia/Singapore")


class ConsoleNotifier:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def send(self, alert: Alert, *, sent_at: datetime) -> str:
        if sent_at.tzinfo is None:
            raise ValueError("sent_at must be timezone-aware")
        local = sent_at.astimezone(SINGAPORE)
        print(f"[{local:%Y-%m-%d %H:%M} SG]\n", file=self.stream)
        print(alert.message, file=self.stream)
        self.stream.flush()
        return f"console:{alert.event_key}"
