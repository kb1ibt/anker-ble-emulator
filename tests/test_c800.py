# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C800 (A1753) through a real ``BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import (
    A1753,
    A1761,
    EmulatedBleakBackend,
    ModuleBuild,
    Outer,
)
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: The A1753's ``0830`` as it sent it to MQTT: v0.2.3.1, v3.5.6, no names.
C800_VERSIONS = bytes.fromhex("00a10876302e322e332e31a20676332e352e36")


async def test_the_c800_reports_its_versions_and_the_c1000_telemetry() -> None:
    device = A1753()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)
        ac_on = await link.send(0x04A, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        device.push(0x402)
        await settle()

    assert device.outer == Outer.PLAIN
    assert device.module_build is ModuleBuild.V0_2_3_1
    assert version[0].plaintext == C800_VERSIONS
    assert [cmd_hex(reply.frame) for reply in status] == ["4840"]
    assert ac_on == []
    pushed = link.replies[-1]
    assert pushed.frame.cmd.msgtype == 0x402
    c1000 = A1761().mcu
    assert status[0].plaintext == c1000.respond(0x040)[0].payload
    assert pushed.plaintext == c1000.push(0x402).payload


def test_the_c800_advertises_nothing_recorded() -> None:
    assert A1753().local_name is None
    assert A1753().advertisement_data.manufacturer_data == {}
