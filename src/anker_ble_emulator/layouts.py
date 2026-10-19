# Copyright (c) 2026 Shawn Stricker
"""Shared types and codecs for a product's hand-written telemetry layout."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from construct import (
    Bytes,
    BytesInteger,
    Construct,
    Struct,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

#: A layout value: an integer for numeric types, bytes for strings and blocks.
Value = int | float | bytes


class LayoutProtocol(Protocol):
    """What ``McuScript``/``EmulatedDevice`` need from a product's layout.

    Each product builds its own command/telemetry code and supplies an
    object satisfying this via ``Profile.layout``.
    """

    def has_command(self, msgtype: int) -> bool:
        """Whether a command maps to ``msgtype``."""

    def check(self, msgtype: int, plaintext: bytes) -> int:
        """Return the status a command's values earn: accepted, or rejected."""

    def state_changes(self, msgtype: int, plaintext: bytes) -> dict[str, Value]:
        """Return the telemetry values an accepted command sets, by field name."""

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names of a message's typed fields and parts; none if unmapped."""

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages with a typed field or part called ``name``."""

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Return a telemetry payload of the message's typed fields, zero unless set."""

    def update(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> bytes:
        """Return a telemetry payload with named values set."""


#: Type bytes (anker-solix-api ``DeviceHexDataTypes``).
TYPE_STR = 0x00
TYPE_UI = 0x01
TYPE_SILE = 0x02
TYPE_VAR = 0x03
TYPE_BIN = 0x04
TYPE_SFLE = 0x05
TYPE_STRB = 0x06

STATUS_ACCEPTED = 0x00
STATUS_REJECTED = 0x04


class LayoutError(KeyError):
    """A message or field the layout doesn't hold."""


def ui(size: int, *, signed: bool = False) -> Construct[Any, Any]:
    """Return a little-endian integer of ``size`` bytes; unsigned unless told otherwise.

    For a device's own sub-field codecs (a port block, a timer record), not
    for a whole TLV field's outer type byte.
    """
    return BytesInteger(size, signed=signed, swapped=True)


#: A port's status/voltage/current/power, byte-identical on the A91B2 and A2345.
PORT_FIELDS = ("status", "voltage", "current", "power")
PortStatus = Struct(*(name / ui(1 if name == "status" else 2) for name in PORT_FIELDS))


class ScalarField(NamedTuple):
    """One telemetry field: its tag, name, type byte and value codec."""

    tag: str
    name: str
    type_byte: int
    codec: Construct[Any, Any]
    zero: Value


def string(tag: str, name: str, size: int) -> ScalarField:
    """Return a fixed-length string field (type ``00``)."""
    return ScalarField(tag, name, TYPE_STR, Bytes(size), bytes(size))


def unsigned(tag: str, name: str) -> ScalarField:
    """Return a 1-byte unsigned field (type ``01``)."""
    return ScalarField(tag, name, TYPE_UI, ui(1), 0)


def signed_byte(tag: str, name: str) -> ScalarField:
    """Return a 1-byte signed field carried as type ``01``."""
    return ScalarField(tag, name, TYPE_UI, ui(1, signed=True), 0)


def short(tag: str, name: str) -> ScalarField:
    """Return a 2-byte signed field (type ``02``)."""
    return ScalarField(tag, name, TYPE_SILE, ui(2, signed=True), 0)


def word(tag: str, name: str) -> ScalarField:
    """Return a 4-byte unsigned field (type ``03``)."""
    return ScalarField(tag, name, TYPE_VAR, ui(4), 0)
