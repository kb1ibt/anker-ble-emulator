# Copyright (c) 2026 Shawn Stricker
"""Prime Charger 160W (A2687)."""

from __future__ import annotations

from anker_ble_emulator.frame import CHANNEL_APP
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from .base import Advert, ProductDevice, Profile, register


PROFILE = Profile(
    serial="ASHDJW00000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.CHARGING_0_0_5_0,
    auth_mode=AuthMode.CONFIRM,
    # The product type (b405) and the advertised name are known; the rest of
    # the manufacturer record (version_code, bind_type, sku) isn't.
    advert=Advert(local_name="Charging"),
    data=("a2687.json",),
    replies={
        0x200: (0xA00,),
        0x205: (0xA05,),
        0x206: (0xA06,),
        0x207: (0xA07,),
        0x20A: (0xA0A,),
    },
    pushes=(0x300,),
    # No 0830 is recorded, so 0030 goes unanswered.
    device_version=None,
    # The recorded requests, and SolixBLE's USB timer (0x209).
    known_commands=dict.fromkeys({0x200, 0x205, 0x206, 0x207, 0x209, 0x20A}),
    chip=b"Charging",
    lib_version=b"v0.0.5.0",
    serial_tail=11,
    fragment_cap=297,
    mcu_channel=CHANNEL_APP,
)
register(Product.A2687, PROFILE)


class A2687(ProductDevice):
    """Prime Charger 160W: encrypted outer, ECDH, owner confirmation by button."""

    PRODUCT = Product.A2687
