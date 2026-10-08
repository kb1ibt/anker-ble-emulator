# Copyright (c) 2026 Shawn Stricker
"""A bleak client backend connected to an emulated device."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.client import BaseBleakClient
from bleak.backends.service import BleakGATTService, BleakGATTServiceCollection
from bleak.exc import BleakError

from .devices import EmulatedDevice


if TYPE_CHECKING:
    from bleak.args import SizedBuffer
    from bleak.assigned_numbers import CharacteristicPropertyName
    from bleak.backends.client import NotifyCallback
    from bleak.backends.descriptor import BleakGATTDescriptor
    from bleak.backends.device import BLEDevice

    from .module import Output

#: The GATT service holding both characteristics; ``ff09`` is only advertised.
GATT_SERVICE_UUID = "8c850001-0302-41c5-b46e-cf057c562025"
COMMAND_UUID = "8c850002-0302-41c5-b46e-cf057c562025"
TELEMETRY_UUID = "8c850003-0302-41c5-b46e-cf057c562025"
SERVICE_HANDLE = 15
TELEMETRY_HANDLE = 17
COMMAND_HANDLE = 20
#: The ATT MTU the bench devices grant.
ATT_MTU = 256


def _services(payload_cap: int) -> BleakGATTServiceCollection:
    services = BleakGATTServiceCollection()
    service = BleakGATTService(None, SERVICE_HANDLE, GATT_SERVICE_UUID)
    services.add_service(service)
    characteristics: tuple[tuple[int, str, list[CharacteristicPropertyName]], ...] = (
        (TELEMETRY_HANDLE, TELEMETRY_UUID, ["notify"]),
        (COMMAND_HANDLE, COMMAND_UUID, ["write-without-response", "write"]),
    )
    for handle, uuid, properties in characteristics:
        services.add_characteristic(
            BleakGATTCharacteristic(
                None, handle, uuid, properties, lambda: payload_cap, service
            )
        )
    return services


class EmulatedBleakBackend(BaseBleakClient):
    """Pass as ``BleakClient(device.ble_device, backend=EmulatedBleakBackend)``."""

    def __init__(self, address_or_ble_device: BLEDevice | str, **kwargs: Any) -> None:
        """Bind to the emulated device carried in the ``BLEDevice``'s details.

        Raises:
            BleakError: If the ``BLEDevice`` doesn't carry an emulated device.

        """
        super().__init__(address_or_ble_device, **kwargs)
        details = getattr(address_or_ble_device, "details", None)
        if not isinstance(details, EmulatedDevice):
            msg = "EmulatedBleakBackend needs the BLEDevice of an EmulatedDevice"
            raise BleakError(msg)
        self.device = details
        self._connected = False
        self._notify: NotifyCallback | None = None
        self._timer: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        """The advertised local name, else the address with dashes, as bleak's."""
        return self.device.local_name or self.address.replace(":", "-")

    @property
    def mtu_size(self) -> int:
        """The ATT MTU."""
        return ATT_MTU

    @property
    def is_connected(self) -> bool:
        """Whether the emulated link is up."""
        return self._connected

    async def connect(self, pair: bool, **kwargs: Any) -> None:  # bleak's signature
        """Bring the link up and start the device's authorize timer.

        Raises:
            BleakError: If the device is on the cloud, or another client holds it.

        """
        if self.device.cloud:
            msg = f"{self.address} is on WiFi to the cloud, not on BLE"
            raise BleakError(msg)
        if self.device.module.connected:
            msg = f"{self.address} is already connected"
            raise BleakError(msg)
        self.device.module.connect()
        self.device.listen(self._deliver)
        self.services = _services(self.device.module.config.fragment_cap)
        self._connected = True
        self._timer = asyncio.get_running_loop().create_task(self._run_timer())

    async def disconnect(self) -> None:
        """Drop the link from the client side."""
        self._teardown()

    async def pair(self, *args: Any, **kwargs: Any) -> None:
        """Do nothing: Anker devices don't bond."""

    async def unpair(self) -> None:
        """Do nothing: Anker devices don't bond."""

    async def read_gatt_char(
        self,
        characteristic: BleakGATTCharacteristic,
        *,
        use_cached: bool = False,
        **kwargs: Any,
    ) -> bytearray:
        """Refuse: the device's characteristics aren't readable.

        Raises:
            BleakError: Always.

        """
        msg = f"{characteristic.uuid} is not readable"
        raise BleakError(msg)

    async def read_gatt_descriptor(
        self,
        descriptor: BleakGATTDescriptor,
        *,
        use_cached: bool = False,
        **kwargs: Any,
    ) -> bytearray:
        """Refuse: the emulated device has no readable descriptors.

        Raises:
            BleakError: Always.

        """
        msg = f"descriptor {descriptor.handle} is not readable"
        raise BleakError(msg)

    async def write_gatt_char(
        self,
        characteristic: BleakGATTCharacteristic,
        data: SizedBuffer,
        response: bool,  # bleak's signature
    ) -> None:
        """Hand a write on the command characteristic to the module.

        Raises:
            BleakError: If not connected, or the characteristic isn't writable.

        """
        if not self._connected:
            msg = "Not connected"
            raise BleakError(msg)
        if characteristic.uuid != COMMAND_UUID:
            msg = f"{characteristic.uuid} is not writable"
            raise BleakError(msg)
        self._deliver(self.device.module.write(bytes(data)))

    async def write_gatt_descriptor(
        self, descriptor: BleakGATTDescriptor, data: SizedBuffer
    ) -> None:
        """Refuse: the emulated device has no writable descriptors.

        Raises:
            BleakError: Always.

        """
        msg = f"descriptor {descriptor.handle} is not writable"
        raise BleakError(msg)

    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback: NotifyCallback,
        **kwargs: Any,
    ) -> None:
        """Send the device's notifications to ``callback``.

        Raises:
            BleakError: If the characteristic doesn't notify.

        """
        if characteristic.uuid != TELEMETRY_UUID:
            msg = f"{characteristic.uuid} does not notify"
            raise BleakError(msg)
        self._notify = callback

    async def stop_notify(self, characteristic: BleakGATTCharacteristic) -> None:
        """Stop sending notifications."""
        self._notify = None

    def _deliver(self, output: Output) -> None:
        """Schedule the module's frames as notifications, in order, then any drop."""
        loop = asyncio.get_running_loop()
        for frame in output.frames:
            if self._notify is not None:
                loop.call_soon(self._notify, bytearray(frame))
        if output.disconnect:
            loop.call_soon(self._drop)

    def _drop(self) -> None:
        """Drop the link from the device side and tell bleak."""
        if not self._connected:
            return
        self._teardown()
        if self._disconnected_callback is not None:
            self._disconnected_callback()

    def _teardown(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._connected = False
        self._notify = None
        self.device.listen(None)
        self.device.module.disconnect()

    async def _run_timer(self) -> None:
        """Run the module's authorize timer every ``timer_period`` seconds."""
        while True:
            await asyncio.sleep(self.device.timer_period)
            self._deliver(self.device.module.check_timers())
