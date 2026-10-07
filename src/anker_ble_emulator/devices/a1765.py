# Copyright (c) 2026 Shawn Stricker
"""SOLIX C1000X Gen 2 (A1765)."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from . import a1763
from .base import DEFAULT_MAC, UNSET, EmulatedDevice, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

#: The A1763's build and recordings, under the A1765's name and product type.
#: Its frames and version names report ``A1763``, as the A1785's report ``A1783``.
PROFILE = dataclasses.replace(
    a1763.PROFILE,
    advert=dataclasses.replace(
        a1763.PROFILE.advert,
        local_name="SOLIX C1000X Gen 2",
        product_type=bytes.fromhex("b119"),
    ),
)
register(Product.A1765, PROFILE)


class A1765(EmulatedDevice):
    """SOLIX C1000X Gen 2: the C1000 Gen 2's build under its own advert."""

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
        """Build a C1000X Gen 2; see ``EmulatedDevice``."""
        super().__init__(
            Product.A1765,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
