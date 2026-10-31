# Copyright (c) 2026 Shawn Stricker
"""Live values: telemetry drawn from the recorded frequencies, once per period.

A table (``devices/data/<pn>_live.json``, built by ``tools/build_live_tables.py``
from the frame survey) holds, per message, the recorded samples of each group of
fields that move together, which groups were active together, the totals, the
slow fields' steps and ``remaining_time``'s split.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar


if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence
    from random import Random

    from .layouts import Value

T = TypeVar("T")
#: Each value with its recorded count.
Weighted = tuple[tuple[T, int], ...]

#: The field drawn from its output-idle or output-active split, and the total
#: that splits it (0 W is output idle).
REMAINING_TIME = "remaining_time"
SPLIT_BY = "total_output_power"
#: Periods a clock's float sum may fall short of a mark by and still reach it.
GRID_SLACK = 1e-9
#: A match cell that holds for any state value: the builder writes it where a
#: key doesn't bind the sample (an AC sample with no AC input, any charge limit).
ANY = "*"


@dataclass(frozen=True)
class LiveSource:
    """Where a device's live values come from.

    Attributes:
        table: The table's resource in ``devices/data/``.
        period: Seconds of the device's telemetry period: frames built within
            one share a draw.

    """

    table: str
    period: float


@dataclass(frozen=True)
class Group:
    """Fields that move together: a port's status, voltage, current and power.

    Attributes:
        name: The group's name, as the table's active sets list it.
        fields: Its fields, in sample order.
        live: The fields a draw sets; the rest are match keys.
        idle: The live fields' idle values, in ``live`` order.
        samples: The recorded samples, in ``fields`` then ``match`` order; a
            match cell may be ``ANY``.
        match: Further match keys from outside the group's own field (an AC
            input's switch and status).
        independent: Its activity is its samples' given its match keys (an
            input plugged in or not), not the section's active sets.

    """

    name: str
    fields: tuple[str, ...]
    live: tuple[str, ...]
    idle: tuple[Value, ...]
    samples: Weighted[tuple[Value | str, ...]]
    match: tuple[str, ...] = ()
    independent: bool = False

    def matching(self, state: Mapping[str, Value]) -> Weighted[tuple[Value, ...]]:
        """Return the samples whose match keys hold ``state``'s values, idle included.

        Each is in ``live`` order. A key the state doesn't hold matches nothing,
        unless the cell is ``ANY``.
        """
        columns = (*self.fields, *self.match)
        keys = [(i, name) for i, name in enumerate(columns) if name not in self.live]
        where = [columns.index(name) for name in self.live]
        matching = []
        for sample, count in self.samples:
            values = tuple(self._live(columns[i], sample[i]) for i in where)
            if all(
                sample[i] == ANY or (k in state and sample[i] == state[k])
                for i, k in keys
            ):
                matching.append((values, count))
        return tuple(matching)

    def drawable(self, state: Mapping[str, Value]) -> Weighted[tuple[Value, ...]]:
        """Return the matching samples that are active: off the idle values."""
        return tuple(item for item in self.matching(state) if item[0] != self.idle)

    def _live(self, field: str, cell: Value | str) -> Value:
        """Return a live field's sample value.

        Raises:
            TypeError: If the cell is a wildcard.

        """
        if isinstance(cell, str):
            msg = f"{self.name}: a wildcard in live field {field!r}"
            raise TypeError(msg)
        return cell


@dataclass(frozen=True)
class Paired:
    """Fields drawn given others: tuples recorded beside each given tuple.

    Attributes:
        given: The fields the draw matches on.
        fields: The fields it sets.
        samples: (given values, field values) pairs.
        unpaired: Frames with no pair: a draw of one holds the fields.

    """

    given: tuple[str, ...]
    fields: tuple[str, ...]
    samples: Weighted[tuple[tuple[Value, ...], tuple[Value, ...]]]
    unpaired: int


@dataclass(frozen=True)
class Slow:
    """A field that moves a recorded step per interval, within its recorded range.

    Attributes:
        field: The field.
        steps: The recorded steps.
        low: The least recorded value.
        high: The greatest recorded value.
        interval: Seconds between the recorded steps.

    """

    field: str
    steps: Weighted[float]
    low: float
    high: float
    interval: float


@dataclass(frozen=True)
class Section:
    """One message's live fields.

    Attributes:
        msgtype: The message.
        groups: Its groups.
        active_sets: The groups recorded active together.
        totals: Each total and the fields it sums.
        paired: Its paired draws.
        slow: Its slow fields.
        remaining_time: ``remaining_time`` with output idle, then active;
            None where the message has none.

    """

    msgtype: int
    groups: tuple[Group, ...]
    active_sets: Weighted[frozenset[str]]
    totals: tuple[tuple[str, tuple[str, ...]], ...]
    paired: tuple[Paired, ...]
    slow: tuple[Slow, ...]
    remaining_time: tuple[Weighted[Value], Weighted[Value]] | None


@dataclass(frozen=True)
class LiveTable:
    """A device's live fields, by message, in drawing order.

    Attributes:
        sections: The messages' live fields.

    """

    sections: tuple[Section, ...]

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LiveTable:
        """Return the table a built ``<pn>_live.json`` holds."""
        return cls(tuple(_section(section) for section in data["sections"]))

    @property
    def names(self) -> frozenset[str]:
        """Every field a draw reads or sets."""
        names: set[str] = set()
        for section in self.sections:
            for group in section.groups:
                names.update(group.fields, group.match)
            for paired in section.paired:
                names.update(paired.given, paired.fields)
            for field, parts in section.totals:
                names.update((field, *parts))
            names.update(slow.field for slow in section.slow)
            if section.remaining_time is not None:
                names.update((REMAINING_TIME, SPLIT_BY))
        return frozenset(names)


def _weighted(items: Sequence[Sequence[Any]]) -> tuple[tuple[Any, int], ...]:
    return tuple((item, int(count)) for item, count in items)


def _section(data: Mapping[str, Any]) -> Section:
    groups = tuple(
        Group(
            name=group["name"],
            fields=tuple(group["fields"]),
            live=tuple(group["live"]),
            idle=tuple(group["idle"]),
            samples=_weighted([(tuple(s), c) for s, c in group["samples"]]),
            match=tuple(group.get("match", ())),
            independent=bool(group.get("independent", False)),
        )
        for group in data["groups"]
    )
    paired = tuple(
        Paired(
            given=tuple(block["given"]),
            fields=tuple(block["fields"]),
            samples=_weighted(
                [
                    ((tuple(given), tuple(values)), c)
                    for (given, values), c in block["samples"]
                ]
            ),
            unpaired=int(block["unpaired"]),
        )
        for block in data.get("paired", [])
    )
    slow = tuple(
        Slow(
            field=block["field"],
            steps=_weighted(block["steps"]),
            low=min(value for value, _ in block["values"]),
            high=max(value for value, _ in block["values"]),
            interval=float(block["interval"]),
        )
        for block in data.get("slow", [])
    )
    remaining = data.get("remaining_time")
    return Section(
        msgtype=int(data["msgtype"]),
        groups=groups,
        active_sets=_weighted(
            [(frozenset(names), c) for names, c in data["active_sets"]]
        ),
        totals=tuple((t["field"], tuple(t["sum_of"])) for t in data.get("totals", [])),
        paired=paired,
        slow=slow,
        remaining_time=None
        if remaining is None
        else (
            _weighted(remaining["output_idle"]),
            _weighted(remaining["output_active"]),
        ),
    )


def _pick(rng: Random, items: Weighted[T]) -> T:
    """Return one of ``items``, as often as its count."""
    return rng.choices([item for item, _ in items], [count for _, count in items])[0]


def _number(name: str, value: Value) -> float:
    if isinstance(value, bytes):
        msg = f"live field {name!r} holds bytes, not a number"
        raise TypeError(msg)
    return value


class LiveValues:
    """Draws a device's live values from its table, once per period."""

    def __init__(self, table: LiveTable, period: float, rng: Random) -> None:
        """Draw from ``table`` with ``rng``, at most once per ``period`` seconds."""
        self.table = table
        self.period = period
        self._rng = rng
        #: The period grid's last mark; None before the first draw.
        self._mark: float | None = None
        #: Each slow field's last step mark, on its own interval's grid.
        self._slow_marks: dict[str, float] = {}

    def due(self, now: float) -> int | None:
        """Return the periods passed by ``now`` if a draw is due; None if not.

        The first draw is due at once and passes none. Marks fall on a grid
        from it, so when frames are built doesn't move the draws.
        """
        if self._mark is None:
            self._mark = now
            return 0
        periods = math.floor((now - self._mark) / self.period + GRID_SLACK)
        if periods < 1:
            return None
        self._mark += periods * self.period
        return periods

    def draw(
        self,
        state: Mapping[str, Value],
        pinned: Collection[str],
        now: float,
        derive: Callable[[Mapping[str, Value]], dict[str, Value]] | None = None,
    ) -> dict[str, Value]:
        """Return a draw of every live field neither pinned nor drawn already.

        A group with a pinned field isn't drawn at all, so its fields stay one
        recorded sample's.

        Args:
            state: The device's whole state.
            pinned: Fields a draw leaves as they are.
            now: The device-clock instant of the draw: a slow field takes a
                recorded step per interval passed since its last.
            derive: The layout's state-derived values, applied after each
                section so a later one reads them; never part of the draw.

        """
        drawn: dict[str, Value] = {}
        derived: dict[str, Value] = {}
        pins = frozenset(pinned)
        for section in self.table.sections:
            current = {**state, **drawn, **derived}
            drawn |= self._section(section, current, pins, pins | drawn.keys(), now)
            if derive is not None:
                derived = derive({**state, **drawn})
        return drawn

    def _slow_steps(self, slow: Slow, now: float) -> int:
        """Return the intervals passed since ``slow``'s last step; mark them.

        The first draw marks the field and steps none; the remainder carries.
        """
        mark = self._slow_marks.get(slow.field)
        if mark is None:
            self._slow_marks[slow.field] = now
            return 0
        steps = math.floor((now - mark) / slow.interval + GRID_SLACK)
        if steps < 1:
            return 0
        self._slow_marks[slow.field] = mark + steps * slow.interval
        return steps

    def _groups(
        self,
        section: Section,
        current: Mapping[str, Value],
        pinned: frozenset[str],
        held: frozenset[str],
    ) -> dict[str, Value]:
        """Return the groups' draw: an active set's, each independent group its own."""
        rng = self._rng
        groups = [
            g
            for g in section.groups
            if not set(g.live) & pinned and not set(g.live) <= held
        ]
        drawable = {g.name: g.drawable(current) for g in groups if not g.independent}
        possible = {name for name, samples in drawable.items() if samples}
        sets = tuple((names & possible, count) for names, count in section.active_sets)
        active = _pick(rng, sets) if sets else frozenset()
        out: dict[str, Value] = {}
        for group in groups:
            if group.independent:
                matching = group.matching(current)
                values = _pick(rng, matching) if matching else group.idle
            elif group.name in active:
                values = _pick(rng, drawable[group.name])
            else:
                values = group.idle
            out |= {
                n: v for n, v in zip(group.live, values, strict=True) if n not in held
            }
        return out

    def _section(
        self,
        section: Section,
        current: dict[str, Value],
        pinned: frozenset[str],
        held: frozenset[str],
        now: float,
    ) -> dict[str, Value]:
        """Return a section's draw; ``held`` (pinned or drawn already) stays."""
        rng = self._rng
        out = self._groups(section, current, pinned, held)
        current |= out
        for field, parts in section.totals:
            if field not in held:
                out[field] = current[field] = sum(_number(p, current[p]) for p in parts)
        for paired in section.paired:
            out |= self._paired(paired, current, held)
            current |= out
        for slow in section.slow:
            if slow.field in held:
                continue
            periods = self._slow_steps(slow, now)
            if periods < 1:
                continue
            value = _number(slow.field, current[slow.field])
            steps = [step for step, _ in slow.steps]
            counts = [count for _, count in slow.steps]
            for step in rng.choices(steps, counts, k=periods):
                value = min(max(value + step, slow.low), slow.high)
            out[slow.field] = current[slow.field] = value
        if section.remaining_time is not None and REMAINING_TIME not in held:
            idle, busy = section.remaining_time
            split = busy if _number(SPLIT_BY, current.get(SPLIT_BY, 0)) else idle
            if split:
                out[REMAINING_TIME] = _pick(rng, split)
        return out

    def _paired(
        self, paired: Paired, current: Mapping[str, Value], held: frozenset[str]
    ) -> dict[str, Value]:
        given = tuple(current[name] for name in paired.given)
        matched = tuple(
            (values, count) for (seen, values), count in paired.samples if seen == given
        )
        if not matched:
            return {}
        choice = _pick(self._rng, ((None, paired.unpaired), *matched))
        if choice is None:
            return {}
        return {
            n: v for n, v in zip(paired.fields, choice, strict=True) if n not in held
        }
