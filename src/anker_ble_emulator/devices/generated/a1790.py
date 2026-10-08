# Copyright (c) 2026 Shawn Stricker
"""SOLIX F3800 (A1790), from its map and SolixBLE's ``F3800``.

Written by ``tools/generate_profiles.py``; nothing about it is recorded.
"""

from __future__ import annotations

from anker_ble_emulator.devices.base import Advert, Profile, register
from anker_ble_emulator.layouts import TYPE_SILE, TYPE_STR, TYPE_VAR, Field
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product


PROFILE = Profile(
    serial="A179000000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=(),
    replies={},
    pushes=(0x402, 0x405),
    device_version=None,
    known_commands=frozenset({0x04A, 0x04B}),
    layout_aliases={0x402: 0x405},
    built=(0x402, 0x405),
    fields={
        0x405: (
            Field(0xBA, "sw_expansion", TYPE_VAR, 5),
            Field(0xBE, "temperature", TYPE_SILE, 3),
            Field(0xC0, "main_battery_soc", TYPE_VAR, 5),
            Field(0xC1, "battery_soh", TYPE_VAR, 5),
            Field(0xCC, "device_sn", TYPE_STR, 17),
        ),
    },
    map_built=True,
)
register(Product.A1790, PROFILE)
