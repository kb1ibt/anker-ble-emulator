# Copyright (c) 2026 Shawn Stricker
"""Time sources for the module's timers and timed events."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Monotonic seconds."""

    def now(self) -> float:
        """Return the current time in seconds."""
        ...

    def bypassed(self, due: float) -> bool:
        """Whether a jump passed ``due``: its event sets its values, sends nothing."""
        ...


class MonotonicClock:
    """The process's monotonic clock."""

    def now(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()

    def bypassed(self, due: float) -> bool:  # noqa: ARG002  # never jumps
        """Return False: this clock never jumps."""
        return False


class ScaledClock:
    """Another clock run ``speed`` times as fast, from its time at creation."""

    def __init__(self, speed: float, source: Clock | None = None) -> None:
        """Run ``source`` (the monotonic clock if None) at ``speed``."""
        self._source = source or MonotonicClock()
        self._speed = speed
        self._start = self._source.now()

    def now(self) -> float:
        """Return the scaled time in seconds."""
        return self._start + (self._source.now() - self._start) * self._speed

    def bypassed(self, due: float) -> bool:  # noqa: ARG002  # never jumps
        """Return False: this clock never jumps."""
        return False


class ManualClock:
    """A clock that moves only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        """Start at ``start`` seconds."""
        self._now = start
        self._jumps: list[tuple[float, float]] = []

    def now(self) -> float:
        """Return the current time in seconds."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``; what comes due on the way runs."""
        self._now += seconds

    def jump(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``, bypassing what falls due between."""
        self._jumps.append((self._now, self._now + seconds))
        self._now += seconds

    def bypassed(self, due: float) -> bool:
        """Whether ``due`` falls strictly inside a jump."""
        return any(start < due < end for start, end in self._jumps)
