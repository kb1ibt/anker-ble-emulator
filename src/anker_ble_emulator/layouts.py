# Copyright (c) 2026 Shawn Stricker
"""Shared types and codecs for a product's hand-written telemetry layout."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from construct import (
    Bytes,
    BytesInteger,
    Construct,
    ExprAdapter,
    FixedSized,
    GreedyBytes,
    Select,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .timed import Timed

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

    def state_changes(
        self, msgtype: int, plaintext: bytes, state: Mapping[str, Value]
    ) -> dict[str, Value]:
        """Return the values an accepted command sets, given the device's state."""

    def reply(
        self, msgtype: int, plaintext: bytes, state: Mapping[str, Value]
    ) -> tuple[int, bytes] | None:
        """Return an accepted request's reply status and fields; None for no reply."""

    def leading(self, msgtype: int) -> frozenset[int]:
        """Return the follow-up pushes a command sends before its reply."""

    def follow_ups(
        self,
        msgtype: int,
        plaintext: bytes,
        before: Mapping[str, Value],
        after: Mapping[str, Value],
    ) -> list[tuple[int, bytes]]:
        """Return the (msgtype, payload) pushes an accepted command's change draws."""

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names of a message's typed fields and parts; none if unmapped."""

    def read(self, msgtype: int, payload: bytes) -> dict[str, Value]:
        """Return the named values a message's payload carries."""

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages with a typed field or part called ``name``."""

    def implied(self, values: Mapping[str, Value]) -> dict[str, Value]:
        """Return the further values that setting ``values`` sets."""

    def started(self) -> tuple[Timed, ...]:
        """Return the timed events the MCU runs from its start (free-running timers)."""

    def timed(
        self, before: Mapping[str, Value], after: Mapping[str, Value]
    ) -> tuple[Timed, ...]:
        """Return the timed events a change from ``before`` to ``after`` starts.

        Both are the device's whole state: the recorded values, set ones over them,
        the RTC under ``mcu.RTC`` and the epoch under ``mcu.EPOCH``; a clock set is a
        change of those alone. ``after`` carries a command's request msgtype under
        ``mcu.COMMAND``, absent for a change made on the unit, and an on-unit
        event's name under ``mcu.UNIT_EVENT`` (``EmulatedDevice.unit_event``).
        """

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Return a telemetry payload of the message's typed fields, zero unless set."""

    def update(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> bytes:
        """Return a telemetry payload with named values set."""

    def publishes(self, msgtype: int, payload_of: Callable[[int], bytes]) -> bool:
        """Whether ``msgtype`` publishes to MQTT now; ``payload_of`` builds others."""

    def sends(self, msgtype: int, values: Mapping[str, Value]) -> bool:
        """Whether the MCU sends its push of ``msgtype`` now, given the set values."""

    def mqtt_routed(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> tuple[tuple[int, bytes], ...]:
        """Return the (msgtype, cleartext) MQTT copies of a push held off BLE."""

    def derive(self, state: Mapping[str, Value]) -> dict[str, Value]:
        """Return the values that are pure functions of ``state``."""

    @property
    def derived(self) -> frozenset[str]:
        """The fields the layout works out from others: a live table draws none."""


#: Type bytes, every code the firmware writes (names from anker-solix-api's
#: ``DeviceHexDataTypes``): string, u8, u16, 4 bytes, a raw block, and a
#: record of length-prefixed sub-fields.
TYPE_STR = 0x00
TYPE_UI = 0x01
TYPE_SILE = 0x02
TYPE_VAR = 0x03
TYPE_BIN = 0x04
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


def at_most(size: int) -> Construct[Any, Any]:
    """Return a codec that parses the first ``size`` bytes, or all of a shorter run."""
    return Select(Bytes(size), GreedyBytes)


def fitted(size: int) -> Construct[Any, Any]:
    """Return ``size`` bytes: a longer value builds cut, a shorter one zero-padded."""
    return ExprAdapter(
        FixedSized(size, GreedyBytes),
        decoder=lambda data, _: data,
        encoder=lambda data, _: at_most(size).parse(data),
    )


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
