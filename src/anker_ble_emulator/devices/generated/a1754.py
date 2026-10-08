# Copyright (c) 2026 Shawn Stricker
"""SOLIX C800 Plus (A1754), from its map and SolixBLE's ``C800``.

Written by ``tools/generate_profiles.py``; nothing about it is recorded.
"""

from __future__ import annotations

from anker_ble_emulator.devices.base import Advert, Profile, register
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product


PROFILE = Profile(
    serial="A175400000000001",
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
    map_built=True,
)
register(Product.A1754, PROFILE)
