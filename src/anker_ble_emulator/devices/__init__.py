# Copyright (c) 2026 Shawn Stricker
"""Emulated devices, one module per product."""

from .a91b2 import A91B2
from .a1783 import A1783
from .a2345 import A2345
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
    "A1783",
    "A2345",
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
