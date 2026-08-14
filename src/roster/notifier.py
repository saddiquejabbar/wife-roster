from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import Alert


class Notifier(Protocol):
    """Deliver one already-rendered alert and return a provider notification ID."""

    def send(self, alert: Alert, *, sent_at: datetime) -> str: ...
