# Copyright (c) 2026 Shawn Stricker
"""SOLIX C1000 Gen 2 (A1763)."""

from __future__ import annotations

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from . import solix_c_gen2
from .base import Advert, ProductDevice, Profile, register


PROFILE = Profile(
    serial="AXDDK960000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_3_3_0,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(
        local_name="SOLIX C1000 Gen 2",
        version_code=0x02,
        bind_type=0x01,
        product_type=bytes.fromhex("b118"),
        sku=b"DK96",
        capability=0x04,
    ),
    data=("a1763.json", solix_c_gen2.DATA),
    replies=solix_c_gen2.REPLIES,
    pushes=solix_c_gen2.PUSHES,
    device_version=solix_c_gen2.DEVICE_VERSION,
    version_names=solix_c_gen2.version_names("A1763"),
    known_commands=solix_c_gen2.KNOWN_COMMANDS,
    cloud_pushes=solix_c_gen2.CLOUD_PUSHES,
    mqtt_routes=solix_c_gen2.MQTT_ROUTES,
    rejects=False,
    clock_keeps_blank=True,
    push_490=solix_c_gen2.PUSH_490_FIELDS,
    expansion=False,
    layout=solix_c_gen2.CGen2Layout(),
)
register(Product.A1763, PROFILE)


class A1763(ProductDevice):
    """SOLIX C1000 Gen 2: encrypted outer, ECDH, owner confirmation by button."""

    PRODUCT = Product.A1763
