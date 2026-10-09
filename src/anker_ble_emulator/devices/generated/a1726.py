# Copyright (c) 2026 Shawn Stricker
"""SOLIX C300 DC (A1726), from its map and SolixBLE's ``C300DC``.

Written by ``tools/generate_profiles.py``; nothing about it is recorded.
"""

from __future__ import annotations

from anker_ble_emulator.devices.base import Advert, Profile, register
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product


PROFILE = Profile(
    serial="A172600000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=(),
    replies={},
    pushes=(0x402, 0x405),
    device_version=None,
    known_commands=frozenset({0x046, 0x04B, 0x04C, 0x04F, 0x052}),
    layout_aliases={0x402: 0x405},
    built=(0x402, 0x405),
    map_built=True,
)
register(Product.A1726, PROFILE)
