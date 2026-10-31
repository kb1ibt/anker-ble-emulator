# Copyright (c) 2026 Shawn Stricker
"""SOLIX C2000 Gen 2 (A1783)."""

from __future__ import annotations

from anker_ble_emulator.live import LiveSource
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from . import solix_c_gen2
from .base import Advert, ProductDevice, Profile, register


PROFILE = Profile(
    serial="APCDKKE0000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_3_3_0,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(
        local_name="SOLIX C2000 Gen 2",
        version_code=0x02,
        bind_type=0x01,
        product_type=bytes.fromhex("b11a"),
        sku=b"DKKE",
        capability=0x04,
    ),
    data=("a1783.json", solix_c_gen2.DATA),
    replies=solix_c_gen2.REPLIES,
    pushes=(*solix_c_gen2.PUSHES, 0x425),
    device_version=solix_c_gen2.DEVICE_VERSION,
    version_names=solix_c_gen2.version_names("A1783"),
    known_commands=solix_c_gen2.KNOWN_COMMANDS,
    cloud_pushes=solix_c_gen2.CLOUD_PUSHES,
    mqtt_routes=solix_c_gen2.MQTT_ROUTES,
    rejects=False,
    clock_keeps_blank=True,
    push_490=solix_c_gen2.PUSH_490_FIELDS,
    layout=solix_c_gen2.CGen2ExpansionLayout(),
    live=LiveSource("a1783_live.json", solix_c_gen2.STREAM_PERIOD),
    module_replies={
        ModuleBuild.V0_3_3_0: (
            0x820,
            0x822,
            0x827,
            0x828,
            0x82E,
            0x82F,
            0x835,
            0x836,
            0x838,
        ),
    },
)
register(Product.A1783, PROFILE)


class A1783(ProductDevice):
    """SOLIX C2000 Gen 2: encrypted outer, ECDH, owner confirmation by button."""

    PRODUCT = Product.A1783
