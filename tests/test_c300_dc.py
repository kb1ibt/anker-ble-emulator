# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C300 DC (A1726) through a real ``BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A1726, EmulatedBleakBackend, ModuleBuild, Outer
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: The ``0830`` the A1726 sent to MQTT: v0.1.5.8, v0.1.2.8, no names.
C300_DC_VERSIONS = bytes.fromhex("00a10876302e312e352e38a20876302e312e322e38")


async def test_the_c300_dc_reports_its_versions_acks_setters_and_pushes() -> None:
    device = A1726()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)
        dc_off = await link.send(0x04B, [(0xA1, b"\x21"), (0xA2, b"\x01\x00")], SESSION)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)
        device.push(0x402)
        await settle()

    assert device.outer == Outer.PLAIN
    assert device.module_build is ModuleBuild.V0_1_5_8
    assert version[0].plaintext == C300_DC_VERSIONS
    assert [cmd_hex(reply.frame) for reply in dc_off] == ["484b"]
    assert [reply.plaintext for reply in dc_off] == [bytes.fromhex("00a10131")]
    assert status == []
    pushed = link.replies[-1]
    assert pushed.frame.cmd.msgtype == 0x402
    assert pushed.plaintext == device.mcu.push(0x402).payload


def test_the_c300_dc_advertises_its_name() -> None:
    c300_dc = A1726()

    assert c300_dc.local_name == "Anker SOLIX C300 DC"
    assert c300_dc.advertisement_data.manufacturer_data == {}
