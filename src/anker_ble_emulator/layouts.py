# Copyright (c) 2026 Shawn Stricker
"""Device layouts from anker-solix-api's maps: named telemetry fields and commands.

A layout gives each telemetry field its tag, type byte and length (learned from
recordings, ``tools/import_maps.py``) and each command its fields' accepted
values. It builds telemetry from zeros, sets named values in recorded or built
payloads, and answers whether a command's values are accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any

from construct import (
    BytesInteger,
    Construct,
    ConstructError,
    Float32l,
    GreedyBytes,
    Int8ub,
    Struct,
)

from .messages import BLE_REPLY_ROUTE, ROUTE_FIELD
from .tlv import FieldError, decode_fields, encode_fields


if TYPE_CHECKING:
    from collections.abc import Mapping

#: A layout value: an integer for numeric types, bytes for strings and blocks.
Value = int | float | bytes

#: Type bytes (anker-solix-api ``DeviceHexDataTypes``).
TYPE_STR = 0x00
TYPE_UI = 0x01
TYPE_SILE = 0x02
TYPE_VAR = 0x03
TYPE_BIN = 0x04
TYPE_SFLE = 0x05
TYPE_STRB = 0x06
#: Value sizes of the fixed-width numeric types.
NUMERIC_SIZES = {TYPE_UI: 1, TYPE_SILE: 2, TYPE_VAR: 4, TYPE_SFLE: 4}
#: A typed field: its type byte, then the value.
TYPED_FIELD = Struct("type" / Int8ub, "value" / GreedyBytes)
#: Command fields every request carries, which are not settings.
FRAME_TAGS = frozenset({0xA1, 0xFD, 0xFE})
#: The lowest field tag; a reply's first byte below it is its status.
FIRST_TAG = 0xA1

STATUS_ACCEPTED = 0x00
STATUS_REJECTED = 0x04


class LayoutError(KeyError):
    """A message or field the layout doesn't hold."""


def numeric(kind: int, size: int) -> Construct[Any, Any]:
    """Return the little-endian layout of a numeric value of ``size`` bytes."""
    if kind == TYPE_SFLE:
        return Float32l
    return BytesInteger(size, signed=kind == TYPE_SILE, swapped=True)


def encode_value(kind: int, size: int, value: Value) -> bytes:
    """Return ``value`` as ``size`` bytes of type ``kind``.

    Raises:
        TypeError: If a numeric type gets bytes, or a byte type a number.

    """
    if kind in NUMERIC_SIZES:
        if isinstance(value, bytes):
            msg = f"type {kind:02x} takes a number, got bytes"
            raise TypeError(msg)
        return numeric(kind, size).build(value)
    if not isinstance(value, bytes):
        msg = f"type {kind:02x} takes bytes, got {value!r}"
        raise TypeError(msg)
    return value[:size].ljust(size, b"\x00")


@dataclass(frozen=True)
class Part:
    """A named value inside a field's value, at a fixed offset."""

    name: str
    offset: int
    kind: int
    size: int


@dataclass(frozen=True)
class Field:
    """A telemetry field: its tag, name, type byte and length, and named parts."""

    tag: int
    name: str | None
    kind: int
    length: int
    parts: tuple[Part, ...] = ()

    @property
    def size(self) -> int:
        """The value's size after the type byte."""
        return self.length - 1


class Layout:
    """One product's telemetry layouts and commands."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        """Read a layout from its JSON form (``tools/import_maps.py``)."""
        self.pn: str = data["pn"]
        self.source: str = data.get("source", "")
        self.messages = {
            int(msgtype, 16): _fields(message["fields"])
            for msgtype, message in data["messages"].items()
        }
        self.commands: dict[int, list[dict[int, dict[str, Any]]]] = {
            int(msgtype, 16): [
                {int(tag, 16): spec for tag, spec in command["fields"].items()}
                for command in commands
            ]
            for msgtype, commands in data["commands"].items()
        }

    @classmethod
    def load(cls, pn: str) -> Layout | None:
        """Return the packaged layout of ``pn``; None if it has none."""
        text = resources.files(__package__).joinpath("maps", f"{pn.lower()}.json")
        if not text.is_file():
            return None
        return cls(json.loads(text.read_text()))

    def alias(self, aliases: Mapping[int, int]) -> None:
        """Type each message in ``aliases`` by the message it names.

        Raises:
            LayoutError: If a named message isn't in the layout.

        """
        for msgtype, source in aliases.items():
            self.messages[msgtype] = self._message(source)

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Return a telemetry payload of the message's typed fields, zero unless set.

        Raises:
            LayoutError: If the layout has no such message.

        """
        fields = self._message(msgtype)
        template = ROUTE_FIELD.build({"route": BLE_REPLY_ROUTE}) + encode_fields(
            (field.tag, bytes([field.kind]) + bytes(field.size))
            for field in fields
            if field.tag != 0xA1  # noqa: PLR2004  # the route is built above
        )
        return self.update(msgtype, template, values or {})

    def update(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> bytes:
        """Return a telemetry payload with named values set.

        Args:
            msgtype: The message the payload is.
            payload: A recorded or built payload; a reply's status byte is kept.
            values: Values by field or part name.

        Raises:
            LayoutError: If a name isn't a typed field or part of the message.

        """
        by_name = {field.name: field for field in self._message(msgtype) if field.name}
        parts = {
            part.name: (field, part)
            for field in self._message(msgtype)
            for part in field.parts
        }
        status = payload[:1] if payload and payload[0] < FIRST_TAG else b""
        current = decode_fields(payload[len(status) :])
        for name, value in values.items():
            if name in by_name:
                field = by_name[name]
                current[field.tag] = bytes([field.kind]) + encode_value(
                    field.kind, field.size, value
                )
            elif name in parts:
                field, part = parts[name]
                old = bytearray(
                    current.get(field.tag, bytes([field.kind]) + bytes(field.size))
                )
                start = 1 + part.offset
                old[start : start + part.size] = encode_value(
                    part.kind, part.size, value
                )
                current[field.tag] = bytes(old)
            else:
                msg = f"{self.pn} {msgtype:03x} has no typed field {name!r}"
                raise LayoutError(msg)
        return status + encode_fields(current.items())

    def has_command(self, msgtype: int) -> bool:
        """Whether the layout maps a command to ``msgtype``."""
        return msgtype in self.commands

    def check(self, msgtype: int, plaintext: bytes) -> int:
        """Return the status a command's values earn: accepted, or rejected.

        A request whose fields don't walk, or whose setting is outside its
        accepted values, is rejected; one setting nothing is accepted.

        Raises:
            LayoutError: If the layout has no such command.

        """
        if self._accepted(msgtype, plaintext) is None:
            return STATUS_REJECTED
        return STATUS_ACCEPTED

    def state_changes(self, msgtype: int, plaintext: bytes) -> dict[str, Value]:
        """Return the telemetry values an accepted command sets, by field name.

        A rejected or unmapped command sets none; so does a setting the map
        links to no typed telemetry field.
        """
        if msgtype not in self.commands:
            return {}
        accepted = self._accepted(msgtype, plaintext)
        if accepted is None:
            return {}
        command, settings = accepted
        changes: dict[str, Value] = {}
        for tag, raw in settings.items():
            spec = command[tag]
            state = spec.get("state")
            if state is None or not self.locate(state):
                continue
            value = _value(raw)
            if (table := spec.get("state_values")) is not None:
                value = table.get(str(value))
            if value is not None:
                changes[state] = value
        return changes

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names of a message's typed fields and parts; none if unmapped."""
        return frozenset(
            name
            for field in self.messages.get(msgtype, ())
            for name in (field.name, *(part.name for part in field.parts))
            if name
        )

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages with a typed field or part called ``name``."""
        return tuple(
            msgtype for msgtype in self.messages if name in self.names(msgtype)
        )

    def _accepted(
        self, msgtype: int, plaintext: bytes
    ) -> tuple[dict[int, dict[str, Any]], dict[int, bytes]] | None:
        """Return the command and its settings if every value is accepted."""
        matched = self._settings(msgtype, plaintext)
        if matched is None:
            return None
        command, settings = matched
        if all(_accepts(command[tag], value) for tag, value in settings.items()):
            return matched
        return None

    def _settings(
        self, msgtype: int, plaintext: bytes
    ) -> tuple[dict[int, dict[str, Any]], dict[int, bytes]] | None:
        """Return the command a request is and its settings; None if it doesn't walk.

        Raises:
            LayoutError: If the layout has no such command.

        """
        if msgtype not in self.commands:
            msg = f"{self.pn} has no command {msgtype:03x}"
            raise LayoutError(msg)
        try:
            fields = decode_fields(plaintext)
        except FieldError:
            return None
        commands = self.commands[msgtype]
        for command in commands:
            settings = {
                tag: fields[tag]
                for tag in command
                if tag in fields and tag not in FRAME_TAGS
            }
            if settings:
                return command, settings
        return commands[0], {}

    def _message(self, msgtype: int) -> tuple[Field, ...]:
        if msgtype not in self.messages:
            msg = f"{self.pn} has no message {msgtype:03x}"
            raise LayoutError(msg)
        return self.messages[msgtype]


def _fields(specs: list[dict[str, Any]]) -> tuple[Field, ...]:
    """Return the typed fields of a message; untyped ones are left out."""
    fields = []
    for spec in specs:
        if "type" not in spec or "length" not in spec:
            continue
        parts = tuple(
            Part(
                part["name"],
                part["offset"],
                int(part["type"], 16),
                part.get("length", NUMERIC_SIZES.get(int(part["type"], 16), 1)),
            )
            for part in spec.get("bytes", ())
            if part.get("name") and "type" in part and "offset" in part
        )
        fields.append(
            Field(
                int(spec["tag"], 16),
                spec.get("name"),
                int(spec["type"], 16),
                spec["length"],
                parts,
            )
        )
    return tuple(fields)


def _value(raw: bytes) -> Value | None:
    """Return a typed command value: a number, or the bytes after the type."""
    try:
        typed = TYPED_FIELD.parse(raw)
    except ConstructError:
        return None
    kind = int(typed.type)
    if kind not in NUMERIC_SIZES:
        return bytes(typed.value)
    if not typed.value:
        return None
    value: Value = numeric(kind, len(typed.value)).parse(typed.value)
    return value


def _accepts(spec: Mapping[str, Any], raw: bytes) -> bool:
    """Whether a command field's typed value is one the map accepts."""
    value = _value(raw)
    if value is None:
        return False
    return isinstance(value, bytes) or _in_range(spec, value)


def _in_range(spec: Mapping[str, Any], value: float) -> bool:
    if "options" in spec:
        return value in spec["options"]
    low, high, step = spec.get("min"), spec.get("max"), spec.get("step")
    if (low is not None and value < low) or (high is not None and value > high):
        return False
    return not (step and (value - (low or 0)) % step)
