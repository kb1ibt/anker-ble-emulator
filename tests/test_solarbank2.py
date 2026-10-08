# Copyright (c) 2026 Shawn Stricker
"""The Solarbank 2 E1600 Pro (A17C1) through a real ``bleak.BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A17C1, EmulatedBleakBackend, Outer
from tests.fixtures.client import SESSION, AppClient, BleakLink, settle


TOKEN = b"owner-token"
#: The recorded telemetry is 511 bytes, sent in three fragments.
TELEMETRY_FRAGMENTS = 3


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
