# Copyright (c) 2026 Shawn Stricker
"""SOLIX F2000 / 767 PowerHouse, 1st generation (A1780): the legacy transport.

The baseline frames are real device telemetry (serial anonymized), captured
by SolixBLE PR #64 (``f2000_legacy.py``); the field offsets and command bytes
are flip-dots/SolixBLEF2000's ``f2000_alt.py``, verified against both an HCI
snoop of the official Anker app and this project's own live-hardware tests.
See ``anker_ble_emulator.legacy`` for the protocol itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.legacy import LegacyProfile
from anker_ble_emulator.products import Product

from .base import DEFAULT_MAC, EmulatedDevice, register_legacy


if TYPE_CHECKING:
    from anker_ble_emulator.products import Transport


#: A ~102-byte base telemetry frame: battery 100%, AC output on, idle, 28 C.
TELEMETRY = bytes.fromhex(
    "09ff0000010149660000000000000000005ab90000ce01000000000000000000000000"
    "000000000000ce0100000000d7006a0074006b00000033030000d7000100021c000000"
    "640064000000000000000000000000303130323033303430353036303730"
    "38a2"
)
#: The ~122-byte extended frame: the same telemetry plus the settings block
#: (600 W AC charging limit, 30 s display timeout, medium brightness, power
#: saving off, light off, Fahrenheit).
EXTENDED = bytes.fromhex(
    "09ff00000101017a0000000000000000005ab90000ce01000000000000000000000000"
    "000000000000ce0100000000d7006a0074006b00000033030000d7000100021c000000"
    "6400640000000000000000000000003031303230333034303530363037303858023c00"
    "1e003c00010001000100023c00000100a0"
)

register_legacy(
    Product.A1780,
    LegacyProfile(local_name="SOLIX F2000", telemetry=TELEMETRY, extended=EXTENDED),
)


class A1780(EmulatedDevice):
    """SOLIX F2000 / 767 PowerHouse, 1st generation: fixed-offset, no encryption."""

    def __init__(
        self, mac: str = DEFAULT_MAC, transport: Transport | None = None
    ) -> None:
        """Build an F2000 1st generation; see ``EmulatedDevice``."""
        super().__init__(Product.A1780, mac=mac, transport=transport)
