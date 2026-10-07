# Copyright (c) 2026 Shawn Stricker
"""The bleak backend, driven through a real ``bleak.BleakClient``."""

import asyncio
from dataclasses import replace

import pytest
from bleak import BleakClient
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from anker_ble_emulator import (
    A1783,
    COMMAND_UUID,
    TELEMETRY_UUID,
    EmulatedBleakBackend,
    ManualClock,
)
from tests.fixtures.client import SESSION, AppClient, BleakLink, settle


TOKEN = b"owner-token"


async def test_connect_exposes_the_anker_service() -> None:
    device = A1783()
    client = BleakClient(device.ble_device, backend=EmulatedBleakBackend)

    await client.connect()

    assert client.is_connected
    assert client.mtu_size == 256
    assert client.name == "SOLIX C2000 Gen 2"
    command = client.services.get_characteristic(COMMAND_UUID)
    telemetry = client.services.get_characteristic(TELEMETRY_UUID)
    assert command is not None
    assert telemetry is not None
    assert command.properties == ["write-without-response", "write"]
    assert telemetry.properties == ["notify"]
    await client.disconnect()
    assert not client.is_connected
    assert not device.module.connected


async def test_unnamed_device_reports_its_address() -> None:
    device = A1783()
    device.profile = replace(
        device.profile, advert=replace(device.profile.advert, local_name=None)
    )

    backend = EmulatedBleakBackend(device.ble_device, timeout=10)

    assert backend.name == "AA-12-DE-AD-BE-EF"


async def test_new_owner_is_granted_by_the_button_then_streams() -> None:
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()

        replies = await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        assert replies[0x827].status == 0x09
        assert link.replies[-1].frame.pattern.hex() == "030101"
        assert link.replies[-1].status == 0
        assert device.module.authorized


async def test_status_request_draws_the_recorded_status_and_telemetry() -> None:
    device = A1783()
    device.module.enrolled.add(TOKEN)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)

        replies = await link.send(0x100, [(0xA1, b"\x21")], SESSION)

    status, telemetry = replies
    script = device.module.mcu
    assert status.frame.cmd == 0x4900
    assert status.plaintext == script.respond(0x100)[0].cleartext
    assert telemetry.frame.cmd == 0x4421
    assert telemetry.plaintext == script.respond(0x100)[1].cleartext
    assert len(link.raw[-4:]) == 4
    assert all(len(data) <= 253 for data in link.raw)


@pytest.mark.parametrize(
    ("request_type", "reply_types"),
    [
        pytest.param(0x057, [0x4857], id="realtime"),
        pytest.param(0x103, [0x4903, 0x4421], id="system setter"),
    ],
)
async def test_recorded_acks(request_type: int, reply_types: list[int]) -> None:
    device = A1783()
    device.module.enrolled.add(TOKEN)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)

        replies = await link.send(request_type, [(0xA1, b"\x21")], SESSION)

    assert [reply.frame.cmd for reply in replies] == reply_types
    assert replies[0].plaintext.hex() == "00a10131"


async def test_device_push_reaches_the_client() -> None:
    device = A1783()
    device.module.enrolled.add(TOKEN)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)

        device.push(0x490)
        await settle()

    assert link.replies[-1].frame.cmd == 0x4490
    assert link.replies[-1].plaintext == device.module.mcu.push(0x490).cleartext


async def test_reconnect_with_an_enrolled_token_needs_no_button() -> None:
    device = A1783()
    device.module.enrolled.add(TOKEN)
    for _ in range(2):
        async with BleakClient(
            device.ble_device, backend=EmulatedBleakBackend
        ) as client:
            link = BleakLink(client, AppClient())
            await link.start()
            replies = await link.negotiate(TOKEN)

            assert replies[0x827].status == 0


async def test_cleartext_connect_drops_the_link() -> None:
    device = A1783()
    dropped = asyncio.Event()
    client = BleakClient(
        device.ble_device,
        disconnected_callback=lambda _client: dropped.set(),
        backend=EmulatedBleakBackend,
    )
    await client.connect()
    link = BleakLink(client, AppClient(encrypted_outer=False))
    await link.start()

    await link.send(0x001, [])

    assert dropped.is_set()
    assert not client.is_connected
    assert link.replies == []


async def test_authorize_timer_drops_an_unauthorized_link() -> None:
    clock = ManualClock()
    device = A1783(clock=clock)
    device.timer_period = 0.001
    dropped = asyncio.Event()
    client = BleakClient(
        device.ble_device,
        disconnected_callback=lambda _client: dropped.set(),
        backend=EmulatedBleakBackend,
    )
    await client.connect()

    clock.advance(30)
    await asyncio.wait_for(dropped.wait(), timeout=1)

    assert not client.is_connected
    assert not device.module.connected


async def test_stop_notify_silences_the_device() -> None:
    device = A1783()
    device.module.enrolled.add(TOKEN)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        await client.stop_notify(TELEMETRY_UUID)
        count = len(link.raw)

        device.push(0x421)
        await settle()

    assert len(link.raw) == count


async def test_second_client_is_refused() -> None:
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend):
        second = BleakClient(device.ble_device, backend=EmulatedBleakBackend)

        with pytest.raises(BleakError, match="already connected"):
            await second.connect()


async def test_pair_and_unpair_do_nothing() -> None:
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        await client.pair()
        await client.unpair()

        assert client.is_connected


async def test_only_the_command_characteristic_takes_writes() -> None:
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        with pytest.raises(BleakError, match="not writable"):
            await client.write_gatt_char(TELEMETRY_UUID, b"\x00", response=False)
        with pytest.raises(BleakError, match="not readable"):
            await client.read_gatt_char(TELEMETRY_UUID)
        with pytest.raises(BleakError, match="does not notify"):
            await client.start_notify(COMMAND_UUID, lambda _sender, _data: None)


async def test_descriptors_are_refused() -> None:
    device = A1783()
    backend = EmulatedBleakBackend(device.ble_device, timeout=10)
    await backend.connect(pair=False)
    assert backend.services is not None
    telemetry = backend.services.get_characteristic(TELEMETRY_UUID)
    assert telemetry is not None
    descriptor = BleakGATTDescriptor(
        None, 18, "00002902-0000-1000-8000-00805f9b34fb", telemetry
    )

    with pytest.raises(BleakError, match="not readable"):
        await backend.read_gatt_descriptor(descriptor)
    with pytest.raises(BleakError, match="not writable"):
        await backend.write_gatt_descriptor(descriptor, b"\x01\x00")


async def test_write_before_connect_is_refused() -> None:
    device = A1783()
    backend = EmulatedBleakBackend(device.ble_device, timeout=10)
    await backend.connect(pair=False)
    assert backend.services is not None
    command = backend.services.get_characteristic(COMMAND_UUID)
    assert command is not None
    await backend.disconnect()

    with pytest.raises(BleakError, match="Not connected"):
        await backend.write_gatt_char(command, b"\x00", response=False)


async def test_drop_after_the_client_left_is_a_no_op() -> None:
    device = A1783()
    dropped = asyncio.Event()
    client = BleakClient(
        device.ble_device,
        disconnected_callback=lambda _client: dropped.set(),
        backend=EmulatedBleakBackend,
    )
    await client.connect()
    request = AppClient(encrypted_outer=False).request(0x001, [])

    await client.write_gatt_char(COMMAND_UUID, request, response=False)
    await client.disconnect()
    await settle()

    assert not dropped.is_set()
    assert not device.module.connected


async def test_device_drop_without_a_callback() -> None:
    device = A1783()
    client = BleakClient(device.ble_device, backend=EmulatedBleakBackend)
    await client.connect()
    request = AppClient(encrypted_outer=False).request(0x001, [])

    await client.write_gatt_char(COMMAND_UUID, request, response=False)
    await settle()

    assert not client.is_connected


async def test_disconnect_before_connect_is_harmless() -> None:
    device = A1783()
    backend = EmulatedBleakBackend(device.ble_device, timeout=10)

    await backend.disconnect()

    assert not backend.is_connected
    assert not device.module.connected


def test_backend_needs_an_emulated_device() -> None:
    with pytest.raises(BleakError, match="EmulatedDevice"):
        EmulatedBleakBackend(BLEDevice("AA:12:DE:AD:BE:EF", None, None), timeout=10)
