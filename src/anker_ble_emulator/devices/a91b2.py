# Copyright (c) 2026 Shawn Stricker
"""Prime Charging Station 240W (A91B2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="AFYJTB0000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    auth_mode=AuthMode.OPEN,
    advert=Advert(
        local_name=None,
        version_code=0x01,
        bind_type=0x00,
        product_type=bytes.fromhex("b401"),
        sku=b"JTB",
        capability=0x00,
    ),
    data="a91b2.json",
    replies={
        0x200: (0xA00,),
        0x20A: (0xA0A,),
        0x20B: (0xA0B, 0x303),
    },
    pushes=(0x303,),
    enforce=False,
)
register(Product.A91B2, PROFILE)


class A91B2(EmulatedDevice):
    """Prime Charging Station 240W: plain outer, ECDH, CBC session, auth mode 0.

    It authorizes at the key exchange; ``0027`` answers and then drops the link.
    """

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
        """Build a Prime Charging Station 240W; see ``EmulatedDevice``."""
        super().__init__(
            Product.A91B2,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            clock=clock,
        )
