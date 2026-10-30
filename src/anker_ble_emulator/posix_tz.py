# Copyright (c) 2026 Shawn Stricker
"""A POSIX TZ string (``0022 a5``): its UTC offset at a moment, and its next change.

The module stores the string and its TZ timer sends the MCU the zone's current
offset (a1783-control.md "Clock"), so DST switches with no new ``0022``.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass


_NAME = r"(?:[A-Za-z]{3,}|<[^>]+>)"
_OFFSET = r"[+-]?\d{1,2}(?::\d{2}){0,2}"
_DATE = r"(?:M\d{1,2}\.\d\.\d|J\d{1,3}|\d{1,3})"
_TZ = re.compile(
    rf"{_NAME}(?P<std>{_OFFSET})"
    rf"(?:(?P<dst_name>{_NAME})(?P<dst>{_OFFSET})?"
    rf"(?:,(?P<start>{_DATE})(?:/(?P<start_time>{_OFFSET}))?"
    rf",(?P<end>{_DATE})(?:/(?P<end_time>{_OFFSET}))?)?)?"
)
#: A rule's switch time when none is given: 02:00 local.
DEFAULT_SWITCH = 2 * 3600
#: POSIX ``Mm.w.d``: week 5 is the month's last ``d``.
LAST_WEEK = 5
#: ``Jn`` counts days 1-365 and never Feb 29, which falls on day 60 of a leap year.
FEB_29 = 60


def _seconds(text: str) -> int:
    """Return ``[+-]hh[:mm[:ss]]`` in seconds."""
    sign = -1 if text.startswith("-") else 1
    parts = [int(part) for part in text.lstrip("+-").split(":")]
    hours, minutes, seconds = [*parts, 0, 0][:3]
    return sign * (hours * 3600 + minutes * 60 + seconds)


def _day(year: int, rule: str) -> int:
    """Return local midnight of ``rule``'s day in ``year``, as epoch seconds."""
    if rule.startswith("M"):
        month, week, weekday = (int(part) for part in rule[1:].split("."))
        first = dt.date(year, month, 1)
        day = 1 + (weekday - (first.weekday() + 1)) % 7 + (week - 1) * 7
        last = calendar.monthrange(year, month)[1]
        while day > last:
            day -= 7
        date = dt.date(year, month, day)
    elif rule.startswith("J"):
        number = int(rule[1:])
        date = dt.date(year, 1, 1) + dt.timedelta(days=number - 1)
        if calendar.isleap(year) and number >= FEB_29:
            date += dt.timedelta(days=1)
    else:
        date = dt.date(year, 1, 1) + dt.timedelta(days=int(rule))
    return calendar.timegm(date.timetuple())


@dataclass(frozen=True)
class PosixTz:
    """A zone's standard and DST offsets, seconds west of UTC, and its DST rules.

    Attributes:
        std_west: The standard offset.
        dst_west: The DST offset; None for a zone without DST (or without rules).
        start: The rule DST starts on, in standard local time at ``start_time``.
        start_time: Seconds after local midnight it starts.
        end: The rule DST ends on, in DST local time at ``end_time``.
        end_time: Seconds after local midnight it ends.

    """

    std_west: int
    dst_west: int | None = None
    start: str = ""
    start_time: int = DEFAULT_SWITCH
    end: str = ""
    end_time: int = DEFAULT_SWITCH

    @classmethod
    def parse(cls, text: str) -> PosixTz:
        """Return the zone ``text`` spells, ``std offset [dst [offset] [,start,end]]``.

        A DST name with no rules keeps standard time all year.

        Raises:
            ValueError: If ``text`` isn't a POSIX TZ string.

        """
        match = _TZ.fullmatch(text)
        if match is None:
            msg = f"not a POSIX TZ string: {text!r}"
            raise ValueError(msg)
        std = _seconds(match["std"])
        if match["dst_name"] is None or match["start"] is None:
            return cls(std)
        dst = std - 3600 if match["dst"] is None else _seconds(match["dst"])
        return cls(
            std,
            dst,
            match["start"],
            DEFAULT_SWITCH
            if match["start_time"] is None
            else _seconds(match["start_time"]),
            match["end"],
            DEFAULT_SWITCH
            if match["end_time"] is None
            else _seconds(match["end_time"]),
        )

    def offset_west(self, utc: int) -> int:
        """Return the zone's offset at ``utc``, seconds west of UTC."""
        dst = self.dst_west
        if dst is None:
            return self.std_west
        start, end = self._changes(self._year(utc), dst)
        in_dst = start <= utc < end if start < end else not end <= utc < start
        return dst if in_dst else self.std_west

    def next_change(self, utc: int) -> int | None:
        """Return the first instant after ``utc`` the offset changes; None if never."""
        dst = self.dst_west
        if dst is None:
            return None
        year = self._year(utc)
        return min(
            change
            for near in (year - 1, year, year + 1)
            for change in self._changes(near, dst)
            if change > utc
        )

    def _year(self, utc: int) -> int:
        """Return the year of ``utc`` in standard local time."""
        return dt.datetime.fromtimestamp(utc - self.std_west, dt.UTC).year

    def _changes(self, year: int, dst_west: int) -> tuple[int, int]:
        """Return ``year``'s DST start and end instants, UTC epoch seconds."""
        start = _day(year, self.start) + self.start_time + self.std_west
        end = _day(year, self.end) + self.end_time + dst_west
        return start, end
