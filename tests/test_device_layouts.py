# Copyright (c) 2026 Shawn Stricker
"""Devices with their anker-solix-api layouts, and the cloud switch."""

import pytest
from bleak import BleakClient
from bleak.exc import BleakError

from anker_ble_emulator import A1783, A2345, EmulatedBleakBackend
from anker_ble_emulator.layouts import Layout, LayoutError
from anker_ble_emulator.mcu import McuScript
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: ``a5`` of the A1783's ``0421``: temperature, charge state, then ``battery_soc``.
BATTERY_SOC_OFFSET = 1 + 2
#: ``a7`` of the A1783's ``0421`` leads with ``ac_output_power_switch``.
AC_SWITCH_OFFSET = 1


def test_every_device_loads_its_layout() -> None:
    assert A1783().layout is not None
    assert A2345().layout is not None
    assert Layout.load("A0000") is None


def test_a_telemetry_value_set_by_name_reaches_the_recorded_frame() -> None:
    device = A1783()

    device.set_values(battery_soc=55)

    push = device.module.mcu.push(0x421, values=device.module.values)
    assert decode_fields(push.payload)[0xA5][BATTERY_SOC_OFFSET] == 55
    recorded = device.module.mcu.push(0x421)
    assert decode_fields(recorded.payload)[0xA5][BATTERY_SOC_OFFSET] != 55


def test_a_value_set_on_the_device_reaches_every_message_that_carries_it() -> None:
    device = A1783()
    script, summary = device.mcu, device.mcu.summary
    assert summary is not None

    device.set_values(battery_soc=55)

    status, telemetry = script.respond(0x100, values=device.module.values)
    post = script.push(0x490, values=device.module.values)
    assert decode_fields(status.payload[1:])[0xA5][BATTERY_SOC_OFFSET] == 55
    assert decode_fields(telemetry.payload)[0xA5][BATTERY_SOC_OFFSET] == 55
    assert summary.read(post.payload)["battery_soc"] == 55


def test_a_part_past_offset_nine_lands_at_its_decimal_offset() -> None:
    device = A1783()

    device.set_values(display_timeout_seconds=300)

    a4 = decode_fields(device.mcu.push(0x421, values=device.module.values).payload)[
        0xA4
    ]
    assert a4[1 + 15 : 1 + 17] == (300).to_bytes(2, "little")


def test_an_mcu_without_a_layout_sends_its_frames_as_recorded() -> None:
    script = A1783().mcu
    quiet = McuScript(pushes=script.pushes)

    push = quiet.push(0x421, values={"battery_soc": 55})

    assert push.payload == script.push(0x421).payload


def test_a_value_the_layout_doesnt_type_is_refused() -> None:
    device = A1783()

    with pytest.raises(LayoutError):
        device.set_values(no_such_field=1)


def test_a_device_without_a_layout_or_summary_takes_no_values() -> None:
    device = A2345()
    device.layout = None

    with pytest.raises(LayoutError, match="sends no typed field"):
        device.set_values(battery_soc=1)


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
    device.set_values(battery_soc=42)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        status = await link.send(0x100, [(0xA1, b"\x21")], SESSION)

    telemetry = next(reply for reply in status if cmd_hex(reply.frame) == "4421")
    assert decode_fields(telemetry.plaintext)[0xA5][BATTERY_SOC_OFFSET] == 42


def test_replies_and_pushes_can_be_set_and_silenced() -> None:
    device = A1783()

    device.set_reply(
        0x100, (0x900, bytes.fromhex("00a10131")), (0x421, b"\xa1\x01\x31")
    )
    device.set_push(0x421, bytes.fromhex("a10131a20101"))

    assert [frame.cmd.msgtype for frame in device.mcu.respond(0x100)] == [0x900, 0x421]
    assert device.mcu.push(0x421).payload == bytes.fromhex("a10131a20101")
    device.set_reply(0x100)
    assert device.mcu.respond(0x100) == []
    prime = A2345()
    prime.set_reply(0x207, (0xA07, b"\xa1\x01\x31"))
    assert prime.mcu.respond(0x207)[0].payload == b"\xa1\x01\x31"
    device.use_mcu(McuScript())
    assert device.mcu.respond(0x057) == []
    with pytest.raises(KeyError):
        device.mcu.push(0x421)


async def test_an_accepted_setter_shows_in_the_telemetry_after_it() -> None:
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        on = await link.send(0x101, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        off = await link.send(0x101, [(0xA1, b"\x21"), (0xA2, b"\x01\x00")], SESSION)
        bad = await link.send(0x101, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION)
        untyped = await link.send(0x101, [(0xA1, b"\x21"), (0xA2, b"\x01")], SESSION)

    switch = [
        decode_fields(reply.plaintext)[0xA7][AC_SWITCH_OFFSET]
        for reply in (*on, *off, *bad, *untyped)
        if cmd_hex(reply.frame) == "4421"
    ]
    assert [cmd_hex(reply.frame) for reply in on] == ["4901", "4421"]
    assert switch == [1, 0, 0, 0]
    assert [reply.plaintext for reply in (on[0], bad[0], untyped[0])] == [
        bytes.fromhex("00a10131")
    ] * 3


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
