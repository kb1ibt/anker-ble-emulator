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
from anker_ble_emulator.parameters import Parameter, Parameters
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: The A1753's ``0830`` as it sent it to MQTT: v0.2.3.1, v3.5.6, no names.
C800_VERSIONS = bytes.fromhex("00a10876302e322e332e31a20676332e352e36")
#: The C1000 tags a C800 frame doesn't carry: no ``f9`` in its map, and ``fd``
#: is the C1000's own pack type.
C1000_ONLY_TAGS = {"f9", "fd"}


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
    for ours, theirs in (
        (status[0].plaintext, c1000.respond(0x040)[0].payload),
        (pushed.plaintext, c1000.push(0x402).payload),
    ):
        c800_fields, c1000_fields = Parameters.parse(ours), Parameters.parse(theirs)
        assert set(c800_fields) == set(c1000_fields) - C1000_ONLY_TAGS
        assert all(
            Parameter.build(c800_fields[tag]) == Parameter.build(c1000_fields[tag])
            for tag in c800_fields
        )


def test_the_c800_advertises_nothing_recorded() -> None:
    assert A1753().local_name is None
    assert A1753().advertisement_data.manufacturer_data == {}
