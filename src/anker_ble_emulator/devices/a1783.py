# Copyright (c) 2026 Shawn Stricker
"""SOLIX C2000 Gen 2 (A1783)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="APCDKKE0000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(
        local_name="SOLIX C2000 Gen 2",
        version_code=0x02,
        bind_type=0x01,
        product_type=bytes.fromhex("b11a"),
        sku=b"DKKE",
        capability=0x04,
    ),
    data="a1783.json",
    replies={
        0x100: (0x900, 0x421),
        0x057: (0x857,),
        0x103: (0x903, 0x421),
    },
    pushes=(0x421, 0x490, 0x425),
)
register(Product.A1783, PROFILE)


class A1783(EmulatedDevice):
    """SOLIX C2000 Gen 2: encrypted outer, ECDH, owner confirmation by button."""

    def __init__(  # noqa: PLR0913  # the identity plus the three protocol choices
        self,
        serial: str | Unset | None = UNSET,
        mac: str = DEFAULT_MAC,
        transport: Transport | None = None,
        *,
        outer: Outer | None = None,
        path: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Build a C2000 Gen 2; see ``EmulatedDevice``."""
        super().__init__(
            Product.A1783,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            clock=clock,
        )
