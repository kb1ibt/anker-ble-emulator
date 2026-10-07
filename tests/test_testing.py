# Copyright (c) 2026 Shawn Stricker
"""``EmulatedConnection``: the MockDevice drop-in over a real emulated device."""

import asyncio

import pytest
from bleak.exc import BleakError

from anker_ble_emulator import A1783, COMMAND_UUID, TELEMETRY_UUID
from anker_ble_emulator.testing import EmulatedConnection, RequestResponse
from tests.fixtures.client import TIMESTAMP, AppClient, BleakLink, settle
from tests.fixtures.consumer import CONSUMER_TARGET, connect, ignore_disconnect


TOKEN = b"owner-token"
#: The app's 4001: a1 = TIMESTAMP, under the static key.
OPEN_4001 = AppClient().request(0x001, [(0xA1, TIMESTAMP)])


async def test_establish_connection_returns_a_connected_client() -> None:
    device = A1783()
    device.module.enrolled.add(TOKEN)
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        client = await connect(device.ble_device, ignore_disconnect)
        link = BleakLink(client, AppClient())
        await link.start()

        replies = await link.negotiate(TOKEN)

        assert client.is_connected
        assert emulated.clients == [client]
        assert replies[0x827].status == 0
        assert emulated.notify_uuids == [TELEMETRY_UUID]
        assert set(emulated.write_uuids) == {COMMAND_UUID}
        assert emulated.writes[0] == OPEN_4001


async def test_exit_disconnects_and_unpatches() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET):
        client = await connect(device.ble_device, ignore_disconnect)

    assert not client.is_connected
    with pytest.raises(RuntimeError, match="not patched"):
        await connect(device.ble_device, ignore_disconnect)


async def test_expected_writes_pass_and_are_checked() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        emulated.expect_ordered(OPEN_4001)
        emulated.expect_ordered()
        client = await connect(device.ble_device, ignore_disconnect)
        link = BleakLink(client, AppClient())
        await link.start()

        await link.send(0x001, [(0xA1, TIMESTAMP)])
        await link.send(0x029, [(0xA1, TIMESTAMP)])

        emulated.check_assertions()


async def test_a_write_other_than_expected_fails() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        emulated.expect_ordered(b"\x00")
        client = await connect(device.ble_device, ignore_disconnect)

        with pytest.raises(AssertionError, match="expected 00"):
            await client.write_gatt_char(COMMAND_UUID, OPEN_4001, response=False)


async def test_a_write_past_the_expectations_fails() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        emulated.expect_ordered_all([RequestResponse("open", OPEN_4001)])
        client = await connect(device.ble_device, ignore_disconnect)
        await client.write_gatt_char(COMMAND_UUID, OPEN_4001, response=False)

        with pytest.raises(AssertionError, match="all made"):
            await client.write_gatt_char(COMMAND_UUID, OPEN_4001, response=False)


async def test_an_expectation_never_met_fails_the_check() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        emulated.expect_ordered(OPEN_4001)

        with pytest.raises(AssertionError, match="num 0"):
            emulated.check_assertions()


async def test_scripted_response_follows_the_device_reply() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        emulated.expect_ordered(OPEN_4001, [b"extra"])
        client = await connect(device.ble_device, ignore_disconnect)
        received: list[bytes] = []
        await client.start_notify(
            TELEMETRY_UUID, lambda _sender, data: received.append(bytes(data))
        )

        await client.write_gatt_char(COMMAND_UUID, OPEN_4001, response=False)
        await settle()

    assert len(received) == 2
    assert received[1] == b"extra"


async def test_refused_write_drops_the_link_and_the_next_connect_works() -> None:
    device = A1783()
    dropped = asyncio.Event()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        emulated.refuse_after(OPEN_4001)
        client = await connect(device.ble_device, lambda _client: dropped.set())

        await client.write_gatt_char(COMMAND_UUID, OPEN_4001, response=False)
        await settle()
        again = await connect(device.ble_device, ignore_disconnect)

        assert dropped.is_set()
        assert not client.is_connected
        assert again.is_connected


async def test_refused_during_write_raises() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        emulated.refuse_after(during_write=True)
        client = await connect(device.ble_device, ignore_disconnect)

        with pytest.raises(BleakError, match="disconnected"):
            await client.write_gatt_char(COMMAND_UUID, OPEN_4001, response=False)

        assert not client.is_connected


async def test_connection_errors_until_allowed() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        emulated.new_connection_error(BleakError("no device"))

        with pytest.raises(BleakError, match="no device"):
            await connect(device.ble_device, ignore_disconnect)
        emulated.allow_connect()
        client = await connect(device.ble_device, ignore_disconnect)

        assert client.is_connected


async def test_disconnect_drops_from_the_device_side() -> None:
    device = A1783()
    dropped = asyncio.Event()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        client = await connect(device.ble_device, lambda _client: dropped.set())

        emulated.disconnect()
        await settle()

        assert dropped.is_set()
        assert not client.is_connected


async def test_send_data_notifies_raw_bytes() -> None:
    device = A1783()
    async with EmulatedConnection(device, CONSUMER_TARGET) as emulated:
        client = await connect(device.ble_device, ignore_disconnect)
        received: list[bytes] = []
        await client.start_notify(
            TELEMETRY_UUID, lambda _sender, data: received.append(bytes(data))
        )

        await emulated.send_data([b"\x01", b"\x02"])

    assert received == [b"\x01", b"\x02"]
