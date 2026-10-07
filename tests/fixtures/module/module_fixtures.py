# Copyright (c) 2026 Shawn Stricker
"""A module wired to a manual clock and a small synthetic MCU script."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from anker_ble_emulator.clock import ManualClock
from anker_ble_emulator.mcu import McuFrame, McuScript
from anker_ble_emulator.module import Module, ModuleConfig


TEST_SERIAL = b"APCDKKE0000000001"
TEST_MAC = bytes.fromhex("aa12deadbeef")
ENROLLED_TOKEN = b"enrolled-token"
#: A status reply long enough to fragment at the 253-byte cap.
LONG_REPLY = b"\x00\xa1\x01\x31" + bytes(range(256)) * 2
ACK_REPLY = bytes.fromhex("00a10131")
PUSH = bytes.fromhex("a10131a20101")

TEST_SCRIPT = McuScript(
    replies={
        0x100: (McuFrame(0x900, LONG_REPLY),),
        0x057: (McuFrame(0x857, ACK_REPLY),),
    },
    pushes={0x421: PUSH},
)


@dataclass(frozen=True)
class ModuleRig:
    """A module and the clock that drives its timers."""

    module: Module
    clock: ManualClock


def build_module(**config: Any) -> ModuleRig:
    """Return a connected A1783-shaped module; ``config`` overrides its fields."""
    clock = ManualClock()
    base = ModuleConfig(mac=TEST_MAC, serial=TEST_SERIAL)
    module = Module(replace(base, **config), TEST_SCRIPT, clock)
    module.enrolled.add(ENROLLED_TOKEN)
    module.connect()
    return ModuleRig(module, clock)
