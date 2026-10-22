# Copyright (c) 2026 Shawn Stricker
"""SOLIX C1000X Gen 2 (A1765)."""

from __future__ import annotations

import dataclasses

from anker_ble_emulator.products import Product

from . import a1763
from .base import ProductDevice, register


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


class A1765(ProductDevice):
    """SOLIX C1000X Gen 2: the C1000 Gen 2's build under its own advert."""

    PRODUCT = Product.A1765
