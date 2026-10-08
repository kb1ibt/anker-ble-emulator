# Copyright (c) 2026 Shawn Stricker
"""SOLIX F2000 1st generation (A1780): the legacy transport, end to end."""

from typing import Any

import pytest
from bleak import BleakClient

from anker_ble_emulator import (
    A1780,
    EmulatedBleakBackend,
    ModuleBuild,
    Outer,
    Path,
    Product,
)
from anker_ble_emulator.devices import COMPANY_ID, EmulatedDevice
from anker_ble_emulator.devices.a1780 import EXTENDED, TELEMETRY
from anker_ble_emulator.legacy import CMD_POLL, FIELD_AC_OUTPUT, OFFSET_AC_OUTPUT
from tests.fixtures.client import settle
from tests.fixtures.legacy import control_request, legacy_request


async def test_connect_exposes_the_f2000_gatt_layout() -> None:
    device = A1780()
    client = BleakClient(device.ble_device, backend=EmulatedBleakBackend)

    await client.connect()

    assert device.gatt.service == "014bf5da-0000-1000-8000-00805f9b34fb"
    command = client.services.get_characteristic(device.gatt.command)
    telemetry = client.services.get_characteristic(device.gatt.telemetry)
    assert command is not None
    assert telemetry is not None
    assert command.properties == ["write-without-response", "write"]
    assert telemetry.properties == ["notify"]
    assert command.service_uuid == telemetry.service_uuid == device.gatt.service
    await client.disconnect()
    assert not device.module.connected


async def test_poll_returns_the_extended_then_the_base_frame() -> None:
    device = A1780()
    received: list[bytes] = []
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        await client.start_notify(
            device.gatt.telemetry, lambda _s, data: received.append(bytes(data))
        )
        await client.write_gatt_char(
            device.gatt.command, legacy_request(CMD_POLL), response=False
        )
        await settle()

    assert received == [EXTENDED, TELEMETRY]


async def test_control_command_is_silent_but_updates_the_next_poll() -> None:
    device = A1780()
    received: list[bytes] = []
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        await client.start_notify(
            device.gatt.telemetry, lambda _s, data: received.append(bytes(data))
        )

        await client.write_gatt_char(
            device.gatt.command, control_request(FIELD_AC_OUTPUT, 0), response=False
        )
        await settle()
        assert received == []

        await client.write_gatt_char(
            device.gatt.command, legacy_request(CMD_POLL), response=False
        )
        await settle()

    assert received[0][OFFSET_AC_OUTPUT] == 0


def test_advertisement_shows_the_f2000_name_and_reversed_mac() -> None:
    device = A1780(mac="E8:EE:CC:3F:A6:1F")

    advert = device.advertisement_data
    assert device.local_name == "SOLIX F2000"
    assert advert.service_uuids == ["00001780-0000-1000-8000-00805f9b34fb"]
    assert advert.manufacturer_data[COMPANY_ID] == bytes.fromhex("1fa63fcceee8")


def test_serial_is_read_from_the_captured_telemetry() -> None:
    assert A1780().serial == "0102030405060708"


def test_negotiated_module_raises_for_a_legacy_device() -> None:
    device = A1780()

    with pytest.raises(NotImplementedError, match="no MCU script"):
        _ = device.negotiated_module


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"outer": Outer.PLAIN}, id="outer"),
        pytest.param({"path": Path.ECDH}, id="path"),
        pytest.param({"module": ModuleBuild.V0_3_0_6}, id="module"),
        pytest.param({"serial": "whatever"}, id="serial"),
    ],
)
def test_negotiated_only_options_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        EmulatedDevice(Product.A1780, **kwargs)
