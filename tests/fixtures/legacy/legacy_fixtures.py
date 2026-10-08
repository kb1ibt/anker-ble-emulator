# Copyright (c) 2026 Shawn Stricker
"""Building well-formed F2000Alt legacy requests, and a test module."""

from __future__ import annotations

from anker_ble_emulator.devices.a1780 import EXTENDED, TELEMETRY
from anker_ble_emulator.legacy import (
    FRAME_OVERHEAD,
    HEADER_WRITE,
    LegacyModule,
    LegacyProfile,
    checksum,
)


#: A pattern field value every recorded request/reply has been observed to use.
PATTERN = b"\x00\x00\x00"
#: ``02``: a control command's first cmd byte.
CONTROL_CMD = 0x02


def legacy_request(cmd: bytes, payload: bytes = b"") -> bytes:
    """Build a well-formed, checksummed client write."""
    length = (FRAME_OVERHEAD + len(payload)).to_bytes(2, "little")
    body = HEADER_WRITE + PATTERN + cmd + length + payload
    return body + bytes([checksum(body)])


def control_request(field_id: int, value: int) -> bytes:
    """Build a confirmed on/off/mode-style control command."""
    return legacy_request(bytes([CONTROL_CMD, field_id]), bytes([0x00, value]))


def build_legacy_module() -> LegacyModule:
    """Return an F2000's module with the real captured baseline frames."""
    profile = LegacyProfile(
        local_name="SOLIX F2000", telemetry=TELEMETRY, extended=EXTENDED
    )
    return LegacyModule(profile)
