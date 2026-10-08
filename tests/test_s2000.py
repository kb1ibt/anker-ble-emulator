# Copyright (c) 2026 Shawn Stricker
"""The SOLIX S2000 (AS220) through a real ``bleak.BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import AS220, EmulatedBleakBackend, Outer
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import SESSION, AppClient, BleakLink, settle


TOKEN = b"a5220000-5011-4000-b000-000000000001"
#: The subscribe SolixBLE's AS220 sends to start the stream.
SUBSCRIBE = [(0xA1, b"\x21"), (0xA2, b"\x04\x01")]
#: The power block; PR#65 verified its AC input plug status and input total.
A6 = 0xA6


async def test_as220_grants_by_button_then_streams_on_subscribe() -> None:
    device = AS220()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        replies = await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        stream = await link.send(0x100, SUBSCRIBE, SESSION)
        setter = await link.send(0x057, [(0xA1, b"\x21")], SESSION)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    assert device.outer == Outer.ENCRYPTED
    assert replies[0x827].status == 0x09
    assert replies[0x829].fields[0xA2] == b"ESP32"
    assert [reply.frame.cmd.msgtype for reply in stream] == [0x900, 0x421]
    assert stream[0].plaintext[1:] == stream[1].plaintext
    assert b"AS220" in stream[1].plaintext
    assert [reply.plaintext for reply in setter] == [bytes.fromhex("00a10131")]
    assert version == []
    assert device.advertisement_data.manufacturer_data == {}


def test_as220_map_offsets_read_the_values_the_pr_verified() -> None:
    device = AS220()
    assert device.layout is not None
    parts = {
        part.name: part
        for field in device.layout.messages[0x421]
        if field.tag == A6
        for part in field.parts
    }
    a6 = decode_fields(device.mcu.push(0x421).payload)[A6][1:]

    plug, total = parts["ac_input_plug_status"], parts["input_power_total"]
    assert (plug.offset, total.offset) == (10, 11)
    assert a6[plug.offset] == 1
    assert int.from_bytes(a6[total.offset : total.offset + 2], "little") == 55
    device.set_values(input_power_total=1129)
    pushed = decode_fields(device.mcu.push(0x421, values=device.module.values).payload)
    assert pushed[A6][1 + 11 : 1 + 13] == (1129).to_bytes(2, "little")


def test_as220_telemetry_takes_the_fields_its_map_types() -> None:
    device = AS220()

    device.set_values(msg_timestamp=0x01020304)

    for msgtype in (0x421, 0x900):
        frame = device.mcu.respond(0x100, values=device.module.values)
        payload = next(f.payload for f in frame if f.cmd.msgtype == msgtype)
        start = payload[:1] if msgtype == 0x900 else b""
        assert decode_fields(payload[len(start) :])[0xFE] == bytes.fromhex("0304030201")
