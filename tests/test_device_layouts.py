# Copyright (c) 2026 Shawn Stricker
"""Devices with their anker-solix-api layouts, and the cloud switch."""

import pytest
from bleak import BleakClient
from bleak.exc import BleakError

from anker_ble_emulator import A1783, A2345, EmulatedBleakBackend
from anker_ble_emulator.layouts import Layout, LayoutError
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: ``a5`` of the A1783's ``0421``: temperature, charge state, then ``battery_soc``.
BATTERY_SOC_OFFSET = 1 + 2


def test_every_device_loads_its_layout() -> None:
    assert A1783().layout is not None
    assert A2345().layout is not None
    assert Layout.load("A0000") is None


def test_a_telemetry_value_set_by_name_reaches_the_recorded_frame() -> None:
    device = A1783()

    device.set_values(0x421, battery_soc=55)

    push = device.module.mcu.push(0x421, values=device.module.values)
    assert decode_fields(push.payload)[0xA5][BATTERY_SOC_OFFSET] == 55
    recorded = device.module.mcu.push(0x421)
    assert decode_fields(recorded.payload)[0xA5][BATTERY_SOC_OFFSET] != 55


def test_a_value_the_layout_doesnt_type_is_refused() -> None:
    device = A1783()

    with pytest.raises(LayoutError):
        device.set_values(0x421, no_such_field=1)


def test_a_device_without_a_layout_takes_no_values() -> None:
    device = A1783()
    device.layout = None

    with pytest.raises(LayoutError, match="no layout"):
        device.set_values(0x421, battery_soc=1)


async def test_a_mapped_command_without_a_recording_gets_a_checked_ack() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        port = [(0xA1, b"\x21"), (0xA2, b"\x01\x00")]
        on = await link.send(0x207, [*port, (0xA3, b"\x01\x01")], SESSION)
        bad = await link.send(0x207, [*port, (0xA3, b"\x01\x05")], SESSION)
        unmapped = await link.send(0x201, [(0xA1, b"\x21")], SESSION)

    assert [cmd_hex(reply.frame) for reply in on] == ["4a07"]
    assert on[0].plaintext == bytes.fromhex("00a10131")
    assert bad[0].plaintext == bytes.fromhex("04a10131")
    assert unmapped == []


async def test_a_value_set_by_name_reaches_the_client() -> None:
    device = A1783()
    device.set_values(0x421, battery_soc=42)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        status = await link.send(0x100, [(0xA1, b"\x21")], SESSION)

    telemetry = next(reply for reply in status if cmd_hex(reply.frame) == "4421")
    assert decode_fields(telemetry.plaintext)[0xA5][BATTERY_SOC_OFFSET] == 42


async def test_going_on_the_cloud_drops_the_link_and_refuses_connects() -> None:
    device = A1783()
    client = BleakClient(device.ble_device, backend=EmulatedBleakBackend)
    await client.connect()

    device.set_cloud(True)
    await settle()

    assert not client.is_connected
    with pytest.raises(BleakError, match="cloud"):
        await client.connect()
    device.set_cloud(False)
    await client.connect()
    assert client.is_connected
    await client.disconnect()
