# Copyright (c) 2026 Shawn Stricker
"""Prime Power Bank 20K (A110B)."""

from __future__ import annotations

from anker_ble_emulator.frame import CHANNEL_APP
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from .base import Advert, ProductDevice, Profile, register


PROFILE = Profile(
    serial="AJ7DL500000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.CHARGING_0_0_5_1,
    auth_mode=AuthMode.CONFIRM,
    # Only the product type (b406) is known, not the rest of the record.
    advert=Advert(local_name=None),
    data=("a110b.json",),
    replies={0x200: (0xA00,)},
    pushes=(0x300,),
    # No 0830 is recorded, so 0030 goes unanswered.
    device_version=None,
    # The recorded request; SolixBLE's 0x20A got no recorded answer.
    known_commands=dict.fromkeys({0x200}),
    chip=b"Charging",
    lib_version=b"v0.0.5.1",
    serial_tail=11,
    fragment_cap=297,
    mcu_channel=CHANNEL_APP,
)
register(Product.A110B, PROFILE)


class A110B(ProductDevice):
    """Prime Power Bank 20K: encrypted outer, ECDH, owner confirmation by button."""

    PRODUCT = Product.A110B
