# Copyright (c) 2026 Shawn Stricker
"""Emulated Anker Solix BLE devices for testing BLE clients through bleak."""

from .backend import COMMAND_UUID, TELEMETRY_UUID, EmulatedBleakBackend
from .clock import Clock, ManualClock, MonotonicClock
from .devices import A1783, DEFAULT_MAC, UNSET, EmulatedDevice
from .frame import Frame, FrameError
from .mcu import McuFrame, McuScript
from .module import AuthMode, Module, ModuleConfig, Output
from .products import PRODUCTS, Outer, Path, Product, ProductInfo, Transport


__all__ = [
    "A1783",
    "COMMAND_UUID",
    "DEFAULT_MAC",
    "PRODUCTS",
    "TELEMETRY_UUID",
    "UNSET",
    "AuthMode",
    "Clock",
    "EmulatedBleakBackend",
    "EmulatedDevice",
    "Frame",
    "FrameError",
    "ManualClock",
    "McuFrame",
    "McuScript",
    "Module",
    "ModuleConfig",
    "MonotonicClock",
    "Outer",
    "Output",
    "Path",
    "Product",
    "ProductInfo",
    "Transport",
]
