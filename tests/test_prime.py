# Copyright (c) 2026 Shawn Stricker
"""The Prime devices (A2345, A91B2) through a real ``bleak.BleakClient``."""

from bleak import BleakClient

from anker_ble_emulator import A91B2, A2345, EmulatedBleakBackend, Outer
from anker_ble_emulator.devices import COMPANY_ID
from tests.fixtures.client import (
    SESSION,
    TIMESTAMP,
    AppClient,
    BleakLink,
    cmd_hex,
    settle,
)


TOKEN = b"owner-token"
ACCOUNT = b"a" * 40


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


async def test_an_unnamed_device_reports_its_address_as_its_name() -> None:
    backend = EmulatedBleakBackend(A91B2().ble_device, timeout=10)

    assert backend.name == "AA-12-DE-AD-BE-EF"
