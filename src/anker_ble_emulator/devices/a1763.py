# Copyright (c) 2026 Shawn Stricker
"""SOLIX C1000 Gen 2 (A1763)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from . import solix_c_gen2
from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

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
    rejects=False,
    summary=solix_c_gen2.SUMMARY,
    expansion=False,
)
register(Product.A1763, PROFILE)


class A1763(EmulatedDevice):
    """SOLIX C1000 Gen 2: encrypted outer, ECDH, owner confirmation by button."""

    def __init__(  # noqa: PLR0913  # the identity plus the four protocol choices
        self,
        serial: str | Unset | None = UNSET,
        mac: str = DEFAULT_MAC,
        transport: Transport | None = None,
        *,
        outer: Outer | None = None,
        path: Path | None = None,
        module: ModuleBuild | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Build a C1000 Gen 2; see ``EmulatedDevice``."""
        super().__init__(
            Product.A1763,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
