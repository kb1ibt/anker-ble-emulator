# Copyright (c) 2026 Shawn Stricker
"""Emulated Anker Solix BLE devices for testing BLE clients through bleak."""

from .backend import (
    COMMAND_UUID,
    GATT_SERVICE_UUID,
    TELEMETRY_UUID,
    EmulatedBleakBackend,
)
from .clock import Clock, ManualClock, MonotonicClock
from .devices import (
    A17C1,
    A91B2,
    A1722,
    A1761,
    A1763,
    A1765,
    A1783,
    A1785,
    A2345,
    A2687,
    AS220,
    DEFAULT_MAC,
    UNSET,
    EmulatedDevice,
)
from .frame import FrameError, decode, encode, make_frame
from .mcu import McuScript, mcu_frame
from .module import AuthMode, Module, ModuleConfig, Output, Versions
from .products import (
    PRODUCTS,
    ModuleBuild,
    Outer,
    Path,
    Product,
    ProductInfo,
    Transport,
)


__all__ = [
    "A17C1",
    "A91B2",
    "A1722",
    "A1761",
    "A1763",
    "A1765",
    "A1783",
    "A1785",
    "A2345",
    "A2687",
    "AS220",
    "COMMAND_UUID",
    "DEFAULT_MAC",
    "GATT_SERVICE_UUID",
    "PRODUCTS",
    "TELEMETRY_UUID",
    "UNSET",
    "AuthMode",
    "Clock",
    "EmulatedBleakBackend",
    "EmulatedDevice",
    "FrameError",
    "ManualClock",
    "McuScript",
    "Module",
    "ModuleBuild",
    "ModuleConfig",
    "MonotonicClock",
    "Outer",
    "Output",
    "Path",
    "Product",
    "ProductInfo",
    "Transport",
    "Versions",
    "decode",
    "encode",
    "make_frame",
    "mcu_frame",
]
