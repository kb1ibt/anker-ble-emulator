# Copyright (c) 2026 Shawn Stricker
"""SOLIX C300 (A1722)."""

from __future__ import annotations

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from .base import Advert, ProductDevice, Profile, register


PROFILE = Profile(
    serial="AZVSBJ0000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_9_8,
    auth_mode=AuthMode.CONFIRM,
    # No advertisement record is recorded for the A1722.
    advert=Advert(local_name=None),
    data=("a1722.json",),
    replies={0x040: (0x840,)},
    pushes=(0x402,),
    device_version="v1.0.5.7",
    version_names=("A1722_high", "A1722_mcu_high", "A1722_esp32_high"),
    # The requests anker-solix-api#348 recorded, and the map's 0x057.
    known_commands=dict.fromkeys(
        {0x040, 0x042, 0x043, 0x044, 0x045, 0x046, 0x04A, 0x04B, 0x04C, 0x04F}
        | {0x050, 0x052, 0x057, 0x076, 0x077, 0x079}
    ),
)
register(Product.A1722, PROFILE)


class A1722(ProductDevice):
    """SOLIX C300: plain outer, ECDH, owner confirmation by button."""

    PRODUCT = Product.A1722
