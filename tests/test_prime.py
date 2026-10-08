# Copyright (c) 2026 Shawn Stricker
"""The Prime devices (A2345, A2687, A91B2) through a real ``bleak.BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A91B2, A2345, A2687, EmulatedBleakBackend, Outer
from anker_ble_emulator.devices import COMPANY_ID
from tests.fixtures.client import (
    SESSION,
    TIMESTAMP,
    AppClient,
    BleakLink,
    cmd_hex,
    pattern_hex,
    settle,
)


TOKEN = b"owner-token"
ACCOUNT = b"a" * 40
#: An A2345's ``0830``: v0.2.9.7, v2.1.1.6, ``A2345``, ``A2345_mcu``, ``A2345_esp32``.
A2345_VERSIONS = bytes.fromhex(
    "00a10876302e322e392e37a20876322e312e312e36a3054132333435"
    "a40941323334355f6d6375a50b41323334355f6573703332"
)


async def test_a91b2_negotiates_in_clear_and_authorizes_at_the_key_exchange() -> None:
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()

        replies = await link.negotiate(TOKEN)

        assert replies[0x803].fields[0xA5] == b"\x00"
        assert not replies[0x821].frame.cmd.encrypted
        assert replies[0x822].frame.cmd.encrypted
        assert device.module.authorized


async def test_a91b2_snapshot_arrives_whole_and_the_keepalive_starts_the_stream() -> (
    None
):
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        snapshot = await link.send(0x200, [(0xA1, b"\x21")], SESSION)
        keepalive = await link.send(0x20B, [(0xA1, b"\x21")], SESSION)

    script = device.module.mcu
    assert [cmd_hex(reply.frame) for reply in snapshot] == ["4a00"]
    assert snapshot[0].plaintext == script.respond(0x200)[0].payload
    assert all(len(data) <= 253 for data in link.raw)
    assert [cmd_hex(reply.frame) for reply in keepalive] == ["4a0b", "4303"]


async def test_bind_echoes_the_serial_and_needs_the_account() -> None:
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        without = await link.send(0x023, [(0xA1, TIMESTAMP)])
        bound = await link.send(0x023, [(0xA1, TIMESTAMP), (0xA2, ACCOUNT)])

    assert without[0].status == 0x04
    assert bound[0].status == 0x00
    assert bound[0].fields == {0xA1: b"AFYJTB0000000001"}


async def test_a2345_grants_by_button_then_sends_a_fragmented_snapshot() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        replies = await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        authorized = device.module.authorized
        snapshot = await link.send(0x200, [(0xA1, b"\x21")], SESSION)

    assert replies[0x827].status == 0x09
    assert authorized
    assert cmd_hex(snapshot[0].frame) == "4a00"
    assert snapshot[0].plaintext == device.module.mcu.respond(0x200)[0].payload
    assert sum(1 for data in link.raw if len(data) == 253) >= 1


async def test_a2345_also_accepts_the_plain_outer() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()

        replies = await link.negotiate(TOKEN)

        assert replies[0x801].status == 0
        assert device.module.authorized


def test_a2345_advertises_a_prime_style_name() -> None:
    device = A2345(mac="AA:12:DE:AD:BE:EF")

    assert device.local_name == "A2345_BEEF"
    assert device.ble_device.name == "A2345_BEEF"
    assert device.advertisement_data.manufacturer_data[COMPANY_ID].hex() == (
        "01aa12deadbeef00b402514a4204"
    )


def test_a91b2_advertises_no_name_and_capability_zero() -> None:
    device = A91B2()

    assert device.local_name is None
    assert device.outer == Outer.PLAIN
    assert device.advertisement_data.manufacturer_data[COMPANY_ID].hex() == (
        "01aa12deadbeef00b4014a544200"
    )


async def test_a2345_version_read_matches_the_recorded_reply() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    assert [cmd_hex(reply.frame) for reply in version] == ["4830"]
    assert version[0].plaintext == A2345_VERSIONS


async def test_a2687_reports_its_module_and_answers_on_channel_11() -> None:
    device = A2687()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        replies = await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        status = await link.send(0x200, [(0xA1, b"\x21")], SESSION)
        usb = await link.send(0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)
        device.push(0x300)
        await settle()

    info = replies[0x829].fields
    assert info[0xA2] == b"Charging"
    assert info[0xA3] == b"v0.0.5.0"
    assert info[0xA5] == device.mac + b"00000000001"
    assert replies[0x803].fields[0xA2] == (297).to_bytes(2, "little")
    assert [cmd_hex(reply.frame) for reply in status] == ["4a00"]
    assert status[0].plaintext == device.mcu.respond(0x200)[0].payload
    assert [reply.plaintext for reply in usb] == [bytes.fromhex("00a10131")]
    assert version == []
    pushed = link.replies[-1]
    assert cmd_hex(pushed.frame) == "4300"
    assert {pattern_hex(reply.frame) for reply in [*status, *usb, pushed]} == {"030111"}


def test_a2687_advertises_no_record() -> None:
    device = A2687()

    assert device.local_name is None
    assert device.advertisement_data.manufacturer_data == {}
    assert device.module_build.session_ops == frozenset()


async def test_an_unnamed_device_reports_its_address_as_its_name() -> None:
    backend = EmulatedBleakBackend(A91B2().ble_device, timeout=10)

    assert backend.name == "AA-12-DE-AD-BE-EF"
