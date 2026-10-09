# Copyright (c) 2026 Shawn Stricker
"""Solarbank 3 E2700 Pro (A17C5), from its map and SolixBLE's ``Solarbank3``.

Written by ``tools/generate_profiles.py``; nothing about it is recorded.
"""

from __future__ import annotations

from anker_ble_emulator.devices.base import Advert, Profile, register
from anker_ble_emulator.layouts import TYPE_SILE, TYPE_STR, TYPE_VAR, Field
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product


PROFILE = Profile(
    serial="A17C500000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=(),
    replies={},
    pushes=(0x402, 0x405),
    device_version=None,
    known_commands=frozenset(),
    layout_aliases={0x402: 0x405},
    built=(0x402, 0x405),
    fields={
        0x405: (
            Field(0xA2, "device_sn", TYPE_STR, 17),
            Field(0xA5, "temperature", TYPE_VAR, 5),
            Field(0xA6, "battery_soc", TYPE_VAR, 5),
            Field(0xA7, "sw_version", TYPE_VAR, 5),
            Field(0xAB, "photovoltaic_power", TYPE_VAR, 5),
            Field(0xAC, "battery_power_signed", TYPE_VAR, 5),
            Field(0xB1, "charged_energy", TYPE_VAR, 5),
            Field(0xB2, "discharged_energy", TYPE_VAR, 5),
            Field(0xB7, "active_charge_soc", TYPE_VAR, 5),
            Field(0xB8, "usage_mode", TYPE_VAR, 5),
            Field(0xBD, "max_load", TYPE_SILE, 3),
            Field(0xBE, "max_load_legal", TYPE_VAR, 5),
            Field(0xBF, "backup_start_timestamp", TYPE_VAR, 5),
            Field(0xC7, "pv_2_power", TYPE_VAR, 5),
            Field(0xC8, "pv_3_power", TYPE_VAR, 5),
            Field(0xC9, "pv_4_power", TYPE_VAR, 5),
            Field(0xD5, "pv_limit", TYPE_VAR, 5),
        ),
    },
    map_built=True,
)
register(Product.A17C5, PROFILE)
