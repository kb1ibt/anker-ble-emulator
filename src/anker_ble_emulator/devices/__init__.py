# Copyright (c) 2026 Shawn Stricker
"""Emulated devices, one module per product."""

from .a1783 import A1783
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
    "A1783",
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
