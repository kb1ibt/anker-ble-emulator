# Copyright (c) 2026 Shawn Stricker
"""The SOLIX F2600 (A1781) through a real ``BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import (
    A1781,
    EmulatedBleakBackend,
    ModuleBuild,
    Outer,
    Transport,
)
from anker_ble_emulator.devices.base import data_resource
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex


TOKEN = b"owner-token"


async def test_answers_its_status_read_with_its_recorded_c840() -> None:
    device = A1781()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)
        ac_on = await link.send(0x04A, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)

    assert device.transport is Transport.NEGOTIATED
    assert device.outer == Outer.PLAIN
    assert device.module_build is ModuleBuild.ESP32_0_0_0_3
    assert [cmd_hex(reply.frame) for reply in status] == ["4840"]
    assert status[0].plaintext == bytes.fromhex(data_resource("a1781.json")["840"])
    assert version == []
    assert ac_on == []
    assert device.layout is None
    assert device.advertisement_data.manufacturer_data == {}
