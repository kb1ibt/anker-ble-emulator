# Copyright (c) 2026 Shawn Stricker
"""The C Gen 2 ``c490`` summary: a nanopb message whose fields are set by name.

The display board posts ``a1 01 31``, then ``a2`` (a 2-byte length, type ``04``
and the message), then the schema name in ``a3``. The pack sections ``.14`` and
``.15`` always hold two entries; the second is the expansion's, all zero when
none is attached.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from construct import (
    Const,
    GreedyBytes,
    GreedyRange,
    Int16ul,
    Prefixed,
    Struct,
    Switch,
    VarInt,
    this,
)

from .layouts import LayoutError
from .messages import ROUTE_FIELD


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from .layouts import Value

#: The summary's msgtype.
SUMMARY_MSGTYPE = 0x490
WIRE_VARINT = 0
WIRE_LEN = 2
#: Top-level fields that are messages; every field inside them is a leaf.
SUBMESSAGES = frozenset({2, 3, 6, 9, 11, 12, 14, 15, 18, 19, 20, 21, 23, 24, 26, 27})
#: The expansion's pack entries and its SoC.
EXPANSION_PREFIXES = (".14#1.", ".15#1.")
EXPANSION_SOC = ".23.1#1"

PB_FIELD = Struct(
    "key" / VarInt,
    "value"
    / Switch(
        this.key & 7, {WIRE_VARINT: VarInt, WIRE_LEN: Prefixed(VarInt, GreedyBytes)}
    ),
)
PB_MESSAGE = GreedyRange(PB_FIELD)
SUMMARY_LAYOUT = Struct(
    "route" / ROUTE_FIELD,
    "tag" / Const(b"\xa2"),
    "body"
    / Prefixed(Int16ul, Struct("type" / Const(b"\x04"), "message" / GreedyBytes)),
    "rest" / GreedyBytes,
)

#: How a leaf's value is written: a varint, a zigzag varint, or fixed bytes.
Kind = Literal["uint", "sint", "bytes"]
#: A leaf as the wire holds it.
Leaf = int | bytes


@dataclass(frozen=True)
class SummaryField:
    """A summary leaf: its path (``.14#1.3`` is field 3 of the second ``.14``)."""

    path: str
    kind: Kind


class Summary:
    """The summary's named fields, read from and set in a recorded payload."""

    def __init__(self, fields: Mapping[str, SummaryField]) -> None:
        """Index the fields by name and by path."""
        self.fields = dict(fields)
        self._by_path = {field.path: field for field in fields.values()}

    @classmethod
    def from_json(cls, data: Mapping[str, Sequence[Any]]) -> Summary:
        """Read ``{name: [path, kind]}``."""
        return cls(
            {name: SummaryField(path, kind) for name, (path, kind) in data.items()}
        )

    def check(self, name: str, value: Value) -> None:
        """Refuse a value the named field can't hold.

        Raises:
            LayoutError: If the summary has no such field.
            TypeError: If a byte field gets a number, or a number field bytes.

        """
        field = self.fields.get(name)
        if field is None:
            msg = f"the summary has no field {name!r}"
            raise LayoutError(msg)
        wants_bytes = field.kind == "bytes"
        if wants_bytes != isinstance(value, bytes) or isinstance(value, float):
            msg = f"summary field {name!r} ({field.kind}) can't hold {value!r}"
            raise TypeError(msg)

    def without_expansion(self) -> dict[str, Value]:
        """Return the values of a summary with no expansion attached: all zero."""
        return {
            name: b"" if field.kind == "bytes" else 0
            for name, field in self.fields.items()
            if field.path.startswith(EXPANSION_PREFIXES) or field.path == EXPANSION_SOC
        }

    def read(self, payload: bytes) -> dict[str, Value]:
        """Return the named values a summary payload carries."""
        leaves: dict[str, Leaf] = {}

        def keep(path: str, leaf: Leaf) -> Leaf:
            leaves[path] = leaf
            return leaf

        _walk(SUMMARY_LAYOUT.parse(payload).body.message, "", keep)
        return {
            name: _decode(field.kind, leaves[field.path])
            for name, field in self.fields.items()
            if field.path in leaves
        }

    def update(self, payload: bytes, values: Mapping[str, Value]) -> bytes:
        """Return a summary payload with the named values it holds set.

        Names it doesn't hold are left to the device's other messages; a byte
        field keeps its recorded length.
        """
        by_path = {
            self.fields[name].path: value
            for name, value in values.items()
            if name in self.fields
        }
        if not by_path:
            return payload

        def put(path: str, leaf: Leaf) -> Leaf:
            if path not in by_path:
                return leaf
            return _encode(self._by_path[path].kind, by_path[path], leaf)

        frame = SUMMARY_LAYOUT.parse(payload)
        message = _walk(frame.body.message, "", put)
        return bytes(
            SUMMARY_LAYOUT.build(
                {"route": frame.route, "body": {"message": message}, "rest": frame.rest}
            )
        )


def _walk(data: bytes, prefix: str, visit: Callable[[str, Leaf], Leaf]) -> bytes:
    """Rebuild a message with each leaf replaced by ``visit(path, leaf)``."""
    seen: Counter[int] = Counter()
    fields: list[dict[str, Any] | None] = []
    for item in PB_MESSAGE.parse(data):
        number = item.key >> 3
        path = f"{prefix}.{number}" + (f"#{seen[number]}" if seen[number] else "")
        seen[number] += 1
        value: Leaf
        if not prefix and number in SUBMESSAGES:
            value = _walk(item.value, path, visit)
        else:
            value = visit(path, item.value)
        fields.append({"key": item.key, "value": value})
    return bytes(PB_MESSAGE.build(fields))


def _encode(kind: Kind, value: Value, recorded: Leaf) -> Leaf:
    """Return ``value`` as a leaf of ``kind``, a byte field at its recorded length.

    Raises:
        TypeError: If a number leaf gets anything but an integer.

    """
    if isinstance(value, bytes) and isinstance(recorded, bytes):
        return value[: len(recorded)].ljust(len(recorded), b"\x00")
    if not isinstance(value, int):
        msg = f"a {kind} leaf takes an integer, got {value!r}"
        raise TypeError(msg)
    return (value << 1) ^ (value >> 63) if kind == "sint" else value


def _decode(kind: Kind, leaf: Leaf) -> Value:
    """Return a leaf's value: a zigzag varint unfolded to its signed integer."""
    if kind == "sint" and isinstance(leaf, int):
        return (leaf >> 1) ^ -(leaf & 1)
    return leaf
