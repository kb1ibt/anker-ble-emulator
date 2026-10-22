# Copyright (c) 2026 Shawn Stricker
"""The Prime devices (A110B, A2345, A25X7, A2687, A91B2) over bleak."""

import pytest
from bleak import BleakClient

from anker_ble_emulator import (
    A25X7,
    A91B2,
    A110B,
    A2345,
    A2687,
    EmulatedBleakBackend,
    ManualClock,
    Outer,
)
from anker_ble_emulator.devices import COMPANY_ID
from anker_ble_emulator.devices.a91b2 import ClockTheme as A91B2ClockTheme
from anker_ble_emulator.devices.a91b2 import Display, Timer
from anker_ble_emulator.devices.a2345 import (
    SCREENSAVER_FIELDS,
    Schedule,
    Screensaver,
    ScreensaverRead,
    Theme,
)
from anker_ble_emulator.devices.base import data_resource
from anker_ble_emulator.devices.prime import OutputPort
from anker_ble_emulator.layouts import ui
from anker_ble_emulator.parameters import Parameters, decode_field
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
#: ``4827`` status until the owner confirms by button.
NEEDS_AUTHENTICATION = 0x09
#: A request's ``fe`` clock, which its pushes carry back.
CLOCK = (0xFE, bytes.fromhex("03933cd66a"))
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
        assert device.negotiated_module.authorized


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

    script = device.negotiated_module.mcu
    assert [cmd_hex(reply.frame) for reply in snapshot] == ["4a00"]
    assert snapshot[0].plaintext == script.respond(0x200)[0].payload
    assert all(len(data) <= 253 for data in link.raw)
    assert [cmd_hex(reply.frame) for reply in keepalive] == ["4a0b", "4303"]


async def test_a2345_420b_buys_ten_4303_pushes_a_second_apart() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    device.push_period = 0
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        start = len(link.replies)
        trigger = await link.send(0x20B, [(0xA1, b"\x21")], SESSION)
        clock.advance(0.5)
        await settle()
        half_second = len(link.replies)
        clock.advance(0.5)
        await settle()
        one_second = len(link.replies)
        clock.advance(8)
        await settle()
        nine_seconds = len(link.replies)
        clock.advance(5)
        await settle()
        fourteen_seconds = len(link.replies)

    pushes = [r for r in link.replies[start:] if cmd_hex(r.frame) == "4303"]
    assert [cmd_hex(reply.frame) for reply in trigger] == ["4a0b", "4303"]
    assert half_second == start + 2
    assert one_second == half_second + 1
    assert nine_seconds == fourteen_seconds == start + 1 + 10
    assert len(pushes) == 10


async def test_a_420b_mid_burst_restarts_the_count() -> None:
    clock = ManualClock()
    device = A91B2(clock=clock)
    device.push_period = 0
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)
        start = len(link.replies)
        await link.send(0x20B, [(0xA1, b"\x21")], SESSION)
        clock.advance(4)
        await settle()
        await link.send(0x20B, [(0xA1, b"\x21")], SESSION)
        clock.advance(30)
        await settle()

    pushes = [r for r in link.replies[start:] if cmd_hex(r.frame) == "4303"]
    # 5 from the first trigger before the second resets the count to 10.
    assert len(pushes) == 15


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
        authorized = device.negotiated_module.authorized
        snapshot = await link.send(0x200, [(0xA1, b"\x21")], SESSION)

    assert replies[0x827].status == NEEDS_AUTHENTICATION
    assert authorized
    assert cmd_hex(snapshot[0].frame) == "4a00"
    assert (
        snapshot[0].plaintext == device.negotiated_module.mcu.respond(0x200)[0].payload
    )
    assert sum(1 for data in link.raw if len(data) == 253) >= 1


async def test_a2345_factory_reset_acks_then_drops_the_link_and_forgets_the_owner() -> (
    None
):
    """``4201``: ``00`` ack, settings and pairing cleared, the link drops after 1 s."""
    clock = ManualClock()
    device = A2345(clock=clock)
    device.push_period = 0
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        device.set_values(screen_brightness=37)
        reset = await link.send(0x201, [(0xA1, b"\x21")], SESSION)
        clock.advance(0.5)
        await settle()
        during_debounce = client.is_connected
        clock.advance(0.5)
        await settle()
        after_debounce = client.is_connected
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        replies = await link.negotiate(TOKEN)

    assert reset[0].plaintext == bytes.fromhex("00a10131")
    assert during_debounce
    assert not after_debounce
    assert device.negotiated_module.values == {}
    assert replies[0x827].status == NEEDS_AUTHENTICATION


async def test_a2345_also_accepts_the_plain_outer() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()

        replies = await link.negotiate(TOKEN)

        assert replies[0x801].status == 0
        assert device.negotiated_module.authorized


def test_a2345_advertises_a_prime_style_name() -> None:
    device = A2345(mac="AA:12:DE:AD:BE:EF")

    assert device.local_name == "A2345_BEEF"
    assert device.ble_device.name == "A2345_BEEF"
    assert device.advertisement_data.manufacturer_data[COMPANY_ID].hex() == (
        "01aa12deadbeef00b402514a4204"
    )


def test_a91b2_advertises_its_prime_name_and_capability_zero() -> None:
    device = A91B2()

    assert device.local_name == "A91B2_BEEF"
    assert device.outer == Outer.PLAIN
    assert device.advertisement_data.manufacturer_data[COMPANY_ID].hex() == (
        "01aa12deadbeef00b4014a544200"
    )


async def test_a91b2_port_switch_covers_both_ac_ports_and_all_four_usbc_ports() -> None:
    """``4207``'s port select is 0-5 (ac_1, ac_2, usbc_1-4), not AC-only.

    PiField/docs/re/prime-a2345-a91b2.md "Commands (fid 0x0f...)":
    cmd207_set_output, port 5 firmware-confirmed by a live toggle.
    """
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        ac = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x00"), (0xA3, b"\x01\x01")], SESSION
        )
        usbc = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x02"), (0xA3, b"\x01\x01")], SESSION
        )
        out_of_range = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x06"), (0xA3, b"\x01\x01")], SESSION
        )
        bad_switch = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x00"), (0xA3, b"\x01\x02")], SESSION
        )

    assert [cmd_hex(reply.frame) for reply in ac] == ["4a07", "4302"]
    assert ac[0].plaintext == bytes.fromhex("00a10131a2020100")
    assert ac[1].plaintext == bytes.fromhex("a10131a2020100a3020101")
    assert [cmd_hex(reply.frame) for reply in out_of_range] == ["4a07"]
    assert usbc[0].plaintext == bytes.fromhex("00a10131a2020102")
    assert out_of_range[0].plaintext == bytes.fromhex("04a10131")
    assert bad_switch[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    # usbc_1 (a4) has real OutputPort telemetry; ac_1/ac_2 have none modeled
    # yet (PORTS_0A00 covers only usbc/usba), so the ac switch above acks
    # but has nothing to verify.
    assert decode_field(params, "a4", OutputPort).output_status == 1


def test_a91b2_a_named_value_reaches_both_messages_that_carry_it() -> None:
    device = A91B2()

    device.set_values(usb1_output_power=42)

    pushed = device.mcu.push(0x303, values=device.negotiated_module.values)
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    pushed_port = decode_field(Parameters.parse(pushed.payload), "a6", OutputPort)
    snapshot_port = decode_field(Parameters.parse(snapshot.payload), "a8", OutputPort)
    assert pushed_port.output_power == snapshot_port.output_power == 42


def test_a91b2_display_charging_and_clock_fields_are_all_settable_by_name() -> None:
    """The 0a00-only single-instance and scalar fields, not just ports/timers.

    PiField/docs/re/prime-a2345-a91b2.md "4a00 snapshot" (A91B2): ac/ad/ae/af
    and the b4/b5 scalars.
    """
    device = A91B2()

    device.set_values(
        screen_saver_switch=True,
        screen_saver_theme=5,
        screen_brightness=10,
        charging_mode=1,
        time_format=1,
        ac_light_mode=1,
    )

    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert params["ac"].value_legacy == bytes.fromhex("0185")
    assert params["ad"].value_legacy == bytes.fromhex("01a0")
    assert params["ae"].value_legacy == bytes.fromhex("040100")
    assert decode_field(params, "b4", ui(1)) == 1
    assert decode_field(params, "b5", ui(1)) == 1


async def test_a91b2_screen_timeout_set_matches_the_captured_byte() -> None:
    """PiField/docs/re/prime-a2345-a91b2.md: "ad 0x20 -> 0x24 on 30 min"."""
    device = A91B2()
    device.set_values(screen_brightness=2, screen_timeout=0)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        on = await link.send(0x203, [(0xA1, b"\x21"), (0xA2, b"\x01\x04")], SESSION)
        rejected = await link.send(
            0x203, [(0xA1, b"\x21"), (0xA2, b"\x01\x05")], SESSION
        )

    assert on[0].plaintext == bytes.fromhex("00a10131")
    assert rejected[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    assert Parameters.parse(snapshot.payload)["ad"].value_legacy == bytes.fromhex(
        "0124"
    )


async def test_a91b2_charging_mode_rejects_an_invalid_submode() -> None:
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        high_power = await link.send(
            0x206, [(0xA1, b"\x21"), (0xA2, b"\x02\x01\x01")], SESSION
        )
        bad_submode = await link.send(
            0x206, [(0xA1, b"\x21"), (0xA2, b"\x02\x01\x05")], SESSION
        )

    assert high_power[0].plaintext == bytes.fromhex("00a10131")
    assert bad_submode[0].plaintext == bytes.fromhex("04a10131")


async def test_a91b2_countdown_disarm_keeps_the_seconds() -> None:
    """PiField/docs/re/prime-a2345-a91b2.md: "en = 0 disarms and keeps the seconds"."""
    device = A91B2()
    device.set_values(ac1_countdown_task_switch=True, ac1_countdown_task_seconds=10800)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        disarm = await link.send(
            0x209,
            [(0xA1, b"\x21"), (0xA2, b"\x01\x00"), (0xA3, b"\x04\x00\x30\x2a\x00\x00")],
            SESSION,
        )

    assert disarm[0].plaintext == bytes.fromhex("00a10131a2020100")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    timer = decode_field(params, "aa", Timer)
    assert not timer.countdown_task_switch
    assert timer.countdown_task_seconds == 10800


async def test_a91b2_countdown_arms_with_its_seconds_and_rejects_a_bad_port() -> None:
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        armed = await link.send(
            0x209,
            [(0xA1, b"\x21"), (0xA2, b"\x01\x00"), (0xA3, b"\x04\x01\x78\x00\x00\x00")],
            SESSION,
        )
        bad_port = await link.send(
            0x209,
            [(0xA1, b"\x21"), (0xA2, b"\x01\x06"), (0xA3, b"\x04\x00\x00\x00\x00\x00")],
            SESSION,
        )

    assert armed[0].plaintext == bytes.fromhex("00a10131a2020100")
    assert bad_port[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    timer = decode_field(params, "aa", Timer)
    assert timer.countdown_task_switch
    assert timer.countdown_task_seconds == 120


async def test_a91b2_charging_mode_rejects_an_unsupported_mode() -> None:
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        bad_mode = await link.send(
            0x206, [(0xA1, b"\x21"), (0xA2, b"\x02\x02\x00")], SESSION
        )

    assert bad_mode[0].plaintext == bytes.fromhex("04a10131")


async def test_a91b2_port_schedule_command_sets_the_full_quad() -> None:
    """``4208``: ``a2`` port, ``a3`` slot (0 off-time / 1 on-time), ``a4`` quad."""
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        on_time = await link.send(
            0x208,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x02"),
                (0xA3, b"\x01\x01"),
                (0xA4, b"\x04\x01\x07\x1e\x1f"),
            ],
            SESSION,
        )
        bad_port = await link.send(
            0x208,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x06"),
                (0xA3, b"\x01\x01"),
                (0xA4, b"\x04\x01\x07\x1e\x1f"),
            ],
            SESSION,
        )
        bad_slot = await link.send(
            0x208,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x02"),
                (0xA3, b"\x01\x02"),
                (0xA4, b"\x04\x01\x07\x1e\x1f"),
            ],
            SESSION,
        )

    assert on_time[0].plaintext == bytes.fromhex("00a10131a2020102a3020101")
    assert bad_port[0].plaintext == bytes.fromhex("04a10131")
    assert bad_slot[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    usbc_1 = decode_field(params, "b0", Timer)
    start = (
        usbc_1.timed_task_start_switch,
        usbc_1.timed_task_start_hour,
        usbc_1.timed_task_start_minute,
        usbc_1.timed_task_start_weekdays,
    )
    assert start == (1, 7, 0x1E, 0x1F)


async def test_a91b2_screen_wake_time_format_and_ac_light_mode_commands_validate() -> (
    None
):
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        wake = await link.send(0x201, [(0xA1, b"\x21")], SESSION)
        time_format = await link.send(
            0x210, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        bad_time_format = await link.send(
            0x210, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )
        ac_light_mode = await link.send(
            0x214, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        bad_ac_light_mode = await link.send(
            0x214, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )

    assert wake[0].plaintext == bytes.fromhex("00a10131")
    assert time_format[0].plaintext == bytes.fromhex("00a10131")
    assert bad_time_format[0].plaintext == bytes.fromhex("04a10131")
    assert ac_light_mode[0].plaintext == bytes.fromhex("00a10131")
    assert bad_ac_light_mode[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "b4", ui(1)) == 1
    assert decode_field(params, "b5", ui(1)) == 1


async def test_a91b2_brightness_and_screen_saver_switch_commands_set_their_fields() -> (
    None
):
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        brightness = await link.send(
            0x204, [(0xA1, b"\x21"), (0xA2, b"\x01\x14")], SESSION
        )
        screen_saver_switch = await link.send(
            0x205, [(0xA1, b"\x21"), (0xA2, b"\x01\x86")], SESSION
        )

    assert brightness[0].plaintext == bytes.fromhex("00a10131")
    assert screen_saver_switch[0].plaintext == bytes.fromhex("00a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "ad", Display).screen_brightness == 4  # 0x14 & 0x0f
    clock = decode_field(params, "ac", A91B2ClockTheme)
    assert clock.screen_saver_switch
    assert clock.screen_saver_theme == 2  # theme 6 stored as the last, 2


def test_a91b2_sw_version_reads_the_version_digits() -> None:
    device = A91B2()

    assert device.sw_version == "1.1.2.4"


def test_a91b2_layout_rejects_a_msgtype_with_no_command() -> None:
    device = A91B2()

    assert device.layout is not None
    assert device.layout.check(0x999, b"") == 0x04


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


async def test_a2345_screen_timeout_brightness_and_theme_commands_validate() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        timeout = await link.send(
            0x203, [(0xA1, b"\x21"), (0xA2, b"\x01\x04")], SESSION
        )
        bad_timeout = await link.send(
            0x203, [(0xA1, b"\x21"), (0xA2, b"\x01\x05")], SESSION
        )
        brightness = await link.send(
            0x204, [(0xA1, b"\x21"), (0xA2, b"\x01\x96")], SESSION
        )
        theme = await link.send(0x205, [(0xA1, b"\x21"), (0xA2, b"\x01\x82")], SESSION)

    assert timeout[0].plaintext == bytes.fromhex("00a10131")
    assert bad_timeout[0].plaintext == bytes.fromhex("04a10131")
    assert brightness[0].plaintext == bytes.fromhex("00a10131")
    assert theme[0].plaintext == bytes.fromhex("00a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "b0", ui(1)) == 4
    assert decode_field(params, "b3", ui(1)) == 0x96
    theme_byte = decode_field(params, "af", Theme)
    assert theme_byte.screen_saver_switch
    assert theme_byte.screen_saver_theme == 2


async def test_a2345_screen_saver_theme_id_clock_off_and_custom_mode_switch() -> None:
    """``4205`` network theme ``83`` + ``a3`` id, clock off, and ``4211``.

    PiField/docs/re/prime-a2345-a91b2.md "4205" and "4211".
    """
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        network = await link.send(
            0x205,
            [(0xA1, b"\x21"), (0xA2, b"\x01\x83"), (0xA3, bytes.fromhex("03d4f89e1d"))],
            SESSION,
        )
        await link.send(0x20F, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        clock_off = await link.send(
            0x205, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        custom_on = await link.send(
            0x211, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        bad_custom = await link.send(
            0x211, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )

    assert network[0].plaintext == bytes.fromhex("00a10131")
    assert clock_off[0].plaintext == bytes.fromhex("00a10131")
    assert custom_on[0].plaintext == bytes.fromhex("00a10131")
    assert bad_custom[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    theme = decode_field(Parameters.parse(snapshot.payload), "af", Theme)
    assert not theme.screen_saver_switch
    assert not theme.holiday_screensaver_switch
    assert theme.screen_saver_theme == 3
    assert theme.screen_saver_theme_id == 496957652


async def test_a2345_charging_mode_and_holiday_switch_commands_validate() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        mode = await link.send(0x206, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION)
        noop = await link.send(0x206, [(0xA1, b"\x21"), (0xA2, b"\x01\x00")], SESSION)
        bad_mode = await link.send(
            0x206, [(0xA1, b"\x21"), (0xA2, b"\x01\x06")], SESSION
        )
        holiday = await link.send(
            0x20F, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        bad_holiday = await link.send(
            0x20F, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )

    assert mode[0].plaintext == bytes.fromhex("00a10131")
    assert noop[0].plaintext == bytes.fromhex("00a10131")
    assert bad_mode[0].plaintext == bytes.fromhex("04a10131")
    assert holiday[0].plaintext == bytes.fromhex("00a10131")
    assert bad_holiday[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "b1", ui(1)) == 2
    assert decode_field(params, "af", Theme).holiday_screensaver_switch


async def test_a2345_port_switch_schedule_and_countdown_cover_all_five_slots() -> None:
    """Port index 0-3 = usbc 1-4, 4 = usba (both ports share one slot)."""
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        switch = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x04"), (0xA3, b"\x01\x01")], SESSION
        )
        bad_port = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x05"), (0xA3, b"\x01\x01")], SESSION
        )
        on_time = await link.send(
            0x208,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x00"),
                (0xA3, b"\x01\x01"),
                (0xA4, b"\x04\x01\x09\x1e\x1f"),
            ],
            SESSION,
        )
        bad_schedule_port = await link.send(
            0x208,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x05"),
                (0xA3, b"\x01\x01"),
                (0xA4, b"\x04\x01\x09\x1e\x1f"),
            ],
            SESSION,
        )
        bad_slot = await link.send(
            0x208,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x00"),
                (0xA3, b"\x01\x02"),
                (0xA4, b"\x04\x01\x09\x1e\x1f"),
            ],
            SESSION,
        )
        countdown = await link.send(
            0x209,
            [(0xA1, b"\x21"), (0xA2, b"\x01\x00"), (0xA3, b"\x04\x01\x2c\x01\x00\x00")],
            SESSION,
        )
        disarm = await link.send(
            0x209,
            [(0xA1, b"\x21"), (0xA2, b"\x01\x01"), (0xA3, b"\x04\x00\x00\x00\x00\x00")],
            SESSION,
        )
        bad_countdown_port = await link.send(
            0x209,
            [(0xA1, b"\x21"), (0xA2, b"\x01\x05"), (0xA3, b"\x04\x00\x00\x00\x00\x00")],
            SESSION,
        )

    assert switch[0].plaintext == bytes.fromhex("00a10131a2020104")
    assert bad_port[0].plaintext == bytes.fromhex("04a10131")
    assert on_time[0].plaintext == bytes.fromhex("00a10131a2020100a3020101")
    assert bad_schedule_port[0].plaintext == bytes.fromhex("04a10131")
    assert bad_slot[0].plaintext == bytes.fromhex("04a10131")
    assert countdown[0].plaintext == bytes.fromhex("00a10131a2020100")
    assert disarm[0].plaintext == bytes.fromhex("00a10131a2020101")
    assert bad_countdown_port[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert (
        decode_field(params, "a8", OutputPort).output_status == 1
    )  # usba_1 switched on
    assert (
        decode_field(params, "a9", OutputPort).output_status == 1
    )  # usba_2 switched on
    usbc_1 = decode_field(params, "aa", Schedule)
    start = (
        usbc_1.timed_task_start_switch,
        usbc_1.timed_task_start_hour,
        usbc_1.timed_task_start_minute,
        usbc_1.timed_task_start_weekdays,
    )
    assert start == (1, 9, 0x1E, 0x1F)
    assert usbc_1.countdown_task_switch
    assert usbc_1.countdown_task_seconds == 300
    assert not decode_field(
        params, "ab", Schedule
    ).countdown_task_switch  # usbc_2 disarmed


@pytest.mark.parametrize(
    ("model", "command", "reply"),
    [
        pytest.param(A2345, 0x212, None, id="A2345 4212 handler sends nothing"),
        pytest.param(A2345, 0x215, None, id="A2345 4215 no-op stub"),
        pytest.param(A2345, 0x21F, None, id="A2345 421f no-op stub"),
        pytest.param(A2345, 0x224, "00a10131", id="A2345 past the table"),
        pytest.param(A2345, 0x072, "00a10131", id="A2345 4072 daily window"),
        pytest.param(A2345, 0x0A0, None, id="A2345 low op not inline"),
        pytest.param(A91B2, 0x20C, None, id="A91B2 420c no-op stub"),
        pytest.param(A91B2, 0x21A, None, id="A91B2 421a no-op stub"),
        pytest.param(A91B2, 0x21D, "00a10131", id="A91B2 past the table"),
        pytest.param(A91B2, 0x072, "00a10131", id="A91B2 4072 daily window"),
        pytest.param(A91B2, 0x0A0, None, id="A91B2 low op not inline"),
    ],
)
def test_a_prime_mcu_answers_what_nothing_records_as_its_firmware_does(
    model: type[A2345 | A91B2], command: int, reply: str | None
) -> None:
    """prime-a2345-a91b2.md "Commands": handlers ack, stubs and low ops say nothing."""
    frames = model().mcu.respond(command)

    expected = [] if reply is None else [(command | 0x800, reply)]
    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == expected


@pytest.mark.parametrize(
    ("command", "request_fields", "expected"),
    [
        pytest.param(
            0x20D,
            "a2020201",
            [(0xA0D, "00a10131"), (0x305, "a10131a2020100")],
            id="420d nonzero acks and triggers 0305",
        ),
        pytest.param(0x20D, "a2020200", [(0xA0D, "00a10131")], id="420d zero acks"),
        pytest.param(
            0x20D, "a2020101", [(0xA0D, "04a10131")], id="420d a2 typed 01 refused"
        ),
        pytest.param(0x20D, "", [(0xA0D, "04a10131")], id="420d without a2 refused"),
        pytest.param(0x222, "a2020101", [], id="4222 set silently"),
        pytest.param(0x222, "a2020105", [], id="4222 other value silently"),
        pytest.param(0x222, "", [(0xA22, "04a10131")], id="4222 without a2 refused"),
    ],
)
def test_an_a2345_handler_answers_by_its_firmware_branch(
    command: int, request_fields: str, expected: list[tuple[int, str]]
) -> None:
    """``420d`` 0x00043938 (its ``0305`` 0x0001475c) and ``4222`` 0x0004ff84.

    prime-a2345-a91b2.md "Commands".
    """
    request = bytes.fromhex("a10121" + request_fields)

    frames = A2345().mcu.respond(command, request=request)

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == expected


def test_a2345_4222_sets_the_screensaver_reads_a6() -> None:
    """``4222`` 0/1 sets ``4a02 a6``; any other value leaves it (0x0004ff84)."""
    device = A2345()
    layout = device.layout
    assert layout is not None
    state = device.mcu.state({})
    clear = bytes.fromhex("a10121a2020100")

    assert layout.state_changes(0x222, clear, state) == {"port_widgets_switch": 0}
    assert layout.state_changes(0x222, bytes.fromhex("a10121a2020105"), state) == {}
    assert state["port_widgets_switch"] == 1  # the captured 0a02's a6
    device.set_values(port_widgets_switch=0)
    values = device.negotiated_module.values
    read = Parameters.parse(device.mcu.respond(0x202, values=values)[0].payload)
    assert decode_field(read, "a6", ui(1)) == 0


@pytest.mark.parametrize(
    ("active", "usb1", "fields"),
    [
        pytest.param((), 0, "8c64646400000000", id="idle"),
        pytest.param((2,), 1, "8c64646400000100", id="one port ignores usb-a"),
        pytest.param((0, 1), 0, "8c642d2d01010000", id="pd1 pd2"),
        pytest.param((1, 2), 1, "6464641e00010100", id="pd2 pd3 with usb-a"),
        pytest.param((0, 1, 2, 3), 1, "46412d2d01010101", id="all with usb-a"),
    ],
)
def test_an_a2345_usbc_status_read_reports_the_power_sharing_budget(
    active: tuple[int, ...], usb1: int, fields: str
) -> None:
    """``4218`` 0x0001daf0: budgets by the active-port bitmap (0x000114b0), statuses.

    prime-a2345-a91b2.md "4218 field5 power-sharing budget".
    """
    device = A2345()
    device.set_values(
        **{f"pd{i + 1}_output_status": int(i in active) for i in range(4)},
        usb1_output_status=usb1,
        usb2_output_status=0,
    )

    frames = device.mcu.respond(
        0x218, request=bytes.fromhex("a10121"), values=device.negotiated_module.values
    )

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == [
        (0xA18, "00a10131a208" + fields)
    ]


@pytest.mark.parametrize(
    ("clock", "reply"),
    [
        pytest.param("fe0503f022a26a", "00a10131a2020182fe0503f022a26a", id="clock"),
        pytest.param("", "00a10131a2020182", id="no clock"),
    ],
)
def test_an_a91b2_screensaver_read_reports_what_4205_wrote(
    clock: str, reply: str
) -> None:
    """``4202`` 0x08008ba4 reads the byte ``4205`` writes: switch bit 7, theme low 7."""
    device = A91B2()
    device.set_values(screen_saver_switch=True, screen_saver_theme=2)
    request = bytes.fromhex("a10121" + clock)

    frames = device.mcu.respond(
        0x202, request=request, values=device.negotiated_module.values
    )

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == [
        (0xA02, reply)
    ]


@pytest.mark.parametrize(
    ("packed", "theme"),
    [
        pytest.param("81", 1, id="theme 1 stored"),
        pytest.param("82", 2, id="last theme stored"),
        pytest.param("85", 2, id="past the last theme stored as the last"),
    ],
)
def test_an_a91b2_screensaver_theme_past_the_last_is_clamped(
    packed: str, theme: int
) -> None:
    """``4205`` ``display_byte_set`` 0x0800cb94: ``if (2 < low7) low7 = 2``."""
    device = A91B2()
    layout = device.layout
    assert layout is not None
    request = bytes.fromhex("a10121a20201" + packed)

    changes = layout.state_changes(0x205, request, device.mcu.state({}))

    assert changes == {"screen_saver_switch": True, "screen_saver_theme": theme}


@pytest.mark.parametrize("model", [A2345, A91B2])
@pytest.mark.parametrize(
    ("command", "request_fields", "echoed"),
    [
        pytest.param(
            0x208,
            "a2020101a3020101a40503010c0000fe0503f022a26a",
            "a2020101a3020101fe0503f022a26a",
            id="4208 echoes port, slot, clock",
        ),
        pytest.param(
            0x207,
            "a2020100a3020100fe0503f022a26a",
            "a2020100fe0503f022a26a",
            id="4207 echoes port, clock",
        ),
        pytest.param(
            0x209,
            "a2020100a30604012c010000fe0503f022a26a",
            "a2020100fe0503f022a26a",
            id="4209 echoes port, clock",
        ),
    ],
)
def test_a_prime_port_setter_ack_echoes_the_port(
    model: type[A2345 | A91B2], command: int, request_fields: str, echoed: str
) -> None:
    """The accepted ack carries the request's port (and slot) and clock.

    The app's 4208 and its 0a08: launches/2026-09-09_232357.919__3.23.0.log
    :1849, :1864; 4207/4209 as the captured 0a07/0a09 (docker collector.mqtt.log).
    """
    request = bytes.fromhex("a10121" + request_fields)

    frames = model().mcu.respond(command, request=request)

    assert frames[0].payload.hex() == "00a10131" + echoed


def test_a2345_screensaver_read_reports_what_the_snapshot_reports() -> None:
    """``4202`` -> ``4a02``: a2 switch/holiday/theme, a3 theme id (doc :407).

    Recorded: docker-arm collector.mqtt-093026.log.gz:37057, unit ...2385.
    """
    device = A2345()
    recorded = device.mcu.respond(0x202)[0].payload

    device.set_values(
        holiday_screensaver_switch=False, screen_saver_theme=1, screen_saver_theme_id=7
    )

    values = device.negotiated_module.values
    read = Parameters.parse(device.mcu.respond(0x202, values=values)[0].payload)
    snapshot = Parameters.parse(device.mcu.respond(0x200, values=values)[0].payload)
    theme = decode_field(read, "a2", ScreensaverRead)
    assert recorded == bytes.fromhex(data_resource("a2345.json")["a02"])
    assert (
        theme.screen_saver_switch,
        theme.holiday_screensaver_switch,
        theme.screen_saver_theme,
    ) == (True, False, 1)
    assert decode_field(read, "a3", ui(4)) == 7
    assert decode_field(snapshot, "af", Theme).screen_saver_theme_id == 7


def test_an_a91b2_port_switch_is_followed_by_its_0302_push() -> None:
    """Every captured A91B2 0a07 is followed by a 0302 with the port and its state.

    docker-arm collector.mqtt.log 2026-10-19 (:84908 0a07, :84910 0302).
    """
    request = bytes.fromhex("a10121a2020102a3020101fe0503933cd66a")

    a91b2 = A91B2().mcu.respond(0x207, request=request)

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in a91b2] == [
        (0xA07, "00a10131a2020102fe0503933cd66a"),
        (0x302, "a10131a2020102a3020101fe0503933cd66a"),
    ]


async def test_a2345_pushes_0302_only_when_a_port_switch_changes() -> None:
    """The A2345's switch watcher (``port_switch_push_0302``) sends on a change."""
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        port = [(0xA1, b"\x21"), (0xA2, b"\x01\x00")]
        on = await link.send(0x207, [*port, (0xA3, b"\x01\x01"), CLOCK], SESSION)
        again = await link.send(0x207, [*port, (0xA3, b"\x01\x01"), CLOCK], SESSION)
        usb = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x04"), (0xA3, b"\x01\x01")], SESSION
        )

    assert [(cmd_hex(reply.frame), reply.plaintext.hex()) for reply in on] == [
        ("4a07", "00a10131a2020100fe0503933cd66a"),
        ("4302", "a10131a2020100a3020101fe0503933cd66a"),
    ]
    assert [cmd_hex(reply.frame) for reply in again] == ["4a07"]
    assert usb[1].plaintext.hex() == "a10131a2020104a3020101"


@pytest.mark.parametrize(
    ("model", "client", "select", "disarmed", "kept"),
    [
        pytest.param(
            A2345,
            AppClient(),
            0,
            "00201c000000000000",
            ("aa", 0),
            id="A2345 zeroes the remaining time",
        ),
        pytest.param(
            A91B2,
            AppClient(encrypted_outer=False),
            0,
            "00201c0000201c0000",
            ("aa", 7200),
            id="A91B2 AC keeps it",
        ),
        pytest.param(
            A91B2,
            AppClient(encrypted_outer=False),
            3,
            "0000000000201c0000",
            ("b1", 0),
            id="A91B2 USB-C zeroes it and reports it first",
        ),
    ],
)
async def test_a_prime_pushes_0307_when_a_countdown_arms_or_disarms(
    model: type[A2345 | A91B2],
    client: AppClient,
    select: int,
    disarmed: str,
    kept: tuple[str, int],
) -> None:
    """Re-arming an armed countdown draws no 0307.

    A2345: launches/2026-09-09_232357.919__3.23.0.log:1651, :1731. A91B2 AC:
    docker-arm collector.mqtt.log :84461, :84881. A91B2 USB-C:
    launches/2026-09-09_210425.106__3.23.0.log:5033, :5164; re-arm silent:
    launches/2026-07-23_000219.535__3.21.1__truncated-end.log:18986.
    """
    device = model()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as ble:
        link = BleakLink(ble, client)
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        port = [(0xA1, b"\x21"), (0xA2, bytes([1, select]))]
        arm = [*port, (0xA3, b"\x04\x01\x20\x1c\x00\x00"), CLOCK]
        armed = await link.send(0x209, arm, SESSION)
        rearmed = await link.send(0x209, arm, SESSION)
        disarm = [*port, (0xA3, b"\x04\x00\x00\x00\x00\x00"), CLOCK]
        disarming = await link.send(0x209, disarm, SESSION)

    a2 = f"a20201{select:02x}"
    assert [(cmd_hex(reply.frame), reply.plaintext.hex()) for reply in armed] == [
        ("4a09", f"00a10131{a2}fe0503933cd66a"),
        ("4307", f"a10131{a2}a30a0401201c0000201c0000fe0503933cd66a"),
    ]
    assert [cmd_hex(reply.frame) for reply in rearmed] == ["4a09"]
    assert disarming[1].plaintext.hex() == f"a10131{a2}a30a04{disarmed}fe0503933cd66a"
    record, remaining = kept
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    countdown = decode_field(Parameters.parse(snapshot.payload), record, Timer)
    assert (
        countdown.countdown_task_seconds,
        countdown.countdown_task_remaining_seconds,
    ) == (7200, remaining)


async def test_an_a91b2_ac_disarm_copies_the_seconds_into_the_remaining_time() -> None:
    """The AC disarm path (0x08004f3c) restores the remaining time to the seconds."""
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as ble:
        link = BleakLink(ble, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        port = [(0xA1, b"\x21"), (0xA2, b"\x01\x00")]
        await link.send(0x209, [*port, (0xA3, b"\x04\x01\x20\x1c\x00\x00")], SESSION)
        device.set_values(ac1_countdown_task_remaining_seconds=100)
        disarm = [*port, (0xA3, b"\x04\x00\x00\x00\x00\x00"), CLOCK]
        disarming = await link.send(0x209, disarm, SESSION)

    assert disarming[1].plaintext.hex() == (
        "a10131a2020100a30a0400201c0000201c0000fe0503933cd66a"
    )
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    countdown = decode_field(Parameters.parse(snapshot.payload), "aa", Timer)
    assert countdown.countdown_task_remaining_seconds == 7200


@pytest.mark.parametrize(
    ("model", "client", "select", "record"),
    [
        pytest.param(A2345, AppClient(), 2, "ac", id="A2345 usbc3"),
        pytest.param(
            A91B2, AppClient(encrypted_outer=False), 3, "b1", id="A91B2 usbc2"
        ),
    ],
)
async def test_a_prime_timed_task_collision_disables_the_other_half_and_pushes_0308(
    model: type[A2345 | A91B2], client: AppClient, select: int, record: str
) -> None:
    """An on-time at the unset off-time's 12:00 disables it and reports it.

    launches/2026-09-09_210425.106__3.23.0.log:3392 -> :3407 (doc "MCU pushes").
    """
    device = model()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as ble:
        link = BleakLink(ble, client)
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        port = [(0xA1, b"\x21"), (0xA2, bytes([1, select]))]
        noon = await link.send(
            0x208,
            [*port, (0xA3, b"\x01\x01"), (0xA4, b"\x03\x01\x0c\x00\x00"), CLOCK],
            SESSION,
        )
        nine = await link.send(
            0x208,
            [*port, (0xA3, b"\x01\x01"), (0xA4, b"\x03\x01\x09\x00\x00"), CLOCK],
            SESSION,
        )

    assert [(cmd_hex(reply.frame), reply.plaintext.hex()) for reply in noon] == [
        ("4a08", f"00a10131a20201{select:02x}a3020101fe0503933cd66a"),
        ("4308", f"a10131a20201{select:02x}a3020100a40504000c0000fe0503933cd66a"),
    ]
    assert [cmd_hex(reply.frame) for reply in nine] == ["4a08"]
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    end_switch = decode_field(Parameters.parse(snapshot.payload), record, Timer)
    assert end_switch.timed_task_end_switch == 0


async def test_a2345_pushes_030d_on_a_mode_change_and_030f_on_a_designation() -> None:
    """430f carries the designated mask, then 430d the mode, b8 and the mask.

    launches/2026-09-08_161516.236__3.23.0.log:12376 (430f), :12481 (430d);
    leaving Port Priority clears the designations: :13935 (430d mode 1, a4 0);
    doc "Charging mode".
    """
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        designate = await link.send(
            0x20C, [(0xA1, b"\x21"), (0xA2, b"\x01\x01"), CLOCK], SESSION
        )
        again = await link.send(
            0x20C, [(0xA1, b"\x21"), (0xA2, b"\x01\x01"), CLOCK], SESSION
        )
        same_mode = await link.send(
            0x206, [(0xA1, b"\x21"), (0xA2, b"\x01\x02"), CLOCK], SESSION
        )
        ai_power = await link.send(
            0x206, [(0xA1, b"\x21"), (0xA2, b"\x01\x01"), CLOCK], SESSION
        )

    assert [(cmd_hex(reply.frame), reply.plaintext.hex()) for reply in designate] == [
        ("4a0c", "00a10131"),
        ("430f", "a10131a3020101fe0503933cd66a"),
        ("430d", "a10131a2020102a30804010046412d2d18a4020101fe0503933cd66a"),
    ]
    assert [cmd_hex(reply.frame) for reply in again] == ["4a0c"]
    assert [cmd_hex(reply.frame) for reply in same_mode] == ["4a06"]
    assert [(cmd_hex(reply.frame), reply.plaintext.hex()) for reply in ai_power] == [
        ("4a06", "00a10131"),
        ("430f", "a10131a3020100fe0503933cd66a"),
        ("430d", "a10131a2020101a30804010046412d2d18a4020100fe0503933cd66a"),
    ]
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    priorities = [
        decode_field(params, tag, Schedule).port_priority
        for tag in ("aa", "ab", "ac", "ad")
    ]
    assert priorities == [0, 0, 0, 0]


async def test_a2345_port_priority_designates_all_four_usbc_ports_at_once() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        designate = await link.send(
            0x20C, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )
        bad_mask = await link.send(
            0x20C, [(0xA1, b"\x21"), (0xA2, b"\x01\x10")], SESSION
        )

    assert designate[0].plaintext == bytes.fromhex("00a10131")
    assert bad_mask[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    priorities = [
        decode_field(params, tag, Schedule).port_priority
        for tag in ("aa", "ab", "ac", "ad")
    ]
    assert priorities == [1, 2, 1, 1]  # only usbc_2 designated


async def test_a2345_knob_time_format_screensaver_and_switches_validate() -> None:
    device = A2345()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        knob = await link.send(0x20E, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        bad_knob = await link.send(
            0x20E, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )
        time_format = await link.send(
            0x210, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        bad_time_format = await link.send(
            0x210, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )
        screensaver = await link.send(
            0x213,
            [(0xA1, b"\x21"), (0xA2, b"\x04\x08\x00\x17\x00\x7f")],
            SESSION,
        )
        compat = await link.send(0x214, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        bad_compat = await link.send(
            0x214, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )
        device_id = await link.send(
            0x223, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        bad_device_id = await link.send(
            0x223, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )

    assert knob[0].plaintext == bytes.fromhex("00a10131")
    assert bad_knob[0].plaintext == bytes.fromhex("04a10131")
    assert time_format[0].plaintext == bytes.fromhex("00a10131")
    assert bad_time_format[0].plaintext == bytes.fromhex("04a10131")
    assert screensaver[0].plaintext == bytes.fromhex("00a10131")
    assert compat[0].plaintext == bytes.fromhex("00a10131")
    assert bad_compat[0].plaintext == bytes.fromhex("04a10131")
    assert device_id[0].plaintext == bytes.fromhex("00a10131")
    assert bad_device_id[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "b4", ui(1)) == 1
    assert decode_field(params, "b5", ui(1)) == 1
    window = decode_field(params, "b9", Screensaver)
    assert [window[name] for name in SCREENSAVER_FIELDS] == [8, 0, 0x17, 0, 0x7F]
    assert decode_field(params, "bb", ui(1)) == 1
    assert decode_field(params, "bf", ui(1)) == 1


def test_a2345_layout_rejects_a_msgtype_with_no_command() -> None:
    device = A2345()

    assert device.layout is not None
    assert device.layout.check(0x999, b"") == 0x04


def test_a2345_a_named_value_reaches_both_messages_that_carry_it() -> None:
    device = A2345()

    device.set_values(pd1_output_power=42)

    pushed = device.mcu.push(0x303, values=device.negotiated_module.values)
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    pushed_port = decode_field(Parameters.parse(pushed.payload), "a2", OutputPort)
    snapshot_port = decode_field(Parameters.parse(snapshot.payload), "a4", OutputPort)
    assert pushed_port.output_power == snapshot_port.output_power == 42


def test_a2345_set_values_validates_port_schedule_single_and_scalar_fields() -> None:
    """The 0a00-only schedule/theme/screensaver/scalar fields, not just ports.

    PiField/docs/re/prime-a2345-a91b2.md "4a00 snapshot" (A2345): aa-ae
    schedules, af theme, b9 screensaver, and the plain-byte scalars.
    """
    device = A2345()

    device.set_values(
        pd1_timed_task_start_switch=1,
        pd1_timed_task_start_hour=7,
        screen_saver_switch=True,
        screen_saver_theme=2,
        screensaver_start_hour=8,
        screen_timeout=2,
        charging_mode=3,
        screen_brightness=50,
        button_rotate_direction=1,
        time_format=1,
        compatibility_switch=1,
        equipment_identification_switch=1,
    )

    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    usbc_1 = decode_field(params, "aa", Schedule)
    assert (usbc_1.timed_task_start_switch, usbc_1.timed_task_start_hour) == (1, 7)
    theme_byte = decode_field(params, "af", Theme)
    assert theme_byte.screen_saver_switch
    assert theme_byte.screen_saver_theme == 2
    assert decode_field(params, "b9", Screensaver).screensaver_start_hour == 8
    assert decode_field(params, "b0", ui(1)) == 2
    assert decode_field(params, "b1", ui(1)) == 3
    assert decode_field(params, "b3", ui(1)) == 50
    assert decode_field(params, "b4", ui(1)) == 1
    assert decode_field(params, "b5", ui(1)) == 1
    assert decode_field(params, "bb", ui(1)) == 1
    assert decode_field(params, "bf", ui(1)) == 1


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
    assert ui(2).parse(replies[0x803].fields[0xA2]) == 297
    assert [cmd_hex(reply.frame) for reply in status] == ["4a00"]
    assert status[0].plaintext == device.mcu.respond(0x200)[0].payload
    assert [reply.plaintext for reply in usb] == [bytes.fromhex("00a10131")]
    assert version == []
    pushed = link.replies[-1]
    assert cmd_hex(pushed.frame) == "4300"
    assert {pattern_hex(reply.frame) for reply in [*status, *usb, pushed]} == {"030111"}


def test_a2687_advertises_its_chip_name_but_no_manufacturer_record() -> None:
    device = A2687()

    assert device.local_name == "Charging"
    assert device.advertisement_data.manufacturer_data == {}
    assert device.module_build.session_ops == frozenset()


async def test_a110b_reports_its_module_and_answers_on_channel_11() -> None:
    device = A110B()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        replies = await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        status = await link.send(0x200, [(0xA1, b"\x21")], SESSION)
        usb_timer = await link.send(0x20A, [(0xA1, b"\x21")], SESSION)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)
        device.push(0x300)
        await settle()

    info = replies[0x829].fields
    assert info[0xA2] == b"Charging"
    assert info[0xA3] == b"v0.0.5.1"
    assert info[0xA5] == device.mac + b"00000000001"
    assert ui(2).parse(replies[0x803].fields[0xA2]) == 297
    assert [cmd_hex(reply.frame) for reply in status] == ["4a00"]
    assert status[0].plaintext == device.mcu.respond(0x200)[0].payload
    assert usb_timer == []
    assert version == []
    pushed = link.replies[-1]
    assert cmd_hex(pushed.frame) == "4300"
    assert pushed.plaintext == device.mcu.push(0x300).payload
    assert {pattern_hex(reply.frame) for reply in [*status, pushed]} == {"030111"}


async def test_a25x7_pushes_its_recorded_pads_and_leaves_the_subscribe_unanswered() -> (
    None
):
    device = A25X7()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        subscribe = await link.send(0x200, [(0xA1, b"\x21")], SESSION)
        device.push(0x300)
        await settle()

    assert subscribe == []
    pushed = link.replies[-1]
    assert cmd_hex(pushed.frame) == "4300"
    assert pushed.plaintext == device.mcu.push(0x300).payload
    assert device.local_name is None
    assert device.advertisement_data.manufacturer_data == {}


def test_a110b_advertises_no_record() -> None:
    device = A110B()

    assert device.local_name is None
    assert device.advertisement_data.manufacturer_data == {}
    assert device.module_build.session_ops == frozenset()


async def test_an_unnamed_device_reports_its_address_as_its_name() -> None:
    backend = EmulatedBleakBackend(A110B().ble_device, timeout=10)

    assert backend.name == "AA-12-DE-AD-BE-EF"
