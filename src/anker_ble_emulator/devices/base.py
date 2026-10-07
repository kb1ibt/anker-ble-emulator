# Copyright (c) 2026 Shawn Stricker
"""Emulated devices: identity, profile, the module and the MCU script."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from typing import TYPE_CHECKING

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from construct import Bytes, Int8ub, Optional, Struct

from anker_ble_emulator.clock import MonotonicClock
from anker_ble_emulator.mcu import McuScript, mcu_frame
from anker_ble_emulator.module import (
    TIMER_PERIOD,
    AuthMode,
    Module,
    ModuleConfig,
    Output,
)
from anker_ble_emulator.products import PRODUCTS, Outer, Path, Product, Transport


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from anker_ble_emulator.clock import Clock

#: A locally administered unicast MAC.
DEFAULT_MAC = "AA:12:DE:AD:BE:EF"
#: The manufacturer-data company id Anker advertises under.
COMPANY_ID = 0xFFFF
SERVICE_UUID = "0000ff09-0000-1000-8000-00805f9b34fb"
MAC_LEN = 6


#: The sku's length by ``version_code``.
SKU_LENGTHS = {0x01: 3, 0x02: 4}

#: The manufacturer record: versionCode, MAC, bindType, productType, sku, and
#: the capability byte where the family has one.
ADVERT_LAYOUT = Struct(
    "version_code" / Int8ub,
    "mac" / Bytes(MAC_LEN),
    "bind_type" / Int8ub,
    "product_type" / Bytes(2),
    "sku" / Bytes(lambda this: SKU_LENGTHS[this.version_code]),
    "capability" / Optional(Int8ub),
)


class Unset(Enum):
    """Marks a parameter left to the product's default."""

    UNSET = "unset"


UNSET = Unset.UNSET


@dataclass(frozen=True)
class Advert:
    """The manufacturer record under company id ``0xffff``.

    Attributes:
        local_name: The advertised name, or None while the device has none.
        version_code: ``01`` with a 3-byte sku, ``02`` with a 4-byte one.
        bind_type: The device's binding state.
        product_type: The model key, 2 bytes.
        sku: The serial's sku substring.
        capability: The trailing capability byte; None where the family has none.

    """

    local_name: str | None
    version_code: int
    bind_type: int
    product_type: bytes
    sku: bytes
    capability: int | None = None

    def manufacturer_data(self, mac: bytes) -> bytes:
        """Return the record's bytes for a device with ``mac``."""
        return ADVERT_LAYOUT.build(
            {
                "version_code": self.version_code,
                "mac": mac,
                "bind_type": self.bind_type,
                "product_type": self.product_type,
                "sku": self.sku,
                "capability": self.capability,
            },
        )


@dataclass(frozen=True)
class Profile:
    """How one product is emulated.

    Attributes:
        serial: The default synthetic serial.
        outer: The default outer.
        path: The default path.
        auth_mode: The provisioned policy byte.
        advert: The advertisement.
        data: The recorded-frame resource in ``devices/data/``.
        replies: Reply msgtypes by request msgtype, in send order.
        pushes: Msgtypes the MCU can push.

    """

    serial: str
    outer: Outer
    path: Path
    auth_mode: AuthMode
    advert: Advert
    data: str
    replies: Mapping[int, tuple[int, ...]]
    pushes: tuple[int, ...]

    def script(self) -> McuScript:
        """Return the MCU script built from the packaged recorded frames."""
        text = resources.files(__package__).joinpath("data", self.data).read_text()
        frames = {
            int(key, 16): bytes.fromhex(value)
            for key, value in json.loads(text).items()
        }
        return McuScript(
            replies={
                request: tuple(mcu_frame(reply, frames[reply]) for reply in replies)
                for request, replies in self.replies.items()
            },
            pushes={
                msgtype: mcu_frame(msgtype, frames[msgtype]) for msgtype in self.pushes
            },
        )


#: Emulation profiles by product; each product module registers its own.
PROFILES: dict[Product, Profile] = {}


def register(pn: Product, profile: Profile) -> None:
    """Make ``pn`` emulatable with ``profile``."""
    PROFILES[pn] = profile


def parse_mac(mac: str) -> bytes:
    """Return ``AA:BB:CC:DD:EE:FF`` (or bare hex) as 6 bytes.

    Raises:
        ValueError: If it isn't 6 hex bytes.

    """
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != MAC_LEN:
        msg = f"MAC {mac!r} is {len(raw)} bytes, not {MAC_LEN}"
        raise ValueError(msg)
    return raw


class EmulatedDevice:
    """One emulated device; a backend connects bleak clients to it."""

    def __init__(  # noqa: PLR0913  # the identity plus the three protocol choices
        self,
        pn: Product,
        serial: str | Unset | None = UNSET,
        mac: str = DEFAULT_MAC,
        transport: Transport | None = None,
        *,
        outer: Outer | None = None,
        path: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Build the device from its product's profile.

        Args:
            pn: The product.
            serial: The provisioned serial; None for none; unset for the default.
            mac: The BLE MAC.
            transport: The GATT transport; None for the product's.
            outer: The negotiation outer; None for the profile's.
            path: The key establishment path; None for the profile's.
            clock: Time for the module's timers; the monotonic clock if None.

        Raises:
            NotImplementedError: If the product or a chosen option isn't emulated.

        """
        profile = PROFILES.get(pn)
        if profile is None:
            msg = f"{pn} has no emulation profile yet"
            raise NotImplementedError(msg)
        self.pn = pn
        self.profile = profile
        self.serial = profile.serial if serial is UNSET else serial
        self.mac = parse_mac(mac)
        self.transport = transport or PRODUCTS[pn].transport
        self.outer = outer or profile.outer
        self.path = path or profile.path
        if self.transport != Transport.NEGOTIATED or self.path != Path.ECDH:
            msg = f"{self.transport} transport, {self.path} path: not emulated yet"
            raise NotImplementedError(msg)
        config = ModuleConfig(
            mac=self.mac,
            serial=None if self.serial is None else self.serial.encode(),
            auth_mode=profile.auth_mode,
            enforce=self.outer == Outer.ENCRYPTED,
        )
        self.module = Module(config, profile.script(), clock or MonotonicClock())
        #: Seconds between runs of the module's authorize timer.
        self.timer_period = TIMER_PERIOD
        self._listener: Callable[[Output], None] | None = None

    @property
    def address(self) -> str:
        """The BLE address bleak reports for the device."""
        return ":".join(f"{byte:02X}" for byte in self.mac)

    @property
    def ble_device(self) -> BLEDevice:
        """A ``BLEDevice`` that carries this device in ``details``."""
        return BLEDevice(self.address, self.profile.advert.local_name, self)

    @property
    def advertisement_data(self) -> AdvertisementData:
        """The advertisement a scan would report."""
        advert = self.profile.advert
        return AdvertisementData(
            local_name=advert.local_name,
            manufacturer_data={COMPANY_ID: advert.manufacturer_data(self.mac)},
            service_data={},
            service_uuids=[SERVICE_UUID],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        )

    def listen(self, listener: Callable[[Output], None] | None) -> None:
        """Route the device's own output (button grants, pushes) to ``listener``."""
        self._listener = listener

    def press_button(self) -> None:
        """Press the side button."""
        self._emit(self.module.press_button())

    def push(self, msgtype: int) -> None:
        """Make the MCU push its recorded frame of ``msgtype``."""
        self._emit(self.module.push(msgtype))

    def _emit(self, output: Output) -> None:
        if self._listener is not None:
            self._listener(output)
