# Copyright (c) 2026 Shawn Stricker
"""Emulated Anker Solix BLE devices for testing BLE clients through bleak."""

from .backend import COMMAND_UUID, TELEMETRY_UUID, EmulatedBleakBackend
from .clock import Clock, ManualClock, MonotonicClock
from .devices import A1783, DEFAULT_MAC, UNSET, EmulatedDevice
from .frame import FrameError, decode, encode, make_frame
from .mcu import McuScript, mcu_frame
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
    "FrameError",
    "ManualClock",
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
    "decode",
    "encode",
    "make_frame",
    "mcu_frame",
]
