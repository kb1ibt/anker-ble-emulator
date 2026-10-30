# Copyright (c) 2026 Shawn Stricker
"""Timed events: what the device does when a moment comes due."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .layouts import Value


@dataclass(frozen=True)
class Step:
    """What a timed event does when it comes due.

    Attributes:
        values: The telemetry values it sets.
        unset: The set values it clears, back to what the recordings carry.
        pushes: The msgtypes the MCU pushes from its recordings, in order.
        frames: The (msgtype, cleartext) pushes it builds itself, after those.
        then: The events it starts, timed from its own due time.
        disconnect: The device drops the link.

    """

    values: Mapping[str, Value] = field(default_factory=dict)
    unset: tuple[str, ...] = ()
    pushes: tuple[int, ...] = ()
    frames: tuple[tuple[int, bytes], ...] = ()
    then: tuple[Timed, ...] = ()
    disconnect: bool = False


#: A timed event's work: (the set values, the device's whole state: the recorded
#: values, set ones over them and the RTC under ``mcu.RTC``) -> its step.
TimedAction: TypeAlias = "Callable[[Mapping[str, Value], Mapping[str, Value]], Step]"


@dataclass(frozen=True)
class Timed:
    """An event ``delay`` seconds on; scheduling one replaces any with its ``key``.

    Attributes:
        delay: Seconds from when it is scheduled to when it comes due.
        key: What it is; a newer event with the same key replaces it.
        action: What it does when it comes due.
        link: It ends with the link.

    """

    delay: float
    key: str
    action: TimedAction
    link: bool = False
