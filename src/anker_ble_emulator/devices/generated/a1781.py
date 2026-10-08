# Copyright (c) 2026 Shawn Stricker
"""SOLIX F2600 (A1781), from its map and SolixBLE's ``F2600``.

Written by ``tools/generate_profiles.py``; nothing about it is recorded.
"""

from __future__ import annotations

from anker_ble_emulator.devices.base import Advert, Profile, register
from anker_ble_emulator.layouts import TYPE_SILE, TYPE_STR, TYPE_VAR, Field
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product


PROFILE = Profile(
    serial="A178100000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=(),
    replies={0x040: (0x840,)},
    pushes=(0x402, 0x405),
    device_version=None,
    known_commands=frozenset(
        {0x040, 0x042, 0x043, 0x044, 0x046, 0x04A, 0x04B, 0x04C, 0x04E, 0x04F, 0x052}
    ),
    layout_aliases={0x402: 0x405, 0x840: 0x405},
    built=(0x402, 0x405, 0x840),
    fields={
        0x405: (
            Field(0xA2, "ac_output_timeout_seconds", TYPE_VAR, 5),
            Field(0xA3, "dc_output_timeout_seconds", TYPE_VAR, 5),
            Field(0xA4, "remaining_time_hours", TYPE_VAR, 5),
            Field(0xA5, "ac_input_power", TYPE_VAR, 5),
            Field(0xA6, "ac_output_power", TYPE_VAR, 5),
            Field(0xA7, "usbc_1_power", TYPE_VAR, 5),
            Field(0xA8, "usbc_2_power", TYPE_VAR, 5),
            Field(0xA9, "usbc_3_power", TYPE_VAR, 5),
            Field(0xAA, "usba_1_power", TYPE_VAR, 5),
            Field(0xAB, "usba_2_power", TYPE_VAR, 5),
            Field(0xAC, "dc_12v_1_power", TYPE_VAR, 5),
            Field(0xAD, "dc_12v_2_power", TYPE_VAR, 5),
            Field(0xAF, "photovoltaic_power", TYPE_VAR, 5),
            Field(0xB0, "output_power_total", TYPE_VAR, 5),
            Field(0xB3, "sw_version", TYPE_VAR, 5),
            Field(0xB9, "sw_expansion", TYPE_VAR, 5),
            Field(0xBB, "ac_output", TYPE_VAR, 5),
            Field(0xBD, "temperature", TYPE_SILE, 3),
            Field(0xBF, "battery_status", TYPE_VAR, 5),
            Field(0xC1, "main_battery_soc", TYPE_VAR, 5),
            Field(0xC3, "battery_soh", TYPE_VAR, 5),
            Field(0xC5, "expansion_packs", TYPE_VAR, 5),
            Field(0xCF, "light", TYPE_VAR, 5),
            Field(0xD0, "device_sn", TYPE_STR, 17),
            Field(0xDB, "energy_saving_switch", TYPE_VAR, 5),
        ),
    },
    map_built=True,
)
register(Product.A1781, PROFILE)
