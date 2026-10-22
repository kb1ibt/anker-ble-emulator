# Copyright (c) 2026 Shawn Stricker
"""SOLIX C1000 (A1761), the first generation."""

from __future__ import annotations

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from .base import Advert, ProductDevice, Profile, register


PROFILE = Profile(
    serial="APC9FE0000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_3_1,
    auth_mode=AuthMode.CONFIRM,
    # No advertisement record is recorded for the A1761.
    advert=Advert(local_name=None),
    data=("a1761.json",),
    replies={0x040: (0x840,), 0x04A: (0x84A,)},
    pushes=(0x402,),
    device_version="v1.5.9",
    known_commands=dict.fromkeys(
        {0x040, 0x042, 0x043, 0x044, 0x045, 0x046, 0x04A, 0x04B, 0x04C, 0x04F}
        | {0x050, 0x052, 0x057, 0x05E, 0x076, 0x077}
    ),
)
register(Product.A1761, PROFILE)


class A1761(ProductDevice):
    """SOLIX C1000: plain outer, ECDH, owner confirmation by button."""

    PRODUCT = Product.A1761
