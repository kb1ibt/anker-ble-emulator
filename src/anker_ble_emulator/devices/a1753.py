# Copyright (c) 2026 Shawn Stricker
"""SOLIX C800 (A1753)."""

from __future__ import annotations

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from .base import Advert, ProductDevice, Profile, register


PROFILE = Profile(
    serial="A175300000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_3_1,
    # The button confirmation isn't recorded; it's the C1000's.
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    # Telemetry is the C1000's (A1761) recording byte for byte, its serial and
    # pack string ("A1761_30Ah") included: only the C800's MQTT 0405 is
    # recorded, its BLE c402 differs from that, and the C800 runs SolixBLE's
    # C1000 class. Units carry other models' identity strings (an A1785 reports
    # PN A1783, a1783-telemetry.md "a2"), so a borrowed one stays.
    data=("a1753.json",),
    replies={0x040: (0x840,)},
    pushes=(0x402,),
    device_version="v3.5.6",
    # The requests the app sent to an A1753; no setter ack is recorded.
    known_commands=dict.fromkeys(
        {0x040, 0x042, 0x044, 0x045, 0x046, 0x04C, 0x04F, 0x050, 0x057, 0x05E}
        | {0x072, 0x076, 0x077}
    ),
)
register(Product.A1753, PROFILE)


class A1753(ProductDevice):
    """SOLIX C800: plain outer, ECDH, owner confirmation by button."""

    PRODUCT = Product.A1753
