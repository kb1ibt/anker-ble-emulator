# Copyright (c) 2026 Shawn Stricker
"""The Solarbank 2 models (A17C1, A17C3) through a real ``bleak.BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A17C1, A17C3, EmulatedBleakBackend, Outer
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import SESSION, AppClient, BleakLink, settle


TOKEN = b"owner-token"
#: The recorded telemetry is 511 bytes, sent in three fragments.
TELEMETRY_FRAGMENTS = 3
#: ``c2`` of the Solarbank 2 ``c405`` is ``max_load`` (sile, int16 LE).
MAX_LOAD_TAG = 0xC2


async def test_a17c1_pushes_its_telemetry_in_three_fragments() -> None:
    device = A17C1()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        before = len(link.raw)
        device.push(0x405)
        await settle()
        fragments = len(link.raw) - before
        device.push(0x409)
        await settle()
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    telemetry, status = link.replies[-2:]
    assert device.outer == Outer.PLAIN
    assert fragments == TELEMETRY_FRAGMENTS
    assert telemetry.frame.cmd.msgtype == 0x405
    assert telemetry.plaintext == device.mcu.push(0x405).payload
    assert status.plaintext == bytes.fromhex("a10131a2020100a302012f")
    assert version == []
    assert device.advertisement_data.manufacturer_data == {}


async def test_a17c3_telemetry_is_typed_as_the_a17c1_s_recorded_c405() -> None:
    device = A17C3()
    recorded = decode_fields(A17C1().mcu.push(0x405).payload)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        max_load = await link.send(
            0x080, [(0xA1, b"\x21"), (0xA2, b"\x02\x20\x03")], SESSION
        )
        device.push(0x405)
        await settle()

    telemetry = link.replies[-1]
    fields = decode_fields(telemetry.plaintext)
    assert device.outer == Outer.PLAIN
    assert telemetry.frame.cmd.msgtype == 0x405
    assert [reply.plaintext for reply in max_load] == [bytes.fromhex("00a10131")]
    assert fields[MAX_LOAD_TAG] == b"\x02\x20\x03"
    assert {tag: (raw[0], len(raw)) for tag, raw in fields.items()} == {
        tag: (recorded[tag][0], len(recorded[tag])) for tag in fields
    }
