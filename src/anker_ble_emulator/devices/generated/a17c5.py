# Copyright (c) 2026 Shawn Stricker
"""Solarbank 3 E2700 Pro (A17C5), from its map and SolixBLE's ``Solarbank3``.

Written by ``tools/generate_profiles.py``; nothing about it is recorded.
"""

from __future__ import annotations

from anker_ble_emulator.devices.base import Advert, Profile, register
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
    map_built=True,
)
register(Product.A17C5, PROFILE)
