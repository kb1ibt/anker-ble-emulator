# Copyright (c) 2026 Shawn Stricker
"""Emulated devices, one module per product."""

from .a91b2 import A91B2
from .a1722 import A1722
from .a1761 import A1761
from .a1763 import A1763
from .a1765 import A1765
from .a1783 import A1783
from .a1785 import A1785
from .a2345 import A2345
from .a2687 import A2687
from .base import (
    COMPANY_ID,
    DEFAULT_MAC,
    PROFILES,
    SERVICE_UUID,
    UNSET,
    Advert,
    EmulatedDevice,
    Profile,
    Unset,
    register,
)


__all__ = [
    "A91B2",
    "A1722",
    "A1761",
    "A1763",
    "A1765",
    "A1783",
    "A1785",
    "A2345",
    "A2687",
    "COMPANY_ID",
    "DEFAULT_MAC",
    "PROFILES",
    "SERVICE_UUID",
    "UNSET",
    "Advert",
    "EmulatedDevice",
    "Profile",
    "Unset",
    "register",
]
