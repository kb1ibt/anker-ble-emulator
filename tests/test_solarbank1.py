# Copyright (c) 2026 Shawn Stricker
"""The Solarbank E1600 (A17C0) through a real ``bleak.BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A17C0, PRODUCTS, EmulatedBleakBackend, Outer, Product
from anker_ble_emulator.devices.a17c0 import TELEMETRY_405
from anker_ble_emulator.devices.base import data_resource
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: ``b4``/``b5``/``b6`` of the ``0405`` telemetry: the three cutoffs (ui).
OUTPUT_CUTOFF_TAG = 0xB4
LOWPOWER_INPUT_TAG = 0xB5
#: ``a3`` of the ``0405`` telemetry is ``battery_level`` (ui).
BATTERY_SOC_TAG = 0xA3


def test_a17c0_fields_match_the_recorded_frame() -> None:
    recorded = decode_fields(bytes.fromhex(data_resource("a17c0.json")["405"]))

    for field in TELEMETRY_405:
        raw = recorded[int(field.tag, 16)]
        assert raw[0] == field.type_byte, field.name
        assert len(raw) - 1 == field.codec.sizeof(), field.name


async def test_a17c0_answers_its_status_request_with_its_recorded_telemetry() -> None:
    device = A17C0()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)
        device.push(0x405)
        await settle()
        check = await link.send(0x056, [(0xA1, b"\x21")], SESSION)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    pushed = link.replies[-len(check) - len(version) - 1]
    recorded = data_resource("a17c0.json")
    assert device.outer == Outer.PLAIN
    assert [cmd_hex(reply.frame) for reply in status] == ["4405"]
    assert status[0].plaintext == bytes.fromhex(recorded["405"])
    assert pushed.frame.cmd.msgtype == 0x405
    assert [reply.plaintext for reply in check] == [bytes.fromhex(recorded["856"])]
    assert version == []
    assert device.advertisement_data.manufacturer_data == {}
    assert PRODUCTS[Product.A17C0].solixble_class == ""


async def test_a17c0_setters_are_checked_and_show_in_its_telemetry() -> None:
    device = A17C0()
    device.set_values(battery_level=55)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        accepted = await link.send(
            0x067, [(0xA1, b"\x21"), (0xA2, b"\x01\x05"), (0xA3, b"\x01\x04")], SESSION
        )
        refused = await link.send(
            0x067, [(0xA1, b"\x21"), (0xA2, b"\x01\x07")], SESSION
        )
        unit = await link.send(0x050, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION)
        status = await link.send(0x040, [(0xA1, b"\x21")], SESSION)

    fields = decode_fields(status[0].plaintext)
    assert [reply.plaintext for reply in accepted] == [bytes.fromhex("00a10131")]
    assert [reply.plaintext for reply in refused] == [bytes.fromhex("04a10131")]
    assert [reply.plaintext for reply in unit] == [bytes.fromhex("04a10131")]
    assert fields[OUTPUT_CUTOFF_TAG] == b"\x01\x05"
    assert fields[LOWPOWER_INPUT_TAG] == b"\x01\x04"
    assert fields[BATTERY_SOC_TAG] == b"\x01\x37"


def test_a17c0_builds_its_telemetry_from_zero() -> None:
    layout = A17C0().layout
    assert layout is not None

    built = decode_fields(layout.build(0x405, {"temperature": -5}))

    assert built[0xA1] == b"\x31"
    assert built[0xAA] == b"\x01\xfb"
    assert built[0xA2] == b"\x00" + bytes(16)
    assert layout.locate("min_load") == (0x405,)
    assert layout.names(0x408) == frozenset()
