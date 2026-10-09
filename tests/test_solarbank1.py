# Copyright (c) 2026 Shawn Stricker
"""The Solarbank E1600 (A17C0), map-built, through a real ``bleak.BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A17C0, PRODUCTS, EmulatedBleakBackend, Outer, Product
from anker_ble_emulator.devices import Profile
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: ``b4`` of the ``0405`` telemetry is ``output_cutoff_data`` (ui).
OUTPUT_CUTOFF_TAG = 0xB4
#: ``a3`` of the ``0405`` telemetry is ``battery_soc`` (ui).
BATTERY_SOC_TAG = 0xA3


async def test_a17c0_answers_its_status_request_with_its_telemetry() -> None:
    device = A17C0()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)
        device.push(0x405)
        await settle()
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    pushed = link.replies[-1]
    assert device.outer == Outer.PLAIN
    assert isinstance(device.profile, Profile)
    assert device.profile.map_built
    assert [cmd_hex(reply.frame) for reply in status] == ["4405"]
    assert status[0].plaintext.startswith(bytes.fromhex("a10131"))
    assert pushed.frame.cmd.msgtype == 0x405
    assert pushed.plaintext == status[0].plaintext
    assert version == []
    assert device.advertisement_data.manufacturer_data == {}
    assert PRODUCTS[Product.A17C0].solixble_class == ""


async def test_a17c0_setters_are_checked_and_show_in_its_telemetry() -> None:
    device = A17C0()
    device.set_values(battery_soc=55)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        accepted = await link.send(
            0x067, [(0xA1, b"\x21"), (0xA2, b"\x01\x0a")], SESSION
        )
        refused = await link.send(
            0x067, [(0xA1, b"\x21"), (0xA2, b"\x01\x07")], SESSION
        )
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)

    fields = decode_fields(status[0].plaintext)
    assert [reply.plaintext for reply in accepted] == [bytes.fromhex("00a10131")]
    assert [reply.plaintext for reply in refused] == [bytes.fromhex("04a10131")]
    assert fields[OUTPUT_CUTOFF_TAG] == b"\x01\x0a"
    assert fields[BATTERY_SOC_TAG] == b"\x01\x37"
