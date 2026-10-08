# Copyright (c) 2026 Shawn Stricker
"""The F2000Alt legacy protocol: fixed-offset frames, no negotiation or encryption.

Reverse-engineered from an HCI snoop of the official Anker app against a 767
PowerHouse / F2000 (A1780) unit on GATT service ``014bf5da``
(flip-dots/SolixBLEF2000's ``f2000_alt.py``; command and telemetry bytes
cross-checked against SolixBLE PR #64's ``f2000_legacy.py``). Every notify
frame is the *whole* wire frame (header through checksum): fields are read
and patched at absolute offsets into it, not into an unwrapped payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from construct import (
    Bytes,
    Checksum,
    ConstructError,
    Container,
    Int8ub,
    Int16ul,
    RawCopy,
    Struct,
    this,
)

from .module import Output


if TYPE_CHECKING:
    #: A parsed client request: ``header``, ``pattern``, ``cmd``, ``payload``.
    LegacyRequest: TypeAlias = Container[Any]

_LOGGER = logging.getLogger(__name__)

#: Header(2) + pattern(3) + cmd(2) + length(2) + checksum(1); the rest is payload.
FRAME_OVERHEAD = 10
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

#: Byte offsets into the full wire frame (prefix included) a control command patches.
OFFSET_AC_OUTPUT = 63
#: The two physical car-socket ports have only ever been observed moving together.
OFFSET_DC_OUTPUT = (80, 81)
#: Settings-block offsets; only present in the ~122-byte extended frame.
OFFSET_POWER_SAVING_MODE = 117
OFFSET_LIGHT_MODE = 118

#: The ~14-byte StateAck notification fired on a physical button press.
STATE_ACK_LENGTH = 14
#: ``cmd[1]`` identifying a StateAck frame (vs. a real telemetry frame's).
STATE_ACK_CMD_BYTE = 0x48
STATE_ACK_AC = 9
STATE_ACK_DC = 10
STATE_ACK_POWER_SAVING = 11
STATE_ACK_LIGHT = 12
#: The serial's byte range in the full wire frame.
OFFSET_SERIAL = slice(85, 101)


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


def _patched_checksum(frame: bytearray) -> None:
    """Recompute ``frame``'s trailing checksum byte in place."""
    frame[-1] = checksum(bytes(frame[:-1]))


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
        """The serial baked into the baseline telemetry frame; None if too short."""
        if len(self.telemetry) < OFFSET_SERIAL.stop:
            return None
        raw = bytes(self.telemetry[OFFSET_SERIAL])
        return raw.decode("ascii", errors="replace").rstrip("\x00") or None


class LegacyModule:
    """The device side of the unencrypted, fixed-offset F2000Alt protocol."""

    def __init__(self, profile: LegacyProfile) -> None:
        """Cache ``profile``'s baseline frames; no link is up yet."""
        self.profile = profile
        self._telemetry = bytearray(profile.telemetry)
        self._extended = bytearray(profile.extended)
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
            return Output([bytes(self._extended), bytes(self._telemetry)])
        if cmd[0] == _CONTROL_CMD:
            return self._control(cmd[1], bytes(request.payload))
        return Output()

    def _control(self, field_id: int, payload: bytes) -> Output:
        """Apply a control command's value to the cached telemetry; no reply."""
        if len(payload) < 2:  # noqa: PLR2004  # reserved byte + value
            return Output()
        value = payload[1]
        if field_id == FIELD_AC_OUTPUT:
            self._patch(OFFSET_AC_OUTPUT, value)
        elif field_id == FIELD_DC_OUTPUT:
            self._patch(OFFSET_DC_OUTPUT[0], value)
            self._patch(OFFSET_DC_OUTPUT[1], value)
        elif field_id == FIELD_POWER_SAVING_MODE:
            self._patch(OFFSET_POWER_SAVING_MODE, value)
        elif field_id == FIELD_LIGHT_MODE:
            self._patch(OFFSET_LIGHT_MODE, value)
        return Output()

    def _patch(self, offset: int, value: int) -> None:
        """Set ``offset`` to ``value`` in every cached frame long enough to carry it.

        Every confirmed field's offset fits the extended frame; only the
        settings-block ones (past the shorter base frame) need the guard.
        """
        if offset < len(self._telemetry):
            self._telemetry[offset] = value
            _patched_checksum(self._telemetry)
        self._extended[offset] = value
        _patched_checksum(self._extended)

    def press_button(self) -> Output:
        """Emit a StateAck of the module's current AC/DC/power-saving/light state."""
        frame = bytearray(STATE_ACK_LENGTH)
        frame[0:2] = HEADER_NOTIFY
        frame[6] = STATE_ACK_CMD_BYTE
        frame[STATE_ACK_AC] = self._telemetry[OFFSET_AC_OUTPUT]
        frame[STATE_ACK_DC] = self._telemetry[OFFSET_DC_OUTPUT[0]]
        frame[STATE_ACK_POWER_SAVING] = self._extended[OFFSET_POWER_SAVING_MODE]
        frame[STATE_ACK_LIGHT] = self._extended[OFFSET_LIGHT_MODE]
        _patched_checksum(frame)
        return Output([bytes(frame)])
