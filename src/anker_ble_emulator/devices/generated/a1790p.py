# Copyright (c) 2026 Shawn Stricker
"""SOLIX F3800 Plus (A1790P), from its map and SolixBLE's ``F3800``.

Written by ``tools/generate_profiles.py``; nothing about it is recorded.
"""

from __future__ import annotations

from anker_ble_emulator.devices.base import Advert, Profile, register
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product


PROFILE = Profile(
    serial="A1790P0000000001",
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
    map_built=True,
)
register(Product.A1790P, PROFILE)
