# Copyright (c) 2026 Shawn Stricker
"""Time sources for the module's timers."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Monotonic seconds."""

    def now(self) -> float:
        """Return the current time in seconds."""
        ...


class MonotonicClock:
    """The process's monotonic clock."""

    def now(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()


class ManualClock:
    """A clock that moves only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        """Start at ``start`` seconds."""
        self._now = start

    def now(self) -> float:
        """Return the current time in seconds."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self._now += seconds
