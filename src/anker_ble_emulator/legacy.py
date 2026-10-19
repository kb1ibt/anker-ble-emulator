# Copyright (c) 2026 Shawn Stricker
"""The F2000Alt legacy protocol: fixed-offset frames, no negotiation or encryption.

Reverse-engineered from an HCI snoop of the official Anker app against a 767
PowerHouse / F2000 (A1780) unit on GATT service ``014bf5da``
(flip-dots/SolixBLEF2000's ``f2000_alt.py``; command and telemetry bytes
cross-checked against SolixBLE PR #64's ``f2000_legacy.py``). Every notify
frame is the *whole* wire frame (header through checksum), so its fields sit
at fixed offsets into it, not into an unwrapped payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from construct import (
    Bytes,
    Checksum,
    Const,
    ConstructError,
    Container,
    If,
    Int8ub,
    Int16ul,
    Padding,
    RawCopy,
    Struct,
    Tell,
    this,
)

from .module import Output


if TYPE_CHECKING:
    #: A parsed client request: ``header``, ``pattern``, ``cmd``, ``payload``.
    LegacyRequest: TypeAlias = Container[Any]

_LOGGER = logging.getLogger(__name__)

#: Header(2) + pattern(3) + cmd(2) + length(2) + checksum(1); the rest is payload.
FRAME_OVERHEAD = 10
#: The trailing checksum byte.
CHECKSUM_LENGTH = 1
#: The client (app) writes this header; the device notifies ``09ff``.
HEADER_WRITE = b"\x08\xee"
HEADER_NOTIFY = b"\x09\xff"
#: ``0101``: poll for a telemetry update; answered with the extended frame, then
#: the base frame (``0101`` -> ``0101`` + ``0149``, by their reply ``cmd``\ s).
CMD_POLL = b"\x01\x01"
#: ``02``: a control command; ``cmd[1]`` selects the field (below).
_CONTROL_CMD = 0x02
#: Field IDs a control command's ``cmd[1]`` selects.
FIELD_AC_OUTPUT = 0x86
FIELD_DC_OUTPUT = 0x87
FIELD_POWER_SAVING_MODE = 0x8A
FIELD_LIGHT_MODE = 0x8B
#: ``cmd[1]`` identifying a StateAck frame (vs. a real telemetry frame's).
STATE_ACK_CMD_BYTE = 0x48


def checksum(data: bytes) -> int:
    """Return the unweighted sum of every byte, mod 256."""
    return sum(data) & 0xFF


#: A client request: header, pattern, cmd, and length-bounded payload, checksummed.
LEGACY_REQUEST = Struct(
    "body"
    / RawCopy(
        Struct(
            "header" / Bytes(2),
            "pattern" / Bytes(3),
            "cmd" / Bytes(2),
            "length" / Int16ul,
            "payload" / Bytes(lambda this: this.length - FRAME_OVERHEAD),
        ),
    ),
    "checksum" / Checksum(Int8ub, checksum, this.body.data),
)

#: A request's cmd: its group (``02`` = control), then the field it selects.
LEGACY_CMD = Struct("group" / Int8ub, "field" / Int8ub)
#: A control command's payload: a reserved byte, then the value to set.
CONTROL_PAYLOAD = Struct("reserved" / Int8ub, "value" / Int8ub)

#: The extended frame's settings block, wire offsets 101-120.
SETTINGS = Struct(
    "bytes_101_116" / Bytes(16),
    "power_saving_mode" / Int8ub,
    "light_mode" / Int8ub,
    "bytes_119_120" / Bytes(2),
)

#: A telemetry notify, base (102 B) or extended (base + ``SETTINGS``), checksummed.
#: Field names carry their wire offsets; the ``bytes_*`` runs are unmapped.
TELEMETRY_FRAME = Struct(
    "body"
    / RawCopy(
        Struct(
            "header" / Bytes(2),
            "pattern" / Bytes(3),
            "cmd" / Bytes(2),
            "length" / Int16ul,
            "bytes_9_62" / Bytes(54),
            "ac_output" / Int8ub,
            "bytes_64_79" / Bytes(16),
            # The two car-socket ports have only ever been observed moving together.
            "dc_output_1" / Int8ub,
            "dc_output_2" / Int8ub,
            "bytes_82_84" / Bytes(3),
            "serial" / Bytes(16),
            "serial_end" / Tell,
            "settings"
            / If(
                lambda this: (
                    this.length - CHECKSUM_LENGTH - this.serial_end >= SETTINGS.sizeof()
                ),
                SETTINGS,
            ),
            "fields_end" / Tell,
            "rest"
            / Bytes(lambda this: this.length - CHECKSUM_LENGTH - this.fields_end),
        ),
    ),
    "checksum" / Checksum(Int8ub, checksum, this.body.data),
)

#: The 14-byte StateAck notification fired on a physical button press.
STATE_ACK = Struct(
    "body"
    / RawCopy(
        Struct(
            "header" / Const(HEADER_NOTIFY),
            Padding(4),
            "cmd" / Const(bytes([STATE_ACK_CMD_BYTE])),
            Padding(2),
            "ac_output" / Int8ub,
            "dc_output" / Int8ub,
            "power_saving_mode" / Int8ub,
            "light_mode" / Int8ub,
        ),
    ),
    "checksum" / Checksum(Int8ub, checksum, this.body.data),
)

#: Control field ID -> the telemetry fields it sets.
_TELEMETRY_FIELDS = {
    FIELD_AC_OUTPUT: ("ac_output",),
    FIELD_DC_OUTPUT: ("dc_output_1", "dc_output_2"),
}
#: Control field ID -> the settings field it sets (extended frame only).
_SETTINGS_FIELDS = {
    FIELD_POWER_SAVING_MODE: "power_saving_mode",
    FIELD_LIGHT_MODE: "light_mode",
}


class LegacyFrameError(ValueError):
    """Bytes that are not a well-formed legacy request."""


def decode_request(data: bytes) -> LegacyRequest:
    """Parse a client write into its header, pattern, cmd, and payload.

    Raises:
        LegacyFrameError: If it's too short, or the length or checksum is wrong.

    """
    try:
        body = LEGACY_REQUEST.parse(data).body.value
    except ConstructError as error:
        msg = f"not a legacy frame: {data.hex()}"
        raise LegacyFrameError(msg) from error
    return Container(
        header=bytes(body.header),
        pattern=bytes(body.pattern),
        cmd=bytes(body.cmd),
        payload=bytes(body.payload),
    )


def _patched(frame: bytes, field_id: int, value: int) -> bytes:
    """Return ``frame`` with ``field_id``'s field(s) set to ``value``, rechecksummed.

    A settings field leaves a base frame, which has no settings block, unchanged.
    """
    fields = TELEMETRY_FRAME.parse(frame).body.value
    if field_id in _SETTINGS_FIELDS:
        if fields.settings is None:
            return frame
        fields.settings[_SETTINGS_FIELDS[field_id]] = value
    else:
        for name in _TELEMETRY_FIELDS[field_id]:
            fields[name] = value
    return TELEMETRY_FRAME.build({"body": {"value": fields}})


@dataclass(frozen=True)
class LegacyProfile:
    """How one legacy-transport product is emulated.

    Attributes:
        local_name: The advertised name.
        telemetry: The baseline ~102-byte base telemetry frame, full wire bytes.
        extended: The baseline ~122-byte extended (base + settings block) frame.
        fragment_cap: The largest frame the link carries; this protocol never
            fragments, so every frame it sends must fit under this.

    """

    local_name: str
    telemetry: bytes
    extended: bytes
    fragment_cap: int = 253

    @property
    def serial_number(self) -> str | None:
        """The serial baked into the baseline telemetry frame; None if it has none."""
        try:
            raw = TELEMETRY_FRAME.parse(self.telemetry).body.value.serial
        except ConstructError:
            return None
        return bytes(raw).decode("ascii", errors="replace").rstrip("\x00") or None


class LegacyModule:
    """The device side of the unencrypted, fixed-offset F2000Alt protocol."""

    def __init__(self, profile: LegacyProfile) -> None:
        """Cache ``profile``'s baseline frames; no link is up yet."""
        self.profile = profile
        self._telemetry = profile.telemetry
        self._extended = profile.extended
        self._connected = False

    @property
    def connected(self) -> bool:
        """Whether a link is up."""
        return self._connected

    @property
    def fragment_cap(self) -> int:
        """The largest frame the link carries."""
        return self.profile.fragment_cap

    def connect(self) -> None:
        """Bring a link up; the cached telemetry carries over from before."""
        self._connected = True

    def disconnect(self) -> None:
        """Drop the link."""
        self._connected = False

    def check_timers(self) -> Output:
        """Do nothing: this protocol has no authorize timer or confirmation window."""
        return Output()

    def write(self, data: bytes) -> Output:
        """Handle one write from the client.

        Args:
            data: The bytes written to the command characteristic.

        """
        try:
            request = decode_request(data)
        except LegacyFrameError:
            _LOGGER.warning("Dropped malformed legacy write %s", data.hex())
            return Output()
        if bytes(request.header) != HEADER_WRITE:
            return Output()
        cmd = bytes(request.cmd)
        if cmd == CMD_POLL:
            return Output([self._extended, self._telemetry])
        command = LEGACY_CMD.parse(cmd)
        if command.group == _CONTROL_CMD:
            return self._control(command.field, bytes(request.payload))
        return Output()

    def _control(self, field_id: int, payload: bytes) -> Output:
        """Apply a control command's value to the cached telemetry; no reply."""
        if field_id not in _TELEMETRY_FIELDS and field_id not in _SETTINGS_FIELDS:
            return Output()
        try:
            value = CONTROL_PAYLOAD.parse(payload).value
        except ConstructError:
            return Output()
        self._telemetry = _patched(self._telemetry, field_id, value)
        self._extended = _patched(self._extended, field_id, value)
        return Output()

    def press_button(self) -> Output:
        """Emit a StateAck of the module's current AC/DC/power-saving/light state."""
        telemetry = TELEMETRY_FRAME.parse(self._telemetry).body.value
        settings = TELEMETRY_FRAME.parse(self._extended).body.value.settings
        frame = STATE_ACK.build(
            {
                "body": {
                    "value": {
                        "ac_output": telemetry.ac_output,
                        "dc_output": telemetry.dc_output_1,
                        "power_saving_mode": settings.power_saving_mode,
                        "light_mode": settings.light_mode,
                    }
                }
            }
        )
        return Output([frame])
