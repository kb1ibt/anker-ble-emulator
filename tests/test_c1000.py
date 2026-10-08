# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C1000 (A1761), first generation, through a real ``bleak.BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A1761, EmulatedBleakBackend, ModuleBuild, Outer
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: The A1761's ``0830`` as anker-solix-api recorded it: v0.2.3.1, v1.5.9, no names.
A1761_VERSIONS = bytes.fromhex("00a10876302e322e332e31a20676312e352e39")
#: ``a6`` of the status frames is ``ac_output_power`` (sile, uint16 LE).
AC_OUTPUT_TAG = 0xA6


async def test_a1761_negotiates_in_clear_and_answers_its_status_read() -> None:
    device = A1761()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)
        ac_on = await link.send(0x04A, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)

    assert device.outer == Outer.PLAIN
    assert device.module_build is ModuleBuild.V0_2_3_1
    assert [cmd_hex(reply.frame) for reply in status] == ["4840"]
    assert status[0].plaintext == device.mcu.respond(0x040)[0].payload
    assert version[0].plaintext == A1761_VERSIONS
    assert [reply.plaintext for reply in ac_on] == [bytes.fromhex("00a10131")]


async def test_a1761_status_frames_take_values_by_their_map_names() -> None:
    device = A1761()
    device.set_values(ac_output_power=321)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)
        device.push(0x402)
        await settle()

    pushed = link.replies[-1]
    assert pushed.frame.cmd.msgtype == 0x402
    for payload in (status[0].plaintext[1:], pushed.plaintext):
        assert decode_fields(payload)[AC_OUTPUT_TAG][1:3] == (321).to_bytes(2, "little")


def test_a1761_advertises_no_record() -> None:
    device = A1761()

    assert device.local_name is None
    assert device.advertisement_data.manufacturer_data == {}
