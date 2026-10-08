# Copyright (c) 2026 Shawn Stricker
"""SOLIX C800X (A1755), from its map and SolixBLE's ``C800``.

Written by ``tools/generate_profiles.py``; nothing about it is recorded.
"""

from __future__ import annotations

from anker_ble_emulator.devices.base import Advert, Profile, register
from anker_ble_emulator.layouts import TYPE_SILE, TYPE_STR, TYPE_VAR, Field
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product


PROFILE = Profile(
    serial="A175500000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=(),
    replies={0x040: (0x840,)},
    pushes=(0x402, 0x405),
    device_version=None,
    known_commands=frozenset({0x040, 0x046, 0x04A, 0x04B, 0x04C, 0x04F, 0x052}),
    layout_aliases={0x402: 0x405, 0x840: 0x405},
    built=(0x402, 0x405, 0x840),
    fields={
        0x405: (
            Field(0xA2, "ac_output_timeout_seconds", TYPE_VAR, 5),
            Field(0xA4, "remaining_time_hours", TYPE_VAR, 5),
            Field(0xA5, "ac_input_power", TYPE_VAR, 5),
            Field(0xA6, "ac_output_power", TYPE_VAR, 5),
            Field(0xA7, "usbc_1_power", TYPE_VAR, 5),
            Field(0xA8, "usbc_2_power", TYPE_VAR, 5),
            Field(0xA9, "usba_1_power", TYPE_VAR, 5),
            Field(0xAA, "usba_2_power", TYPE_VAR, 5),
            Field(0xAF, "photovoltaic_power", TYPE_VAR, 5),
            Field(0xB0, "output_power_total", TYPE_VAR, 5),
            Field(0xB3, "sw_version", TYPE_VAR, 5),
            Field(0xBD, "temperature", TYPE_SILE, 3),
            Field(0xC1, "main_battery_soc", TYPE_VAR, 5),
            Field(0xC3, "battery_soh", TYPE_VAR, 5),
            Field(0xD0, "device_sn", TYPE_STR, 17),
        ),
    },
    map_built=True,
)
register(Product.A1755, PROFILE)
