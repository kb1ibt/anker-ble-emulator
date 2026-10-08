# Copyright (c) 2026 Shawn Stricker
"""Cross-check SolixBLE's device classes against anker-solix-api's maps.

Per product: each property's decode position against the map's typed field or
part there (agree, gap, conflict, or absent from the map), and each command's
link to the telemetry it changes. A link comes from the map's ``state`` on the
command variant whose selector the SolixBLE parameters name, verified when a
property reads that position; failing that, from the method's name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anker_ble_emulator.layouts import NUMERIC_SIZES, TYPE_STR, TYPE_STRB


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from anker_ble_emulator.layouts import Field, Layout

#: Gen-1 status frames, which anker-solix-api maps as the MQTT ``0405``.
FALLBACK = {0x402: 0x405, 0x840: 0x405}
#: Method-name prefixes and suffixes stripped to find the property it sets.
VERBS = ("turn_", "set_", "enable_", "disable_", "start_", "stop_")
SUFFIXES = ("_on", "_off", "_enabled", "_disabled")


@dataclass(frozen=True)
class Position:
    """A decode position: the message, the field's tag, and the offset in it."""

    msgtype: int
    tag: int
    offset: int


@dataclass
class Link:
    """What one SolixBLE command changes in the map's telemetry."""

    method: str
    cmd: int | None
    #: ``map`` (a state the map names), ``position`` (one a property reads),
    #: ``name`` (by the method's name), ``ambiguous`` or ``none``.
    verdict: str
    states: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()


@dataclass
class Check:
    """One product's cross-check."""

    pn: str
    solixble_class: str
    agree: int = 0
    conflicts: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)


def source(layout: Layout, msgtype: int, tag: int) -> int | None:
    """Return the layout message that types ``tag`` of ``msgtype``, or None."""
    for candidate in (msgtype, FALLBACK.get(msgtype)):
        if candidate in layout.messages and any(
            f.tag == tag for f in layout.messages[candidate]
        ):
            return candidate
    return None


def _where(read: Mapping[str, Any]) -> str:
    begin, end = read["begin"], read["end"]
    start = "" if begin is None else begin - 1
    stop = "" if end is None else end - 1
    return f"{read['tag']}[{start}:{stop}]"


def compare(read: Mapping[str, Any], spec: Field) -> tuple[str, str]:
    """Return ``(verdict, detail)`` of one decode read against the map's field."""
    begin, end = read["begin"], read["end"]
    start = 0 if begin is None else begin - 1
    width = None if end is None else end - (begin or 0)
    name = spec.name or "(unnamed)"
    if spec.parts:
        return _compare_parts(read["parse"], start, width, spec)
    if start:
        return "gap", f"map keeps {spec.tag:02x} whole as {name}"
    if width is not None and width != spec.size:
        return "conflict", f"width {width} vs map {name} {spec.size}"
    if read["parse"] == "string" and spec.kind in NUMERIC_SIZES:
        return "conflict", f"string vs map {name} type {spec.kind:02x}"
    return "agree", ""


def _compare_parts(
    parse: str, start: int, width: int | None, spec: Field
) -> tuple[str, str]:
    for part in spec.parts:
        # A length-prefixed string's part starts at its length byte.
        string = parse == "string" and part.kind in (TYPE_STR, TYPE_STRB)
        if part.offset == start or (string and part.offset == start - 1):
            if width is not None and part.offset == start and width != part.size:
                return "conflict", f"width {width} vs map {part.name} {part.size}"
            return "agree", ""
    stop = None if width is None else start + width
    overlap = [
        part
        for part in spec.parts
        if part.offset + part.size > start and (stop is None or part.offset < stop)
    ]
    if overlap:
        names = ", ".join(f"{p.name}@{p.offset}/{p.size}" for p in overlap)
        return "conflict", f"overlaps map {names}"
    return "gap", f"map names nothing at {spec.tag:02x}[{start}]"


def _streams(facts: Mapping[str, Any]) -> dict[str, list[int]]:
    return {
        "stream": [int(cmd, 16) for cmd in facts["telemetry"]],
        "snapshot": [int(cmd, 16) for cmd in facts["snapshot"]],
    }


def positions(
    facts: Mapping[str, Any], layout: Layout, check: Check
) -> dict[Position, list[str]]:
    """Compare every property read into ``check``; return the positions read.

    A whole-record read names no position, so it neither agrees nor links.
    """
    streams = _streams(facts)
    read_at: dict[Position, list[str]] = {}
    for prop, reads in facts["properties"].items():
        for read in reads:
            if read["parse"] == "record" or not isinstance(read["tag"], str):
                continue
            tag = int(read["tag"], 16)
            msgtype = next(
                (
                    m
                    for m in streams[read["source"]]
                    if source(layout, m, tag) is not None
                ),
                None,
            )
            if msgtype is None:
                check.absent.append(f"{prop} {_where(read)} ({read['source']})")
                continue
            typed_by = source(layout, msgtype, tag)
            if typed_by is None:  # pragma: no cover  # found above
                continue
            spec = next(f for f in layout.messages[typed_by] if f.tag == tag)
            verdict, detail = compare(read, spec)
            if verdict == "agree":
                check.agree += 1
            else:
                line = f"{prop} {_where(read)} vs {typed_by:04x}: {detail}"
                (check.conflicts if verdict == "conflict" else check.gaps).append(line)
            offset = 0 if read["begin"] is None else read["begin"] - 1
            read_at.setdefault(Position(typed_by, tag, offset), []).append(prop)
    return read_at


def variant(
    layout: Layout, cmd: int, fields: Mapping[str, Any]
) -> Mapping[int, Mapping[str, Any]] | None:
    """Return the map's variant of ``cmd`` whose selector defaults ``fields`` carry."""
    variants = layout.commands.get(cmd, [])
    for command in variants:
        if all(
            fields.get(f"{tag:02x}") == spec["default"]
            for tag, spec in command.items()
            if "default" in spec
            and "state" not in spec
            and len({str(v.get(tag, {}).get("default")) for v in variants}) > 1
        ):
            return command
    return None


def located(layout: Layout, name: str) -> Iterable[Position]:
    """Return the positions of a telemetry field or part called ``name``."""
    for msgtype, fields in layout.messages.items():
        for spec in fields:
            if spec.name == name:
                yield Position(msgtype, spec.tag, 0)
            for part in spec.parts:
                if part.name == name:
                    yield Position(msgtype, spec.tag, part.offset)


def _stem(method: str) -> str:
    stem = next((method[len(v) :] for v in VERBS if method.startswith(v)), method)
    return next((stem[: -len(s)] for s in SUFFIXES if stem.endswith(s)), stem)


def link(
    command: Mapping[str, Any],
    layout: Layout,
    read_at: Mapping[Position, list[str]],
    properties: Mapping[str, Any],
) -> Link:
    """Return what one SolixBLE command changes."""
    method, cmd = command["method"], command["cmd"]
    msgtype = None if cmd is None else int(cmd, 16)
    chosen = None if msgtype is None else variant(layout, msgtype, command["fields"])
    states = tuple(
        sorted({spec["state"] for spec in (chosen or {}).values() if "state" in spec})
    )
    if states:
        hits = sorted(
            {
                prop
                for state in states
                for position in located(layout, state)
                for prop in read_at.get(position, [])
            }
        )
        return Link(method, msgtype, "position" if hits else "map", states, tuple(hits))
    stem = _stem(method)
    named = tuple(
        sorted(
            p
            for p in properties
            if p == stem or p.startswith(stem + "_") or p.endswith("_" + stem)
        )
    )
    names = tuple(
        sorted(
            {
                name
                for prop in named
                for position, props in read_at.items()
                if prop in props
                for name in _named_at(layout, position)
            }
        )
    )
    if len(names) == 1:
        return Link(method, msgtype, "name", names, named)
    return Link(method, msgtype, "ambiguous" if names else "none", names, named)


def _named_at(layout: Layout, position: Position) -> Iterable[str]:
    for spec in layout.messages.get(position.msgtype, ()):
        if spec.tag != position.tag:
            continue
        if position.offset == 0 and spec.name and not spec.parts:
            yield spec.name
        for part in spec.parts:
            if part.offset == position.offset:
                yield part.name


def cross_check(
    pn: str, solixble_class: str, facts: Mapping[str, Any], layout: Layout
) -> Check:
    """Return one product's cross-check of its SolixBLE class against its map."""
    check = Check(pn, solixble_class)
    read_at = positions(facts, layout, check)
    check.links = [
        link(command, layout, read_at, facts["properties"])
        for command in facts["commands"]
    ]
    return check
