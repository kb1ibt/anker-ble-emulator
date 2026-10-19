# Copyright (c) 2026 Shawn Stricker
"""Building well-formed F2000Alt legacy requests, and a test module."""

from __future__ import annotations

from construct import Int8ub

from anker_ble_emulator.devices.a1780 import EXTENDED, TELEMETRY
from anker_ble_emulator.legacy import (
    CONTROL_PAYLOAD,
    FRAME_OVERHEAD,
    HEADER_WRITE,
    LEGACY_REQUEST,
    LegacyModule,
    LegacyProfile,
)


#: A pattern field value every recorded request/reply has been observed to use.
PATTERN = b"\x00\x00\x00"
#: ``02``: a control command's first cmd byte.
CONTROL_CMD = 0x02
#: A control command's cmd: ``02``, then the field it sets.
CONTROL_CMD_LAYOUT = Int8ub[2]


def legacy_request(cmd: bytes, payload: bytes = b"") -> bytes:
    """Build a well-formed, checksummed client write."""
    body = {
        "header": HEADER_WRITE,
        "pattern": PATTERN,
        "cmd": cmd,
        "length": FRAME_OVERHEAD + len(payload),
        "payload": payload,
    }
    return LEGACY_REQUEST.build({"body": {"value": body}})


def control_request(field_id: int, value: int) -> bytes:
    """Build a confirmed on/off/mode-style control command."""
    cmd = CONTROL_CMD_LAYOUT.build([CONTROL_CMD, field_id])
    return legacy_request(cmd, CONTROL_PAYLOAD.build({"reserved": 0, "value": value}))


def build_legacy_module() -> LegacyModule:
    """Return an F2000's module with the real captured baseline frames."""
    profile = LegacyProfile(
        local_name="SOLIX F2000", telemetry=TELEMETRY, extended=EXTENDED
    )
    return LegacyModule(profile)
