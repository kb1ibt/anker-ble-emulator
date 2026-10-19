# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C300 (A1722) through a real ``BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A1722, EmulatedBleakBackend, ModuleBuild, Outer
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex


TOKEN = b"owner-token"
#: The A1722's ``0830`` as anker-solix-api#348 recorded it: v0.2.9.8, v1.0.5.7
#: and the three component names.
A1722_VERSIONS = bytes.fromhex(
    "00a10876302e322e392e38a20876312e302e352e37a30a41313732325f68696768"
    "a40e41313732325f6d63755f68696768a51041313732325f65737033325f68696768"
)


async def test_a1722_negotiates_in_clear_and_reports_its_versions() -> None:
    device = A1722()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)
        dc_on = await link.send(0x04B, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)

    assert device.outer == Outer.PLAIN
    assert device.module_build is ModuleBuild.V0_2_9_8
    assert [cmd_hex(reply.frame) for reply in status] == ["4840"]
    assert version[0].plaintext == A1722_VERSIONS
    assert dc_on == []
    assert device.advertisement_data.manufacturer_data == {}
