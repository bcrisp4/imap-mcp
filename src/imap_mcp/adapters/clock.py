"""The system clock."""

from __future__ import annotations

import time
from datetime import UTC, datetime


class SystemClock:
    """Reads the real time."""

    def now(self) -> datetime:
        """Return the current time, timezone-aware UTC."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return a monotonic time in seconds."""
        return time.monotonic()
