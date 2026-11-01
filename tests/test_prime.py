# Copyright (c) 2026 Shawn Stricker
"""The Prime devices (A110B, A2345, A25X7, A2687, A91B2) over bleak."""

from collections.abc import Callable
from datetime import UTC, datetime

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
from anker_ble_emulator.devices.a91b2 import A91B2Layout, Display, Timer
from anker_ble_emulator.devices.a91b2 import ClockTheme as A91B2ClockTheme
from anker_ble_emulator.devices.a2345 import (
    DISPLAY,
    DISPLAY_TICK,
    SCHEDULE_FROM,
    SCREEN_REQUEST,
    SCREENSAVER_FIELDS,
    SCREENSAVER_INHIBIT,
    WINDOW_END_DATE,
    WINDOW_PHASE,
    WINDOW_START_DATE,
    WINDOW_START_DONE,
    A2345Layout,
    Schedule,
    Screensaver,
    ScreensaverRead,
    Theme,
    stored_bytes,
)
from anker_ble_emulator.devices.base import data_resource
from anker_ble_emulator.devices.prime import OutputPort
from anker_ble_emulator.layouts import Value, ui
from anker_ble_emulator.parameters import Parameter, Parameters, decode_field
from tests.fixtures.client import (
    SESSION,
    TIMESTAMP,
    TZ,
    UTC_OFFSET,
    AppClient,
    BleakLink,
    cmd_hex,
    exchange,
    pattern_hex,
    settle,
)
from tests.fixtures.prime import (
    CUSTOM_PHOTO,
    CUSTOM_PHOTO_2,
    HOLIDAY_URL,
    PRIME_EPOCH,
    SCREENSAVER_AFTER,
    advance,
    arm,
    baseline_jpeg,
    command,
    device_clock,
    holiday_schedule,
    into_screensaver,
    local_datetime,
    prime_client,
    published,
    pushed_by,
    screensaver,
    screensaver_window,
    snapshot,
    theme_select,
    unit_change,
    upload_chunk,
    upload_prepare,
    upload_theme,
    usbc_identity,
)


TOKEN = b"owner-token"
ACCOUNT = b"a" * 40
#: Past the 5th 100 ms tick after a change, when an A2345 report goes out.
REPORT_WAIT = 0.6
DAY_SECONDS = 86400
#: Past the A2345 window's first evaluation (the 10th 150 ms pass, 1.5 s), and
#: one pass on.
WINDOW_FIRST_WAIT = 1.6
PASS_WAIT = 0.15
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
    device = A91B2(clock=ManualClock())
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        snapshot = await link.send(0x200, [(0xA1, b"\x21")], SESSION)
        keepalive = await link.send(0x20B, [(0xA1, b"\x21")], SESSION)

    module = device.negotiated_module
    replayed = module.mcu.respond(0x200, epoch=module.epoch)[0]
    assert [cmd_hex(reply.frame) for reply in snapshot] == ["4a00"]
    assert snapshot[0].plaintext == replayed.payload
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
    device = A2345(clock=ManualClock())
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
    module = device.negotiated_module
    replayed = module.mcu.respond(0x200, epoch=module.epoch)[0]
    assert snapshot[0].plaintext == replayed.payload
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
        await settle()  # its 430a screen report
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
    # The settings are gone; the schedule's start (RAM) lands after, and the
    # window's first evaluation waits for the 10th pass.
    assert set(device.negotiated_module.values) == {SCHEDULE_FROM}
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
    device = A91B2(clock=ManualClock())
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        ac = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x00"), (0xA3, b"\x01\x00")], SESSION
        )
        usbc = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x02"), (0xA3, b"\x01\x00")], SESSION
        )
        out_of_range = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x06"), (0xA3, b"\x01\x01")], SESSION
        )
        bad_switch = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x00"), (0xA3, b"\x01\x02")], SESSION
        )

    assert [cmd_hex(reply.frame) for reply in ac] == ["4a07"]
    assert ac[0].plaintext.hex() == "00a10131a2020100" + device_clock(0)
    assert [cmd_hex(reply.frame) for reply in out_of_range] == ["4a07"]
    assert usbc[0].plaintext.hex() == "00a10131a2020102" + device_clock(0)
    assert out_of_range[0].plaintext == bytes.fromhex("04a10131")
    assert bad_switch[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    # The switch is each port's record [0] (ac_1 aa, usbc_1 b0), not its block's
    # PD attach flag.
    assert not decode_field(params, "aa", Timer).relay_switch
    assert not decode_field(params, "b0", Timer).relay_switch
    assert decode_field(params, "ab", Timer).relay_switch


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
    assert Parameter.build(params["ac"]) == bytes.fromhex("ac020185")
    assert Parameter.build(params["ad"]) == bytes.fromhex("ad0201a0")
    assert Parameter.build(params["ae"]) == bytes.fromhex("ae03040100")
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
    ad = Parameters.parse(snapshot.payload)["ad"]
    assert Parameter.build(ad) == bytes.fromhex("ad020124")


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
    device = A91B2(clock=ManualClock())
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

    assert disarm[0].plaintext.hex() == "00a10131a2020100" + device_clock(0)
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    timer = decode_field(params, "aa", Timer)
    assert not timer.countdown_task_switch
    assert timer.countdown_task_seconds == 10800


async def test_a91b2_countdown_arms_with_its_seconds_and_rejects_a_bad_port() -> None:
    device = A91B2(clock=ManualClock())
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

    assert armed[0].plaintext.hex() == "00a10131a2020100" + device_clock(0)
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
    device = A91B2(clock=ManualClock())
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

    assert on_time[0].plaintext.hex() == "00a10131a2020102a3020101" + device_clock(0)
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
            0x204, [(0xA1, b"\x21"), (0xA2, b"\x01\x5a")], SESSION
        )
        theme = await link.send(0x205, [(0xA1, b"\x21"), (0xA2, b"\x01\x82")], SESSION)

    assert timeout[0].plaintext == bytes.fromhex("00a10131")
    # Acked 00; screen_timeout_set's range check ignores it (cmd203 0x00043ec0).
    assert bad_timeout[0].plaintext == bytes.fromhex("00a10131")
    assert brightness[0].plaintext == bytes.fromhex("00a10131")
    assert theme[0].plaintext == bytes.fromhex("00a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "b0", ui(1)) == 4
    assert decode_field(params, "b3", ui(1)) == 0x5A
    theme_byte = decode_field(params, "af", Theme)
    assert theme_byte.screen_saver_switch
    assert theme_byte.screen_saver_theme == 2


async def test_a2345_screen_saver_theme_id_clock_off_and_custom_mode_switch() -> None:
    """``4205`` network ``83`` with ``a3`` alone, clock off, and ``4211``.

    The network ``4205`` lacks its CRC and URL, so it's acked and dropped, and
    the recorded theme (3, ``496957652``) stays; clock off keeps the theme.
    PiField/docs/re/prime-a2345-a91b2.md "4205", "Theme upload" and "4211".
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

    assert [cmd_hex(reply.frame) for reply in network] == ["4a05"]
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
    device = A2345(clock=ManualClock())
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        switch = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x04"), (0xA3, b"\x01\x00")], SESSION
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

    clock = device_clock(0)
    assert switch[0].plaintext.hex() == "00a10131a2020104" + clock
    # Acked and ignored: port_switch_set stores only index <= 4 (doc 4207 row).
    assert bad_port[0].plaintext.hex() == "00a10131a2020105" + clock
    assert on_time[0].plaintext.hex() == "00a10131a2020100a3020101" + clock
    assert bad_schedule_port[0].plaintext == bytes.fromhex("04a10131")
    # Acked and dropped: cmd208's setter takes only a3 0 / 1.
    assert bad_slot[0].plaintext.hex() == "00a10131a2020100a3020102" + clock
    assert countdown[0].plaintext.hex() == "00a10131a2020100" + clock
    assert disarm[0].plaintext.hex() == "00a10131a2020101" + clock
    # Acked with the port: countdown_set? drops a port past 4 (doc 0307 row).
    assert bad_countdown_port[0].plaintext.hex() == "00a10131a2020105" + clock
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert not decode_field(params, "ae", Schedule).relay_switch  # both USB-A
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
            [(0xA0D, "00a10131")],
            id="420d nonzero acks; its 0305 is queued",
        ),
        pytest.param(0x20D, "a2020200", [(0xA0D, "00a10131")], id="420d zero acks"),
        pytest.param(
            0x20D, "a203020001", [(0xA0D, "04a10131")], id="420d a2 length 3 refused"
        ),
        pytest.param(
            0x20D, "a2020101", [(0xA0D, "00a10131")], id="420d a2 typed 01 acks"
        ),
        pytest.param(0x20D, "", [(0xA0D, "04a10131")], id="420d without a2 refused"),
        pytest.param(
            0x203, "a2020105", [(0xA03, "00a10131")], id="4203 out of range acks"
        ),
        pytest.param(0x203, "a20105", [(0xA03, "04a10131")], id="4203 a2 length 1"),
        pytest.param(0x203, "a203010001", [(0xA03, "04a10131")], id="4203 a2 length 3"),
        pytest.param(0x203, "", [(0xA03, "04a10131")], id="4203 without a2 refused"),
        pytest.param(
            0x204, "a2020265", [(0xA04, "00a10131")], id="4204 typed 02, over 100"
        ),
        pytest.param(0x204, "a203010001", [(0xA04, "04a10131")], id="4204 a2 length 3"),
        pytest.param(0x204, "", [(0xA04, "04a10131")], id="4204 without a2 refused"),
        pytest.param(
            0x20E, "a2020102", [(0xA0E, "00a10131")], id="420e out of range acks"
        ),
        pytest.param(0x20E, "a203010001", [(0xA0E, "04a10131")], id="420e a2 length 3"),
        pytest.param(0x20E, "", [(0xA0E, "04a10131")], id="420e without a2 refused"),
        pytest.param(0x222, "a2020101", [], id="4222 set silently"),
        pytest.param(0x222, "a2020105", [], id="4222 other value silently"),
        pytest.param(0x222, "", [(0xA22, "04a10131")], id="4222 without a2 refused"),
    ],
)
def test_an_a2345_handler_answers_by_its_firmware_branch(
    command: int, request_fields: str, expected: list[tuple[int, str]]
) -> None:
    """``420d`` 0x00043938 (its ``0305`` 0x0001475c) and ``4222`` 0x0004ff84.

    prime-a2345-a91b2.md "Commands" and "Source-byte order": ``4203`` / ``4204``
    / ``420e`` / ``420d`` answer ``04`` only for an ``a2`` missing or of TLV
    length other than 2, any type byte; the setters range-check after the ack.
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

    assert layout.state_changes(0x222, clear, state) == {"time_display": 0}
    assert layout.state_changes(0x222, bytes.fromhex("a10121a2020105"), state) == {}
    assert state["time_display"] == 1  # the captured 0a02's a6
    device.set_values(time_display=0)
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
    "clock",
    [
        pytest.param("fe0503e822a26a", id="request clock"),
        pytest.param("", id="no request clock"),
    ],
)
def test_an_a91b2_screensaver_read_reports_what_4205_wrote(clock: str) -> None:
    """``4202`` 0x08008ba4 reads the byte ``4205`` writes: switch bit 7, theme low 7.

    Then the unit's clock, whatever the request carried, as ``0a02`` does.
    """
    device = A91B2()
    device.set_values(screen_saver_switch=True, screen_saver_theme=2)
    request = bytes.fromhex("a10121" + clock)

    frames = device.mcu.respond(
        0x202, request=request, values=device.negotiated_module.values, epoch=0x6AA222E6
    )

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == [
        (0xA02, "00a10131a2020182fe0503e622a26a")
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
            "a2020101a3020101a40503010c0000fe0503e822a26a",
            "a2020101a3020101",
            id="4208 echoes port, slot",
        ),
        pytest.param(
            0x207,
            "a2020100a3020100fe0503e822a26a",
            "a2020100",
            id="4207 echoes port",
        ),
        pytest.param(
            0x209,
            "a2020100a30604012c010000fe0503e822a26a",
            "a2020100",
            id="4209 echoes port",
        ),
    ],
)
def test_a_prime_port_setter_ack_carries_the_port_and_the_unit_clock(
    model: type[A2345 | A91B2], command: int, request_fields: str, echoed: str
) -> None:
    """The accepted ack carries the request's port (and slot), then the unit's clock.

    prime-a2345-a91b2.md's ``4207`` / ``4208`` / ``4209`` rows; the clock is the
    unit's, not the request's, as the ``0a07`` and ``0a09`` frames show.
    """
    request = bytes.fromhex("a10121" + request_fields)

    frames = model().mcu.respond(command, request=request, epoch=0x6AA222E6)

    assert frames[0].payload.hex() == "00a10131" + echoed + "fe0503e622a26a"


@pytest.mark.parametrize(
    ("msgtype", "fields", "frames"),
    [
        pytest.param(
            0x209,
            [(0xA2, b"\x01\x01"), (0xA3, bytes.fromhex("0401201c0000"))],
            [
                ("4a09", "00a10131a2020101"),
                ("4307", "a10131a2020101a30a0401201c0000201c0000"),
            ],
            id="4209 ack, then 0307, 2026-09-09 23:24:24",
        ),
        pytest.param(
            0x207,
            [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x01")],
            [("4a07", "00a10131a2020100")],
            id="4207 ack, 2026-09-04 20:15:23",
        ),
        pytest.param(
            0x208,
            [(0xA2, b"\x01\x01"), (0xA3, b"\x01\x00"), (0xA4, b"\x03\x00\x0c\x00\x00")],
            [("4a08", "00a10131a2020101a3020100")],
            id="4208 ack, 2026-09-09 23:24:50",
        ),
    ],
)
def test_an_a2345_setter_stamps_its_own_clock_replays_the_app_log(
    msgtype: int, fields: list[tuple[int, bytes]], frames: list[tuple[str, str]]
) -> None:
    """The ack and its pushes carry the unit's clock, not the request's.

    prime-a2345-a91b2.md "Commands", "Reply ``a1``" and the ``4209`` row: over
    MQTT ``0209`` at ``e822a26a`` drew ``0a09`` and, on the next 150 ms pass,
    ``0307`` at the unit's ``e622a26a``; ``0207`` at ``1b5f9b6a`` its ``0a07``
    at ``1a5f9b6a``; the ``0208`` at ``0223a26a`` its ``0a08`` at ``0123a26a``.
    Here the request runs 2 s ahead of the unit.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    ahead = (0xFE, b"\x03" + ui(4).build(PRIME_EPOCH + 2))
    advance(device, client, clock, 0.05)

    answered = command(device, client, msgtype, [*fields, ahead])
    answered += advance(device, client, clock, 0.15)

    assert answered == [(cmd, body + device_clock(0)) for cmd, body in frames]


def test_a2345_screensaver_read_reports_what_the_snapshot_reports() -> None:
    """``4202`` -> ``4a02``: a2 switch/holiday/theme, a3 theme id.

    prime-a2345-a91b2.md's ``4202`` row and the ``4a02`` fields below its table.
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


@pytest.mark.parametrize(
    ("select", "early", "due"),
    [
        pytest.param(0, 0.04, 0.02, id="ac_1: the next 100 ms pass"),
        pytest.param(2, 0.44, 0.02, id="usbc_1: 4 passes after the marking pass"),
    ],
)
def test_an_a91b2_4207_draws_its_0302_on_the_port_kind_s_pass(
    select: int, early: float, due: float
) -> None:
    """The recorded app_log pairs (2026-09-09): AC with the ack, USB-C 0.36 s on.

    prime-a2345-a91b2.md A91B2 ``0302`` row and "``0302`` recorded timing": the
    period-100 tasks see the change on their next pass (0.1 s for a ``4207``
    at 0.05 s). ``ac_switch_push_0302?`` sends on it; ``usbc_switch_push_0302?``
    marks it there and sends on the 5th call counting that one, 0.5 s.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    advance(device, client, clock, 0.05)
    port = (0xA2, bytes([1, select]))

    replies = command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])
    waiting = advance(device, client, clock, early)
    sent = advance(device, client, clock, due)

    assert [cmd for cmd, _ in replies] == ["4a07"]
    assert waiting == []
    assert sent == [("4302", f"a10131a20201{select:02x}a3020100{device_clock(0)}")]


def test_a2345_an_app_port_switch_draws_no_0302() -> None:
    """``cmd207`` writes ``0302``'s source byte 1, so its change is dropped.

    prime-a2345-a91b2.md push table ``0302``: no A2345 ``0302`` is recorded,
    the app's ``4207``s draw none.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")

    off = command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])
    after_off = advance(device, client, clock, 1)
    usb = command(device, client, 0x207, [(0xA2, b"\x01\x04"), (0xA3, b"\x01\x00")])
    after_usb = advance(device, client, clock, 1)

    assert off == [("4a07", "00a10131a2020100" + device_clock(0))]
    assert [cmd for cmd, _ in usb] == ["4a07"]
    assert after_off == after_usb == []
    assert not decode_field(snapshot(device), "aa", Timer).relay_switch


def test_a2345_two_app_switches_in_one_0302_pass_report_the_second() -> None:
    """The byte's 1 drops only the pass's first pending port; the next one sends.

    prime-a2345-a91b2.md push table ``0302``: ``port_switch_push_0302``.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    command(device, client, 0x207, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x00")])
    command(device, client, 0x207, [(0xA2, b"\x01\x04"), (0xA3, b"\x01\x00")])
    sent = advance(device, client, clock, REPORT_WAIT + DISPLAY_TICK)

    assert sent == [("4302", "a10131a2020104a3020100" + device_clock(0))]


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        pytest.param([(0xA2, b"\x01\x05"), (0xA3, b"\x01\x00")], "00", id="port 5"),
        pytest.param([(0xA2, b"\x01\x00"), (0xA3, b"\x01\x02")], "00", id="state 2"),
        pytest.param(
            [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x00\x00")], "04", id="a3 length 3"
        ),
        pytest.param([(0xA2, b"\x01\x00")], "04", id="no a3"),
    ],
)
def test_a2345_4207_acks_a_dropped_change_and_refuses_a_bad_tlv(
    fields: list[tuple[int, bytes]], status: str
) -> None:
    """``cmd207``: ``04`` unless ``a2`` / ``a3`` are type + 1 byte.

    ``port_switch_set`` stores only index <= 4 and state <= 1, else acked and
    ignored (prime-a2345-a91b2.md ``4207`` row).
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    replies = command(device, client, 0x207, fields)

    assert [(cmd, payload[:2]) for cmd, payload in replies] == [("4a07", status)]
    assert decode_field(snapshot(device), "aa", Timer).relay_switch


#: Mode 5's profile: ``a3`` the 7-byte ``b8``, ``a4`` 12 bytes of port records.
PROFILE_A3 = (0xA3, b"\x04" + bytes(7))
PROFILE_A4 = (0xA4, b"\x04" + bytes(12))


@pytest.mark.parametrize(
    ("fields", "mode"),
    [
        pytest.param([(0xA2, b"\x01\x05")], 1, id="no a3 a4"),
        pytest.param(
            [(0xA2, b"\x01\x05"), PROFILE_A3, (0xA4, b"\x04" + bytes(11))],
            1,
            id="short a4",
        ),
        pytest.param([(0xA2, b"\x01\x05"), PROFILE_A3, PROFILE_A4], 5, id="whole"),
    ],
)
def test_a2345_4206_mode_5_needs_its_profile(
    fields: list[tuple[int, bytes]], mode: int
) -> None:
    """Mode 5 needs ``a3`` (7 B) and ``a4`` (12 B); without them it's acked, dropped."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    device.set_values(charging_mode=1)

    replies = command(device, client, 0x206, fields)

    assert [(cmd, payload[:2]) for cmd, payload in replies] == [("4a06", "00")]
    assert decode_field(snapshot(device), "b1", ui(1)) == mode


def test_a2345_4208_a_slot_other_than_0_or_1_is_acked_and_dropped() -> None:
    """``cmd208``: an ``a3`` other than 0 / 1 is acked ``00``; nothing is written."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    before = decode_field(snapshot(device), "aa", Timer)

    replies = command(
        device, client, 0x208, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x02"), SUNDAY_2326]
    )

    assert [(cmd, payload[:2]) for cmd, payload in replies] == [("4a08", "00")]
    assert decode_field(snapshot(device), "aa", Timer) == before


def test_a2345_an_app_port_switch_inside_the_debounce_drops_a_timer_0302() -> None:
    """One source byte: ``cmd207``'s 1 drops the pending expiry ``0302`` at send.

    prime-a2345-a91b2.md push table ``0302``, "as with the screen reports".
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(1)])
    expired = advance(device, client, clock, 1)

    advance(device, client, clock, 0.2)
    command(device, client, 0x207, [(0xA2, b"\x01\x01"), (0xA3, b"\x01\x01")])
    later = advance(device, client, clock, 1)

    assert [cmd for cmd, _ in expired] == ["4307"]
    assert later == []


def test_a2345_one_0302_pass_drops_only_the_first_pending_port() -> None:
    """``port_switch_push_0302``: one byte, one pass over the ports in order.

    pd1 and pd2 expire 0.1 s apart (byte 2), a ``4207`` writes 1 before the
    pass; the pass drops pd1's (resetting the byte to 0) and sends pd2's.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(1)])
    advance(device, client, clock, 0.1)
    command(device, client, 0x209, [(0xA2, b"\x01\x01"), arm(1)])
    advance(device, client, clock, 1)

    command(device, client, 0x207, [(0xA2, b"\x01\x02"), (0xA3, b"\x01\x01")])
    sent = advance(device, client, clock, 1)

    assert [(cmd, payload[:22]) for cmd, payload in sent if cmd == "4302"] == [
        ("4302", "a10131a2020101a3020100")
    ]


def test_a2345_a_firmware_change_after_a_4207_has_both_reported() -> None:
    """The last writer wins: an expiry's 2 after a ``4207``'s 1 sends both ports."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    command(device, client, 0x209, [(0xA2, b"\x01\x01"), arm(1)])
    advance(device, client, clock, 0.8)

    command(device, client, 0x207, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x00")])
    sent = advance(device, client, clock, 1)

    assert [payload[:22] for cmd, payload in sent if cmd == "4302"] == [
        "a10131a2020100a3020100",
        "a10131a2020101a3020100",
    ]


def test_a2345_each_port_reports_0302_after_its_own_last_change() -> None:
    """``port_switch_change_mark?``: a change restarts only its own port's count.

    prime-a2345-a91b2.md "``0302`` timing": pd1 on at 0 s and pd2 at 0.3 s
    report at ~0.5 s and ~0.8 s.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_relay_switch=False, pd2_relay_switch=False)
    advance(device, client, clock, 1)

    device.set_values(pd1_relay_switch=True)
    advance(device, client, clock, 0.3)
    device.set_values(pd2_relay_switch=True)
    first = advance(device, client, clock, 0.25)
    second = advance(device, client, clock, 0.3)

    assert [payload[:22] for _, payload in first] == ["a10131a2020100a3020101"]
    assert [payload[:22] for _, payload in second] == ["a10131a2020101a3020101"]


def test_a2345_ports_due_on_one_tick_go_in_port_order() -> None:
    """``port_switch_tick`` on ``tick_100ms_group``: one tick's pass walks 0-4.

    pd3 marked at 1.42 s and pd1 at 1.45 s both count from the 1.5 s tick and
    are due on the 1.9 s one; a ``4207``'s 1 drops the walk's first, pd1, and
    pd3 sends.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_relay_switch=False, pd3_relay_switch=False)
    advance(device, client, clock, 1.42)

    device.set_values(pd3_relay_switch=True)
    advance(device, client, clock, 0.03)
    device.set_values(pd1_relay_switch=True)
    advance(device, client, clock, 0.05)
    command(device, client, 0x207, [(0xA2, b"\x01\x01"), (0xA3, b"\x01\x01")])
    early = advance(device, client, clock, 0.35)
    due = advance(device, client, clock, 0.1)

    assert early == []
    assert [payload[:22] for _, payload in due] == ["a10131a2020102a3020101"]


def test_a2345_the_systick_phase_shifts_the_report_grid() -> None:
    """The hidden ``systick_phase`` (0-3 s) places the SysTick groups' grids.

    At 0.05 a 1.0 s tap counts from the 1.05 s tick and sends on the 1.45 s one.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_relay_switch=False, systick_phase=0.05)
    advance(device, client, clock, 1)

    device.set_values(pd1_relay_switch=True)
    early = advance(device, client, clock, 0.42)
    due = advance(device, client, clock, 0.05)

    assert early == []
    assert [cmd for cmd, _ in due] == ["4302"]
    with pytest.raises(TypeError, match=r"systick_phase is 0 to 3\.0 s"):
        device.set_values(systick_phase=3.0)


def test_a2345_the_systick_phase_shifts_the_countdown_grids() -> None:
    """At 0.5 the 1 s steps fall on 1.5 and 2.5 s, the passes on 0.5 + 0.15 k.

    A 2 s ``4209`` at 0.6 s steps to 0 at 2.5 s; the 2.6 s pass expires it.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(systick_phase=0.5)
    advance(device, client, clock, 0.6)

    command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(2)])
    early = advance(device, client, clock, 1.95)
    expired = advance(device, client, clock, 0.1)

    assert [cmd for cmd, _ in early] == ["4307"]
    assert [cmd for cmd, _ in expired] == ["4307"]


def test_a2345_a_change_between_ticks_reports_on_the_5th_tick_after_it() -> None:
    """A mark writes count 0; the next tick counts 1, and the 5th sends.

    prime-a2345-a91b2.md "Report timing": a tap at 1.03 s sends on the 1.5 s
    tick, 0.47 s on.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_relay_switch=False)
    advance(device, client, clock, 1.03)

    device.set_values(pd1_relay_switch=True)
    early = advance(device, client, clock, 0.45)
    due = advance(device, client, clock, 0.04)

    assert early == []
    assert [cmd for cmd, _ in due] == ["4302"]


def test_a2345_a_countdown_steps_on_the_boot_1_s_grid_and_expires_on_a_pass() -> None:
    """prime-a2345-a91b2.md "Countdown timing", on the boot-phased grids.

    ``4209`` (3 s) at 0.4 s: its arm ``0307`` on the 0.45 s pass; ``remaining``
    steps on the 1, 2 and 3 s boundaries; the 3.0 s pass sees 0, switches the
    port off and sends the disarm ``0307``; the ``0302`` counts from the 3.1 s
    tick and sends on the 3.5 s one.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    advance(device, client, clock, 0.4)

    def remaining() -> int:
        timer = decode_field(snapshot(device), "aa", Timer)
        return int(timer.countdown_task_remaining_seconds)

    replies = command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(3)])
    armed = advance(device, client, clock, 0.1)
    steps = [remaining()]
    for at in (0.6, 1.0):
        advance(device, client, clock, at)
        steps.append(remaining())
    before_pass = advance(device, client, clock, 0.85)
    expired = advance(device, client, clock, 0.06)
    switched = advance(device, client, clock, 0.6)

    assert [cmd for cmd, _ in replies] == ["4a09"]
    assert [cmd for cmd, _ in armed] == ["4307"]
    assert steps == [3, 2, 1]
    assert before_pass == []
    assert [cmd for cmd, _ in expired] == ["4307"]
    assert [cmd for cmd, _ in switched] == ["4302"]
    assert not decode_field(snapshot(device), "aa", Timer).relay_switch


def test_a2345_a_countdown_armed_on_an_off_port_is_disarmed_with_no_0307() -> None:
    """The next 150 ms pass disarms it; it never showed armed, so no ``0307``."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_relay_switch=False)

    command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(600)])
    later = advance(device, client, clock, 1)

    timer = decode_field(snapshot(device), "aa", Timer)
    assert later == []
    assert not timer.countdown_task_switch


def test_a2345_a_countdown_on_an_off_port_writes_no_source_byte() -> None:
    """``countdown_tick_push_0307`` writes 2 only when it finds the port on.

    prime-a2345-a91b2.md "Schedule timing": so an off port's disarm leaves an
    app ``4207``'s 1, and that ``4207``'s ``0302`` is still dropped.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    advance(device, client, clock, 0.05)

    command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])
    command(device, client, 0x209, [port, arm(600)])
    later = advance(device, client, clock, 1)

    assert later == []
    assert not decode_field(snapshot(device), "aa", Timer).countdown_task_switch


def test_a2345_the_systick_groups_first_fire_one_period_after_boot() -> None:
    """Nothing fires at boot: 100 ms at 0.1 s, the 150 ms pass at 0.15 s, 1 s at 1 s.

    prime-a2345-a91b2.md "Scheduler groups": a ``4209`` (1 s) at boot shows
    armed on the 0.15 s pass, steps to 0 at 1 s, expires on the 1.05 s pass,
    and its ``0302`` counts from the 1.1 s tick and sends on the 1.5 s one.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    replies = command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(1)])
    timeline = [
        [cmd for cmd, _ in advance(device, client, clock, step)]
        for step in (0.14, 0.02, 0.88, 0.02, 0.43, 0.02)
    ]

    assert [cmd for cmd, _ in replies] == ["4a09"]
    assert timeline == [[], ["4307"], [], ["4307"], [], ["4302"]]


def test_a2345_the_schedule_starts_10_passes_after_the_rtc_is_valid() -> None:
    """``schedule_tick_push_0308`` runs from the 10th 150 ms pass with the RTC valid.

    prime-a2345-a91b2.md "Schedule timing" (``DAT_2000af52`` counts before its
    ``> 9`` test): valid from boot, the 10th pass is at 1.5 s. An off-time in
    the current minute (Sunday 23:25) fires on it.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    sunday_2325 = (0xA4, b"\x03\x01\x17\x19\x40")
    command(
        device, client, 0x208, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x00"), sunday_2325]
    )

    advance(device, client, clock, 1.49)
    waiting = decode_field(snapshot(device), "aa", Timer).relay_switch
    advance(device, client, clock, 0.02)

    assert waiting
    assert not decode_field(snapshot(device), "aa", Timer).relay_switch


def test_a2345_a_schedule_fire_on_a_port_already_so_releases_a_4207s_0302() -> None:
    """A fire writes source 2 before ``port_switch_set``, even with nothing to change.

    prime-a2345-a91b2.md "Schedule timing": the pd1 on-time (on already) fires
    on the 1.5 s pass, so pd2's app ``4207`` at 1.3 s sends its ``0302`` at 1.8 s.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    on_time = [
        (0xA2, b"\x01\x00"),
        (0xA3, b"\x01\x01"),
        (0xA4, b"\x03\x01\x17\x19\x40"),
    ]
    command(device, client, 0x208, on_time)
    advance(device, client, clock, 1.3)

    command(device, client, 0x207, [(0xA2, b"\x01\x01"), (0xA3, b"\x01\x00")])
    sent = advance(device, client, clock, 0.6)

    assert [payload[:22] for _, payload in sent] == ["a10131a2020101a3020100"]


def test_a2345_a_repeating_slot_fires_once_in_its_minute() -> None:
    """Latched until the minute stops matching: switched back on, it stays on."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(device, client, 0x208, [port, (0xA3, b"\x01\x00"), SUNDAY_2326])
    advance(device, client, clock, 45.1)
    fired_off = not decode_field(snapshot(device), "aa", Timer).relay_switch

    command(device, client, 0x207, [port, (0xA3, b"\x01\x01")])
    advance(device, client, clock, 30)

    assert fired_off
    assert decode_field(snapshot(device), "aa", Timer).relay_switch


@pytest.mark.parametrize("model", [A2345, A91B2])
def test_a_prime_once_slot_passed_by_a_clock_set_is_disabled_unfired(
    model: type[A2345 | A91B2],
) -> None:
    """A once slot fires only at its exact date and minute; passed, it's disabled.

    prime-a2345-a91b2.md "Schedule timing" and the A91B2 "Schedule firing":
    disabled with a ``0308`` en 00, the port (0 on both) left as it was.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    once_2326 = (0xA4, b"\x03\x01\x17\x1a\x00")
    advance(device, client, clock, 2)
    command(device, client, 0x208, [port, (0xA3, b"\x01\x00"), once_2326])

    moved = ui(4).build(ui(4).parse(TIMESTAMP) + 2 + 120)
    exchange(
        device.negotiated_module,
        client,
        0x022,
        [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)],
    )
    passed = advance(device, client, clock, 1)

    timer = decode_field(snapshot(device), "aa", Timer)
    assert [cmd for cmd, _ in passed] == ["4308"]
    assert timer.relay_switch
    assert timer.timed_task_end_switch == 0


def test_a2345_a_once_slot_set_on_the_unit_is_dated_by_the_same_rule() -> None:
    """A ``set_values`` once 23:26 at Sunday 23:25:17 is dated today and fires."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    advance(device, client, clock, 2)

    device.set_values(
        pd1_timed_task_end_switch=1,
        pd1_timed_task_end_hour=23,
        pd1_timed_task_end_minute=26,
        pd1_timed_task_end_weekdays=0,
    )
    fired = advance(device, client, clock, 44)

    assert [cmd for cmd, _ in fired] == ["4308", "4302"]
    assert not decode_field(snapshot(device), "aa", Timer).relay_switch


@pytest.mark.parametrize(
    ("model", "pushes"),
    [
        pytest.param(A2345, ["4308", "4302"], id="A2345"),
        pytest.param(A91B2, ["4308", "4302"], id="A91B2"),
    ],
)
def test_a_prime_once_slot_at_the_current_minute_is_dated_tomorrow(
    model: type[A2345 | A91B2], pushes: list[str]
) -> None:
    """The slot set dates a once slot today only if its hh:mm is strictly later.

    prime-a2345-a91b2.md "Slot record" and the A91B2 ``4208`` row: a once 23:25
    written at Sunday 23:25:15 is dated Monday, so it fires a day on, then
    ``0308`` en 00.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    once_2325 = (0xA4, b"\x03\x01\x17\x19\x00")
    command(
        device, client, 0x208, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x00"), once_2325]
    )

    advance(device, client, clock, 2)
    waiting = decode_field(snapshot(device), "aa", Timer).relay_switch
    fired = advance(device, client, clock, DAY_SECONDS)

    assert waiting
    assert [cmd for cmd, _ in fired] == pushes
    assert not decode_field(snapshot(device), "aa", Timer).relay_switch


@pytest.mark.parametrize(
    ("rewrite", "fired"),
    [
        pytest.param(False, False, id="a clock set back leaves its date"),
        pytest.param(True, True, id="a 4208 rewrite dates it again"),
    ],
)
@pytest.mark.parametrize("model", [A2345, A91B2])
def test_a_prime_once_slot_date_is_set_only_by_a_4208(
    model: type[A2345 | A91B2], *, rewrite: bool, fired: bool
) -> None:
    """Nothing but the slot set dates a once slot; the tick judges that date.

    prime-a2345-a91b2.md "Slot record" and the A91B2 ``4208`` row: a once
    23:26 written Sunday 23:25 waits for Sunday after the clock goes back a
    day; the same slot written again on Saturday 23:25 is dated Saturday and
    fires at Saturday 23:26.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    once = [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x00"), (0xA4, b"\x03\x01\x17\x1a\x00")]
    command(device, client, 0x208, once)

    moved = ui(4).build(ui(4).parse(TIMESTAMP) - DAY_SECONDS)
    fields = [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)]
    exchange(device.negotiated_module, client, 0x022, fields)
    if rewrite:
        command(device, client, 0x208, once)
    advance(device, client, clock, 50)

    assert decode_field(snapshot(device), "aa", Timer).relay_switch is not fired


@pytest.mark.parametrize(
    ("first", "second", "back", "collides"),
    [
        pytest.param(
            b"\x03\x01\x17\x1e\x40",
            b"\x03\x01\x17\x1e\x00",
            False,
            True,
            id="a once dated Sunday meets a Sunday slot",
        ),
        pytest.param(
            b"\x03\x01\x0c\x00\x40",
            b"\x03\x01\x0c\x00\x00",
            False,
            False,
            id="a once dated Monday misses a Sunday slot",
        ),
        pytest.param(
            b"\x03\x01\x17\x1e\x00",
            b"\x03\x01\x17\x1e\x00",
            False,
            True,
            id="two once slots dated one Sunday meet",
        ),
        pytest.param(
            b"\x03\x01\x17\x1e\x00",
            b"\x03\x01\x17\x1e\x00",
            True,
            True,
            id="once slots dated Sunday and Saturday still meet",
        ),
    ],
)
@pytest.mark.parametrize("model", [A2345, A91B2])
def test_a_prime_4208_collision_takes_a_once_slots_weekday_from_its_date(
    model: type[A2345 | A91B2],
    first: bytes,
    second: bytes,
    *,
    back: bool,
    collides: bool,
) -> None:
    """The conflict clear: a once slot's day is its date's (port 0 on both).

    prime-a2345-a91b2.md "Defaults and collisions" and the A91B2 ``0308`` row:
    the off-time written second disables the enabled on-time at the same
    hh:mm, the ``0308`` before the ack. Against a repeating slot a once slot
    counts on its date's weekday; two once slots always meet. Here it is
    Sunday 23:25; ``back`` writes the second on Saturday.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(device, client, 0x208, [port, (0xA3, b"\x01\x01"), (0xA4, first)])
    if back:
        moved = ui(4).build(ui(4).parse(TIMESTAMP) - DAY_SECONDS)
        fields = [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)]
        exchange(device.negotiated_module, client, 0x022, fields)

    written = command(
        device, client, 0x208, [port, (0xA3, b"\x01\x00"), (0xA4, second)]
    )

    expected = ["4308", "4a08"] if collides else ["4a08"]
    assert [cmd for cmd, _ in written] == expected
    on_time = decode_field(snapshot(device), "aa", Timer).timed_task_start_switch
    assert on_time == (0 if collides else 1)


@pytest.mark.parametrize(
    ("days", "collides"),
    [
        pytest.param(0x01, True, id="a Monday 12:00 meets it"),
        pytest.param(0x40, False, id="a Sunday 12:00 misses it"),
    ],
)
@pytest.mark.parametrize("model", [A2345, A91B2])
def test_a_prime_unset_half_is_a_once_slot_dated_0_0_0_a_monday(
    model: type[A2345 | A91B2], days: int, *, collides: bool
) -> None:
    """The unset ``ff 0c 00 00`` half: a once 12:00 dated 0/0/0, which reads Monday.

    prime-a2345-a91b2.md "Defaults and collisions" (``FUN_0003e538``) and the
    A91B2 ``0308`` row (``weekday_bit_from_date?``): the dates are zeroed, and
    0/0/0 gives 0x01, so a written repeating 12:00 meets the unset off-time
    only on Monday.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    noon = (0xA4, bytes((3, 1, 12, 0, days)))

    written = command(
        device, client, 0x208, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x01"), noon]
    )

    expected = ["4308", "4a08"] if collides else ["4a08"]
    assert [cmd for cmd, _ in written] == expected
    off_time = decode_field(snapshot(device), "aa", Timer).timed_task_end_switch
    assert off_time == (0 if collides else 0xFF)


@pytest.mark.parametrize(
    ("phase", "written", "sent"),
    [
        pytest.param(0.0, 0.05, 0.5, id="a write between ticks"),
        pytest.param(0.05, 0.2, 0.65, id="the group's phase"),
    ],
)
def test_a2345_a_screen_report_goes_on_the_5th_grid_tick_after_its_write(
    phase: float, written: float, sent: float
) -> None:
    """``screen_report_service_tick`` runs in ``tick_100ms_group``, on its grid.

    prime-a2345-a91b2.md "Report timing": the 5th tick after the mark sends.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(systick_phase=phase)
    advance(device, client, clock, written)

    device.set_values(screen_brightness=40)
    early = advance(device, client, clock, sent - written - 0.03)
    due = advance(device, client, clock, 0.05)

    assert early == []
    assert [cmd for cmd, _ in due] == ["430a"]


def test_a2345_screen_reports_go_before_the_0302s_on_one_tick() -> None:
    """``screen_report_service_tick`` runs before ``port_switch_tick`` in a tick."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_relay_switch=False)
    advance(device, client, clock, 1)

    device.set_values(pd1_relay_switch=True)
    device.set_values(screen_brightness=40)
    sent = advance(device, client, clock, REPORT_WAIT + DISPLAY_TICK)

    assert [cmd for cmd, _ in sent] == ["430a", "4302"]


def test_a2345_a_port_switched_on_on_the_unit_is_the_power_on_tap() -> None:
    """``screen_port_on_click?`` 0x00018f84: source 2, its ``0302`` after the pass.

    The tap only turns a port on, so switching one off on the unit reports nothing.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    device.set_values(pd1_relay_switch=False)
    off = advance(device, client, clock, 1)
    device.set_values(pd1_relay_switch=True)
    early = advance(device, client, clock, 0.4)
    on = advance(device, client, clock, 0.2)

    assert (off, early) == ([], [])
    assert on == [("4302", "a10131a2020100a3020101" + device_clock(1))]


@pytest.mark.parametrize(
    ("fields", "record", "expected"),
    [
        pytest.param(
            [(0xA2, b"\x01\x04"), (0xA3, b"\x04\x02\x00\x00\x00\x00")],
            "ae",
            (True, 0, 0),
            id="port 4, en 2 as 1, seconds 0",
        ),
        pytest.param(
            [(0xA2, b"\x01\x00"), (0xA3, b"\x04\x00\x2c\x01\x00\x00")],
            "aa",
            (False, 300, 0),
            id="disarm writes its seconds",
        ),
    ],
)
def test_a2345_4209_writes_its_seconds_unchecked(
    fields: list[tuple[int, bytes]], record: str, expected: tuple[bool, int, int]
) -> None:
    """``countdown_set?``: any nonzero en is 1, the seconds go in unchecked.

    prime-a2345-a91b2.md ``0307`` row: into both set and remaining, the
    remaining forced to 0 on a disarm; ports 0-4 all take a countdown.
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    replies = command(device, client, 0x209, fields)

    timer = decode_field(snapshot(device), record, Timer)
    assert (replies[0][0], replies[0][1][:2]) == ("4a09", "00")
    assert (
        timer.countdown_task_switch,
        timer.countdown_task_seconds,
        timer.countdown_task_remaining_seconds,
    ) == expected


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param([(0xA2, b"\x01\x00\x00"), arm(1)], id="a2 length 3"),
        pytest.param([(0xA2, b"\x01\x00"), (0xA3, b"\x04\x01\x00")], id="a3 short"),
        pytest.param([(0xA2, b"\x01\x00")], id="no a3"),
    ],
)
def test_a2345_4209_refuses_an_a2_or_a3_of_another_length(
    fields: list[tuple[int, bytes]],
) -> None:
    """``cmd209`` answers ``04`` only for ``a2`` / ``a3`` lengths other than 2 / 6."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    replies = command(device, client, 0x209, fields)

    assert replies == [("4a09", "04a10131")]


@pytest.mark.parametrize(
    ("select", "disarmed", "kept"),
    [
        pytest.param(0, "00201c0000201c0000", ("aa", 7200, 7200), id="AC keeps it"),
        pytest.param(3, "0000000000201c0000", ("b1", 7200, 0), id="USB-C zeroes it"),
    ],
)
def test_an_a91b2_pushes_0307_on_the_pass_after_an_arm_or_disarm(
    select: int, disarmed: str, kept: tuple[str, int, int]
) -> None:
    """The countdown tick's enable edge shows a ``4209`` on the next timer pass.

    prime-a2345-a91b2.md A91B2 "Countdown timing" and ``0307`` row:
    ``cmd209_set_countdown`` only sets the record and replies; the edge is
    pushed by ``ac/usbc_countdown_tick_push_0307?``, a re-arm 1 -> 1 silent.
    The record carries the remaining time first.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, bytes([1, select]))
    advance(device, client, clock, 0.05)

    armed = command(device, client, 0x209, [port, arm(7200)])
    armed_pass = advance(device, client, clock, 0.15)
    rearmed = command(device, client, 0x209, [port, arm(7200)])
    rearmed += advance(device, client, clock, 0.15)
    disarm = (0xA3, b"\x04\x00\x00\x00\x00\x00")
    disarming = command(device, client, 0x209, [port, disarm])
    disarmed_pass = advance(device, client, clock, 0.15)

    a2 = f"a20201{select:02x}"
    reply = ("4a09", f"00a10131{a2}{device_clock(0)}")
    record = f"a10131{a2}a30a04{{}}{device_clock(0)}"
    assert armed == rearmed == disarming == [reply]
    assert armed_pass == [("4307", record.format("01201c0000201c0000"))]
    assert disarmed_pass == [("4307", record.format(disarmed))]
    name, seconds, remaining = kept
    countdown = decode_field(snapshot(device), name, Timer)
    assert (
        countdown.countdown_task_seconds,
        countdown.countdown_task_remaining_seconds,
    ) == (seconds, remaining)


def test_an_a2345_pushes_0307_on_the_pass_after_an_arm_or_disarm() -> None:
    """``countdown_tick_push_0307`` shows the enable edge on the next 150 ms pass.

    prime-a2345-a91b2.md ``0307`` row: a re-arm 1 -> 1 is silent; on disarm
    ``remaining`` is 0. ``countdown_set?`` writes every ``4209``'s seconds, a
    disarm's too.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    advance(device, client, clock, 0.05)

    armed = command(device, client, 0x209, [port, arm(7200)])
    armed_pass = advance(device, client, clock, 0.15)
    rearmed = command(device, client, 0x209, [port, arm(7200)])
    rearmed += advance(device, client, clock, 0.15)
    disarm = (0xA3, b"\x04\x00\x05\x00\x00\x00")
    disarmed = command(device, client, 0x209, [port, disarm])
    disarmed_pass = advance(device, client, clock, 0.15)

    reply = ("4a09", f"00a10131a2020100{device_clock(0)}")
    record = f"a10131a2020100a30a04{{}}{device_clock(0)}"
    assert armed == rearmed == disarmed == [reply]
    assert armed_pass == [("4307", record.format("01201c0000201c0000"))]
    assert disarmed_pass == [("4307", record.format("000500000000000000"))]
    timer = decode_field(snapshot(device), "aa", Timer)
    assert (timer.countdown_task_seconds, timer.countdown_task_remaining_seconds) == (
        5,
        0,
    )


@pytest.mark.parametrize(
    ("model", "select", "record", "expired", "left"),
    [
        pytest.param(
            A91B2, 0, "aa", "000300000003000000", 3, id="A91B2 AC keeps the seconds"
        ),
        pytest.param(
            A91B2, 3, "b1", "000000000003000000", 0, id="A91B2 USB-C zeroes it"
        ),
    ],
)
def test_an_a91b2_countdown_runs_down_then_switches_the_port_off(
    model: type[A91B2], select: int, record: str, expired: str, left: int
) -> None:
    """Each RTC second takes one off the remaining time; at 0 the port switches off.

    The 3 s pass ends it: the ``0307`` disarm, remaining as a ``4209`` disarm
    leaves it, then the port's ``0302`` from its switch task (AC on the next
    100 ms pass, USB-C 4 passes after its marking pass; doc A91B2 ``0302`` /
    ``0307`` rows).
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, bytes([1, select]))

    def countdown() -> tuple[bool, bool, int]:
        timer = decode_field(snapshot(device), record, Timer)
        return (
            timer.relay_switch,
            timer.countdown_task_switch,
            timer.countdown_task_remaining_seconds,
        )

    armed = command(device, client, 0x209, [port, arm(3)])
    timeline = [(countdown(), advance(device, client, clock, 1)) for _ in range(3)]
    ended = advance(device, client, clock, 0.5)

    a2 = f"a20201{select:02x}"
    stamp = device_clock(3)
    off = ("4302", f"a10131{a2}a3020100{stamp}")
    disarm = ("4307", f"a10131{a2}a30a04{expired}{stamp}")
    shown = ("4307", f"a10131{a2}a30a04010300000003000000{device_clock(0)}")
    assert [cmd for cmd, _ in armed] == ["4a09"]
    assert timeline == [
        ((True, True, 3), [shown]),
        ((True, True, 2), []),
        ((True, True, 1), []),
    ]
    assert ended == [disarm, off]
    assert countdown() == (False, False, left)


def test_a_countdown_jumped_past_expires_without_a_push() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(3)])
    clock.jump(10)
    jumped = device.negotiated_module.check_pushes()

    timer = decode_field(snapshot(device), "aa", Timer)
    assert jumped.frames == []
    assert not timer.relay_switch
    assert not timer.countdown_task_switch
    assert timer.countdown_task_remaining_seconds == 0


def test_a_countdown_disarm_drops_its_pending_expiry() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")

    command(device, client, 0x209, [port, arm(5)])
    early = advance(device, client, clock, 2)
    disarm = command(device, client, 0x209, [port, (0xA3, b"\x04\x00\x00\x00\x00\x00")])
    later = advance(device, client, clock, 10)

    assert [cmd for cmd, _ in early] == ["4307"]
    assert [cmd for cmd, _ in disarm] == ["4a09"]
    assert [cmd for cmd, _ in later] == ["4307"]
    assert decode_field(snapshot(device), "aa", Timer).relay_switch


def test_a_countdown_rearm_counts_down_again_from_its_seconds() -> None:
    """A re-arm at 3 s steps on the 4-8 s boundaries; the 8.1 s pass expires it."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")

    command(device, client, 0x209, [port, arm(5)])
    advance(device, client, clock, 3)
    rearm = command(device, client, 0x209, [port, arm(5)])
    restarted = decode_field(snapshot(device), "aa", Timer)
    early = advance(device, client, clock, 5.05)
    expired = advance(device, client, clock, 0.1)
    expired += advance(device, client, clock, 0.5)

    assert [cmd for cmd, _ in rearm] == ["4a09"]
    assert restarted.countdown_task_remaining_seconds == 5
    assert early == []
    assert [(cmd, payload[-14:]) for cmd, payload in expired] == [
        ("4307", device_clock(8)),
        ("4302", device_clock(8)),
    ]


def test_a_countdown_remaining_set_by_a_test_counts_down_from_there() -> None:
    """``remaining`` 2 from 0 s steps on the 1 and 2 s boundaries; 2.1 s expires it."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(600)])
    device.set_values(pd1_countdown_task_remaining_seconds=2)
    early = advance(device, client, clock, 2)
    expired = advance(device, client, clock, 1)

    assert [cmd for cmd, _ in early] == ["4307"]
    assert [cmd for cmd, _ in expired] == ["4307", "4302"]


@pytest.mark.parametrize(
    ("model", "select", "record", "app_reported"),
    [
        pytest.param(A2345, 0, "aa", False, id="A2345 usbc1"),
        pytest.param(A91B2, 2, "b0", True, id="A91B2 usbc1"),
    ],
)
def test_a_prime_timed_task_switches_its_port_at_its_time_each_week(
    model: type[A2345 | A91B2], select: int, record: str, *, app_reported: bool
) -> None:
    """An enabled off-time at Sunday 23:26 switches the port off then, by the RTC.

    The firmware fires it in that minute and ``0302`` reports it (doc "MCU
    pushes" ``0302``); a repeating half stays enabled and fires a week on. The
    ``4207`` turning it back on is reported too on the A91B2, which has no
    source gate; the A2345's source byte drops it.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, bytes([1, select]))
    sunday_2326 = (0xA4, b"\x03\x01\x17\x1a\x40")

    write = command(device, client, 0x208, [port, (0xA3, b"\x01\x00"), sunday_2326])
    early = advance(device, client, clock, 44)
    fired = advance(device, client, clock, 1)
    fired += advance(device, client, clock, REPORT_WAIT)
    timer = decode_field(snapshot(device), record, Timer)
    command(device, client, 0x207, [port, (0xA3, b"\x01\x01")])
    switched_on = advance(device, client, clock, REPORT_WAIT)
    week_early = advance(device, client, clock, 7 * 86400 - 1 - REPORT_WAIT)
    week = advance(device, client, clock, 1)
    week += advance(device, client, clock, REPORT_WAIT)

    off = f"a10131a20201{select:02x}a3020100"
    on = ("4302", f"a10131a20201{select:02x}a3020101{device_clock(46)}")
    assert [cmd for cmd, _ in write] == ["4a08"]
    assert switched_on == ([on] if app_reported else [])
    assert early == week_early == []
    assert fired == [("4302", off + device_clock(45))]
    assert not timer.relay_switch
    assert timer.timed_task_end_switch == 1
    assert week == [("4302", off + device_clock(45 + 7 * 86400))]


def test_a_once_timed_task_fires_at_its_next_time_then_disables_itself() -> None:
    """A once on-time (weekdays 0) fires at its date and minute, then ``0308`` en 00.

    Its date is off the wire: today's hh:mm, or tomorrow's once that minute is
    past (doc "MCU pushes" ``0308``: a fired once slot).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])

    command(
        device,
        client,
        0x208,
        [port, (0xA3, b"\x01\x01"), (0xA4, b"\x03\x01\x17\x1a\x00")],
    )
    fired = advance(device, client, clock, 45)
    fired += advance(device, client, clock, REPORT_WAIT)
    week = advance(device, client, clock, 7 * 86400)

    clock_field = device_clock(45)
    assert fired == [
        ("4308", f"a10131a2020100a3020101a4050400171a00{clock_field}"),
        ("4302", f"a10131a2020100a3020101{clock_field}"),
    ]
    assert week == []
    timer = decode_field(snapshot(device), "aa", Timer)
    assert timer.relay_switch
    assert timer.timed_task_start_switch == 0


def test_a_timed_task_firing_on_a_port_already_so_sends_no_0302() -> None:
    """An on-time firing on a port already on changes nothing and reports nothing."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")

    command(
        device,
        client,
        0x208,
        [port, (0xA3, b"\x01\x01"), (0xA4, b"\x03\x01\x17\x1a\x40")],
    )
    fired = advance(device, client, clock, 46)

    assert fired == []
    timer = decode_field(snapshot(device), "aa", Timer)
    assert timer.relay_switch
    assert timer.timed_task_start_switch == 1


def test_an_a91b2_once_timed_task_fires_at_its_date_then_disables_itself() -> None:
    """A once on-time (weekdays 0) fires at its date and minute, then ``0308`` en 00.

    prime-a2345-a91b2.md A91B2 "Schedule firing": on the first 150 ms pass in
    Sunday 23:26 (45 s on), the timer run fires it and disables it; usbc_1's
    ``0302`` goes 4 passes after the 45 s marking pass.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x02")
    command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])
    advance(device, client, clock, 0.6)

    command(
        device,
        client,
        0x208,
        [port, (0xA3, b"\x01\x01"), (0xA4, b"\x03\x01\x17\x1a\x00")],
    )
    fired = advance(device, client, clock, 44.9)

    clock_field = device_clock(45)
    assert fired == [
        ("4308", f"a10131a2020102a3020101a4050400171a00{clock_field}"),
        ("4302", f"a10131a2020102a3020101{clock_field}"),
    ]
    timer = decode_field(snapshot(device), "b0", Timer)
    assert timer.relay_switch
    assert timer.timed_task_start_switch == 0


def test_an_a91b2_timed_task_firing_on_a_port_already_so_sends_no_0302() -> None:
    """An on-time firing on a port already on changes nothing and reports nothing."""
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x02")

    command(device, client, 0x208, [port, (0xA3, b"\x01\x01"), SUNDAY_2326])
    fired = advance(device, client, clock, 46)

    assert fired == []
    assert decode_field(snapshot(device), "b0", Timer).relay_switch


def test_an_a91b2_countdown_expiry_on_a_port_already_off_sends_no_0302() -> None:
    """The expiry reports the switch only if it changes it; ``0307`` still disarms.

    Armed at 0.6 s, shown armed on the 0.75 s pass, stepped to 0 at the 1 s
    RTC edge, it expires on the 1.05 s pass.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x02")
    command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])
    advance(device, client, clock, 0.6)

    command(device, client, 0x209, [port, arm(1)])
    expired = advance(device, client, clock, 0.6)

    assert [cmd for cmd, _ in expired] == ["4307", "4307"]


def test_an_a91b2_countdown_steps_on_the_rtcs_own_second() -> None:
    """``remaining`` drops in the RTC second interrupt; the 150 ms group expires it.

    prime-a2345-a91b2.md A91B2 "Countdown timing" / "Timer timing": the RTC is
    set at 0.3 s, so its seconds start at 0.3 + k. A 2 s ``4209`` at 0.4 s
    steps at 1.3 and 2.3 s; the 2.4 s pass (one period after boot, 0.15 k)
    switches the port off and disarms it. The USB-C task marks the change on
    its 2.4 s pass, after the timer group, and sends 4 passes on.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x02")
    advance(device, client, clock, 0.3)
    fields = [(0xA1, TIMESTAMP), (0xA3, UTC_OFFSET), (0xA5, TZ)]
    exchange(device.negotiated_module, client, 0x022, fields)
    advance(device, client, clock, 0.1)

    def remaining() -> int:
        timer = decode_field(snapshot(device), "b0", Timer)
        return int(timer.countdown_task_remaining_seconds)

    command(device, client, 0x209, [port, arm(2)])
    steps = []
    for at in (0.85, 0.1):
        advance(device, client, clock, at)
        steps.append(remaining())
    before_pass = advance(device, client, clock, 0.9)
    expired = advance(device, client, clock, 0.2)
    off = not decode_field(snapshot(device), "b0", Timer).relay_switch
    reported = advance(device, client, clock, 0.4)

    assert steps == [2, 1]
    assert before_pass == []
    assert [cmd for cmd, _ in expired] == ["4307"]
    assert off
    assert [cmd for cmd, _ in reported] == ["4302"]


def test_an_a91b2_repeating_slot_needs_second_0() -> None:
    """A repeating slot fires only when the seconds read 0, so a jump into :05 skips.

    prime-a2345-a91b2.md A91B2 "Schedule firing": a clock set to Sunday
    23:26:05 leaves a 23:26 off-time unfired for that minute.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x02")
    command(device, client, 0x208, [port, (0xA3, b"\x01\x00"), SUNDAY_2326])
    advance(device, client, clock, 2)

    moved = ui(4).build(ui(4).parse(TIMESTAMP) + 2 + 49)
    fields = [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)]
    exchange(device.negotiated_module, client, 0x022, fields)
    advance(device, client, clock, 30)

    assert decode_field(snapshot(device), "b0", Timer).relay_switch


def test_an_a91b2_repeating_slot_fires_on_the_first_pass_in_second_0() -> None:
    """At phase 0.05 the passes run at 0.05 + 0.15 k: 23:26:00 (45 s) fires at 45.05."""
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(systick_phase=0.05)
    port = (0xA2, b"\x01\x02")
    command(device, client, 0x208, [port, (0xA3, b"\x01\x00"), SUNDAY_2326])

    advance(device, client, clock, 45.02)
    waiting = decode_field(snapshot(device), "b0", Timer).relay_switch
    advance(device, client, clock, 0.04)

    assert waiting
    assert not decode_field(snapshot(device), "b0", Timer).relay_switch


def test_an_a91b2_slots_hold_through_another_port_s_pass_in_second_0() -> None:
    """A fired repeating slot stays latched in second 0; a future once slot waits.

    prime-a2345-a91b2.md A91B2 "Schedule firing": the usbc_1 off-time fires on
    the 45 s pass; usbc_1 is switched back on, and a 0 s ``4209`` on usbc_2
    brings the 45.15 s pass, which neither fires usbc_1 again nor ends usbc_3's
    once 23:30.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    usbc_1 = (0xA2, b"\x01\x02")
    command(device, client, 0x208, [usbc_1, (0xA3, b"\x01\x00"), SUNDAY_2326])
    once_2330 = (0xA4, b"\x03\x01\x17\x1e\x00")
    command(
        device, client, 0x208, [(0xA2, b"\x01\x04"), (0xA3, b"\x01\x00"), once_2330]
    )
    advance(device, client, clock, 45.03)
    command(device, client, 0x207, [usbc_1, (0xA3, b"\x01\x01")])

    command(device, client, 0x209, [(0xA2, b"\x01\x03"), arm(0)])
    advance(device, client, clock, 0.15)

    assert decode_field(snapshot(device), "b0", Timer).relay_switch
    assert not decode_field(snapshot(device), "b1", Timer).relay_switch
    assert decode_field(snapshot(device), "b2", Timer).timed_task_end_switch == 1


@pytest.mark.parametrize(
    ("model", "select", "record", "sent"),
    [
        pytest.param(A2345, 0, "aa", 0.6, id="A2345: source 2, the 5th tick on"),
        pytest.param(A91B2, 0, "aa", 0.2, id="A91B2 AC: its next 100 ms pass"),
        pytest.param(A91B2, 2, "b0", 0.6, id="A91B2 USB-C: 4 passes after marking"),
    ],
)
def test_a_prime_countdown_armed_at_0_switches_its_port_off_on_the_next_pass(
    model: type[A2345 | A91B2], select: int, record: str, sent: float
) -> None:
    """A 0 s ``4209`` on a port that's on: the 0.15 s pass ends it and switches off.

    Ended before its edge is shown, it is never shown armed, so no ``0307``.
    The ``0302`` goes as each unit reports a switch: the A2345's countdown
    writes source 2 (it found the port on) and reports on the 5th tick; the
    A91B2's AC task on its 0.2 s pass, the USB-C task 4 passes after marking.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    advance(device, client, clock, 0.05)

    command(device, client, 0x209, [(0xA2, bytes([1, select])), arm(0)])
    early = advance(device, client, clock, sent - 0.05 - 0.02)
    timer = decode_field(snapshot(device), record, Timer)
    reported = advance(device, client, clock, 0.04)

    off = f"a10131a20201{select:02x}a3020100{device_clock(0)}"
    assert early == []
    assert not timer.relay_switch
    assert not timer.countdown_task_switch
    assert reported == [("4302", off)]


def test_an_a91b2_disarm_on_a_never_armed_ac_port_acks_and_draws_no_0307() -> None:
    """A never-armed AC port (record ``+0`` ``ff``) is skipped by the AC tick.

    prime-a2345-a91b2.md A91B2 "AC countdown record ``+0``": a disarm ``4209``
    there acks and draws no ``0307``; the countdown stays at its defaults.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    disarm = [(0xA2, b"\x01\x01"), (0xA3, b"\x04\x00\x00\x00\x00\x00")]

    replies = command(device, client, 0x209, disarm)
    later = advance(device, client, clock, 1)

    timer = decode_field(snapshot(device), "ab", Timer)
    assert [cmd for cmd, _ in replies] == ["4a09"]
    assert later == []
    assert not timer.countdown_task_switch


def test_an_a91b2_countdown_disarm_stops_its_steps() -> None:
    """A ``4209`` disarm on ac_1 keeps the seconds as the remaining time, no step on.

    The next pass shows the disarm; nothing follows.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(device, client, 0x209, [port, arm(5)])
    advance(device, client, clock, 0.5)

    command(device, client, 0x209, [port, (0xA3, b"\x04\x00\x00\x00\x00\x00")])
    later = advance(device, client, clock, 3)

    timer = decode_field(snapshot(device), "aa", Timer)
    assert [cmd for cmd, _ in later] == ["4307"]
    assert (timer.countdown_task_switch, timer.countdown_task_remaining_seconds) == (
        False,
        5,
    )
    assert timer.relay_switch


def test_an_a91b2_switch_flipped_back_within_a_pass_reports_nothing() -> None:
    """``ac_switch_push_0302?`` compares the switch with its shadow: off, on, none."""
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    advance(device, client, clock, 0.02)

    command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])
    command(device, client, 0x207, [port, (0xA3, b"\x01\x01")])
    later = advance(device, client, clock, 1)

    assert later == []


def test_a2345_a_clock_set_out_of_a_fired_minute_clears_its_latch() -> None:
    """The latch holds until a pass sees the minute no longer matching.

    prime-a2345-a91b2.md "Schedule timing": jumped out of 23:26 and back, the
    slot fires again.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(device, client, 0x208, [port, (0xA3, b"\x01\x00"), SUNDAY_2326])
    advance(device, client, clock, 45.1)
    command(device, client, 0x207, [port, (0xA3, b"\x01\x01")])

    def clock_set(shift: int) -> None:
        moved = ui(4).build(ui(4).parse(TIMESTAMP) + int(clock.now()) + shift)
        fields = [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)]
        exchange(device.negotiated_module, client, 0x022, fields)

    clock_set(60)
    advance(device, client, clock, 0.2)
    kept_on = decode_field(snapshot(device), "aa", Timer).relay_switch
    clock_set(0)
    advance(device, client, clock, 0.2)

    assert kept_on
    assert not decode_field(snapshot(device), "aa", Timer).relay_switch


def test_a_timed_task_disabled_drops_its_pending_firing() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    end = (0xA3, b"\x01\x00")

    command(device, client, 0x208, [port, end, (0xA4, b"\x03\x01\x17\x1a\x40")])
    command(device, client, 0x208, [port, end, (0xA4, b"\x03\x00\x17\x1a\x40")])
    later = advance(device, client, clock, 120)

    assert later == []
    assert decode_field(snapshot(device), "aa", Timer).relay_switch


@pytest.mark.parametrize(
    ("shift", "due"),
    [
        pytest.param(-3600, 3645, id="an hour back fires an hour later"),
        pytest.param(120, 7 * 86400 - 75, id="past it waits a week"),
    ],
)
def test_a_clock_set_re_times_a_queued_timed_task(shift: int, due: int) -> None:
    """A ``0022`` that moves the RTC moves a slot's firing by the new clock."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(
        device,
        client,
        0x208,
        [port, (0xA3, b"\x01\x00"), (0xA4, b"\x03\x01\x17\x1a\x40")],
    )

    moved = ui(4).build(ui(4).parse(TIMESTAMP) + shift)
    exchange(
        device.negotiated_module,
        client,
        0x022,
        [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)],
    )
    early = advance(device, client, clock, due - 1)
    fired = advance(device, client, clock, 1)
    fired += advance(device, client, clock, REPORT_WAIT)

    assert early == []
    assert fired == [("4302", f"a10131a2020100a3020100{device_clock(shift + due)}")]


def test_a2345_a_clock_set_into_a_slots_minute_fires_it_on_the_next_pass() -> None:
    """The pass checks the slot's hour:minute, so a jump into it fires it then.

    prime-a2345-a91b2.md "Schedule timing": the first pass in its minute fires
    a repeating slot.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(device, client, 0x208, [port, (0xA3, b"\x01\x00"), SUNDAY_2326])
    advance(device, client, clock, 2)

    moved = ui(4).build(ui(4).parse(TIMESTAMP) + 2 + 60)
    exchange(
        device.negotiated_module,
        client,
        0x022,
        [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)],
    )
    fired = advance(device, client, clock, 0.2)

    assert fired == []
    assert not decode_field(snapshot(device), "aa", Timer).relay_switch


def test_the_a91b2_mcu_sets_its_clock_a_second_ahead() -> None:
    """``esp_op09_set_clock``: UTC + 1 and RTC = UTC - offset + 1 (prime doc 0x09)."""
    device = A91B2(clock=ManualClock())

    prime_client(device, TOKEN)

    module = device.negotiated_module
    assert module.epoch == ui(4).parse(TIMESTAMP) + 1
    assert module.rtc == ui(4).parse(TIMESTAMP) - ui(4).parse(UTC_OFFSET) + 1


def test_the_a2345_mcu_takes_no_utc_before_2024_04_30() -> None:
    """``esp_op26_set_clock`` needs a UTC from 2024-04-30 16:00 on, then adds 1 s.

    prime doc fid 0x10 op ``0x26`` (``esp_op26_set_clock`` 0x00047f70).
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    before = (module.epoch, module.rtc)

    exchange(
        module,
        client,
        0x022,
        [(0xA1, ui(4).build(0x6631157F)), (0xA3, UTC_OFFSET), (0xA5, TZ)],
    )
    early = (module.epoch, module.rtc)
    exchange(
        module,
        client,
        0x022,
        [(0xA1, ui(4).build(0x66311580)), (0xA3, UTC_OFFSET), (0xA5, TZ)],
    )

    assert early == before
    assert module.epoch == 0x66311580 + 1


def test_an_a91b2_ac_disarm_copies_the_seconds_into_the_remaining_time() -> None:
    """The AC disarm path (0x08004f3c) restores the remaining time to the seconds.

    The next timer pass shows the disarm with it.
    """
    clock = ManualClock()
    device = A91B2(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(device, client, 0x209, [port, arm(7200)])
    advance(device, client, clock, 0.2)
    device.set_values(ac1_countdown_task_remaining_seconds=100)

    command(device, client, 0x209, [port, (0xA3, b"\x04\x00\x00\x00\x00\x00")])
    disarmed = advance(device, client, clock, 0.15)

    assert disarmed == [
        ("4307", "a10131a2020100a30a0400201c0000201c0000" + device_clock(0))
    ]
    countdown = decode_field(snapshot(device), "aa", Timer)
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

    The ``0308`` comes before the ack (prime-a2345-a91b2.md's ``4208`` row and
    "MCU pushes" ``0308``); both carry the unit's clock, as the A91B2 ``0308``
    worked example does.
    """
    device = model(clock=ManualClock())
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
        ("4308", f"a10131a20201{select:02x}a3020100a40504000c0000{device_clock(0)}"),
        ("4a08", f"00a10131a20201{select:02x}a3020101{device_clock(0)}"),
    ]
    assert [cmd_hex(reply.frame) for reply in nine] == ["4a08"]
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    end_switch = decode_field(Parameters.parse(snapshot.payload), record, Timer)
    assert end_switch.timed_task_end_switch == 0


def test_a2345_pushes_030f_at_once_and_030d_after_the_mode_settle() -> None:
    """430f carries the designated mask at once, 430d the mode about 3.3 s later.

    430d carries the mode, ``b8`` and the mask, after the fixed settle counter,
    for 420c and 4206 alike (prime-a2345-a91b2.md "MCU pushes" 030f); leaving
    Port Priority clears the designations (the snapshot's ``[18]`` priority row).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    designate = command(device, client, 0x20C, [(0xA2, b"\x01\x01"), CLOCK])
    early = advance(device, client, clock, 3.2)
    settled = advance(device, client, clock, 0.2)
    again = command(device, client, 0x20C, [(0xA2, b"\x01\x01"), CLOCK])
    same_mode = command(device, client, 0x206, [(0xA2, b"\x01\x02"), CLOCK])
    smart = command(device, client, 0x206, [(0xA2, b"\x01\x01"), CLOCK])
    smart_settled = advance(device, client, clock, 3.4)

    profile = "a30804010046412d2d18"
    assert designate == [
        ("4a0c", "00a10131"),
        ("430f", f"a10131a3020101{device_clock(0)}"),
    ]
    assert early == []
    assert settled == [
        ("430d", f"a10131a2020102{profile}a4020101{device_clock(3)}"),
    ]
    assert [cmd for cmd, _ in again] == ["4a0c"]
    assert [cmd for cmd, _ in same_mode] == ["4a06"]
    assert smart == [
        ("4a06", "00a10131"),
        ("430f", f"a10131a3020100{device_clock(3)}"),
    ]
    assert smart_settled == [
        ("430d", f"a10131a2020101{profile}a4020100{device_clock(6)}"),
    ]
    params = snapshot(device)
    priorities = [
        decode_field(params, tag, Schedule).port_priority
        for tag in ("aa", "ab", "ac", "ad")
    ]
    assert priorities == [0, 0, 0, 0]


def test_a2345_mode_change_drops_every_port_to_0_w_while_they_renegotiate() -> None:
    """Every port block reads status 0, 0 W, for about 8 s, then what it had.

    doc "Commands" ``4206``: "renegotiates every port: all drop to 0 W for
    ~8 s". A value set during the window keeps it.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_output_status=1, pd1_output_power=60)
    module = device.negotiated_module
    ports = ("a4", "a5", "a6", "a7", "a8", "a9")

    def blocks() -> list[tuple[int, int]]:
        params = snapshot(device)
        return [
            (block.output_status, block.output_power)
            for block in (decode_field(params, tag, OutputPort) for tag in ports)
        ]

    def stream() -> list[tuple[int, int]]:
        params = Parameters.parse(module.mcu.push(0x303, values=module.values).payload)
        return [
            (block.output_status, block.output_power)
            for block in (
                decode_field(params, tag, OutputPort)
                for tag in ("a2", "a3", "a4", "a5", "a6", "a7")
            )
        ]

    had, had_stream = blocks(), stream()
    command(device, client, 0x206, [(0xA2, b"\x01\x03")])
    renegotiating, renegotiating_stream = blocks(), stream()
    advance(device, client, clock, 4)
    device.set_values(pd2_output_power=30)
    early = advance(device, client, clock, 3.9)
    still = blocks()
    back = advance(device, client, clock, 0.2)

    assert had[0] == had_stream[0] == (1, 60)
    assert renegotiating == renegotiating_stream == [(0, 0)] * 6
    assert early == back == []
    assert still == [(0, 0), (0, 30), (0, 0), (0, 0), (0, 0), (0, 0)]
    assert blocks() == [had[0], (had[1][0], 30), *had[2:]]
    assert stream() == [had_stream[0], (had_stream[1][0], 30), *had_stream[2:]]


def test_a2345_second_mode_change_while_renegotiating_keeps_the_one_window() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_output_status=1, pd1_output_power=60)
    had = snapshot(device)["a4"]["value"]

    command(device, client, 0x206, [(0xA2, b"\x01\x03")])
    advance(device, client, clock, 4)
    command(device, client, 0x206, [(0xA2, b"\x01\x04")])
    during = snapshot(device)["a4"]["value"]
    advance(device, client, clock, 4.1)

    assert decode_field(snapshot(device), "a4", OutputPort).output_power == 60
    assert snapshot(device)["a4"]["value"] == had != during


def test_a2345_mode_change_jumped_past_restores_the_ports_without_a_push() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    had = snapshot(device)

    command(device, client, 0x206, [(0xA2, b"\x01\x03")])
    clock.jump(30)
    jumped = device.negotiated_module.check_pushes()

    assert jumped.frames == []
    after = snapshot(device)
    assert [after[tag]["value"] for tag in ("a4", "a5", "a6", "a7", "a8", "a9")] == [
        had[tag]["value"] for tag in ("a4", "a5", "a6", "a7", "a8", "a9")
    ]


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
    # Acked 00; knob_direction_set's range check ignores it (cmd20e 0x000439f4).
    assert bad_knob[0].plaintext == bytes.fromhex("00a10131")
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


def test_a2345_owner_change_resets_a_custom_theme_and_a_resend_does_nothing() -> None:
    """``420a`` stores its owner; only a different one counts as a change.

    A change puts a custom-upload theme (5) back to 0; an identical re-send and
    a malformed owner change nothing. The recorded ``4a0a`` answers every send
    (doc "``420a``": ``region_owner_store_if_changed`` 0x00015bf8).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    country = (0xA2, b"\x02US")

    def owner_420a(
        owner: bytes, theme: int, region: tuple[int, bytes] = country
    ) -> tuple[bool, int]:
        device.set_values(screen_saver_theme=theme)
        fields = [region, (0xA3, b"\x04" + owner)] if owner else [region]
        replies = command(device, client, 0x20A, fields)
        recorded = [("4a0a", device.mcu.respond(0x20A)[0].payload.hex())]
        af = decode_field(snapshot(device), "af", Theme)
        return replies == recorded, af.screen_saver_theme

    assert owner_420a(ACCOUNT, 5) == (True, 0)
    assert owner_420a(ACCOUNT, 5) == (True, 5)
    assert owner_420a(b"b" * 40, 5) == (True, 0)
    assert owner_420a(b"c" * 36, 2) == (True, 2)
    assert owner_420a(b"d" * 10, 5) == (True, 5)
    assert owner_420a(b"", 5) == (True, 5)
    assert owner_420a(b"e" * 36, 5, (0xA2, b"\x04GB")) == (True, 0)  # SolixBLE's


def test_a2345_owner_change_leaves_custom_mode_for_the_last_mode_held() -> None:
    """A new owner in custom mode 5 restores the last mode 1-4, then ``030d``.

    doc "``420a``": every exit from mode 5 does ``charging_mode_set(DAT_2000af62)``,
    the last mode below 5 that was active; the mode change reports as any does.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    country = (0xA2, b"\x02US")
    device.set_values(charging_mode=3)
    device.set_values(charging_mode=5)
    advance(device, client, clock, 10)

    command(device, client, 0x20A, [country, (0xA3, b"\x04" + ACCOUNT)])
    restored = decode_field(snapshot(device), "b1", ui(1))
    settled = advance(device, client, clock, 3.4)
    command(device, client, 0x20A, [country, (0xA3, b"\x04" + ACCOUNT)])

    assert restored == 3
    assert [(cmd, payload[:14]) for cmd, payload in settled] == [
        ("430d", "a10131a2020103")
    ]
    assert decode_field(snapshot(device), "b1", ui(1)) == 3


def test_a2345_custom_mode_switch_off_restores_the_last_mode_held() -> None:
    """``4211 a2 0`` leaves custom mode 5 for the last mode 1-4; ``a2 1`` does nothing.

    doc "Commands" ``4211``: "0 restores the previous mode"; ``420a`` bullet.
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    device.set_values(charging_mode=4)
    device.set_values(charging_mode=5)

    def switch(value: int) -> int:
        command(device, client, 0x211, [(0xA2, bytes([1, value]))])
        return int(decode_field(snapshot(device), "b1", ui(1)))

    assert switch(1) == 5
    assert switch(0) == 4
    assert switch(0) == 4


def test_a2345_owner_change_on_a_fresh_custom_unit_restores_the_factory_mode() -> None:
    """With no change below 5 since the factory, the last mode is 1, AI Power."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    device.set_values(charging_mode=5)

    command(device, client, 0x20A, [(0xA2, b"\x02US"), (0xA3, b"\x04" + ACCOUNT)])

    assert decode_field(snapshot(device), "b1", ui(1)) == 1


def test_a2345_reports_a_screen_setting_changed_on_the_unit() -> None:
    """``030a`` brightness, ``0309`` timeout, ``0310`` knob, ``030b`` theme.

    Each goes out when its setting changes on the unit (prime-a2345-a91b2.md
    "Screen reports"), ``030b`` with its ``a3`` theme id, on BLE's ``a1 31``.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    theme_id = "a305048eb81a0e"

    def settled(**values: Value) -> list[tuple[str, str]]:
        device.set_values(**values)
        return advance(device, client, clock, REPORT_WAIT)

    assert settled(screen_brightness=40) == [
        ("430a", f"a10131a2020128{device_clock(0)}")
    ]
    assert settled(screen_timeout=2, button_rotate_direction=1) == [
        ("4309", f"a10131a2020102{device_clock(1)}"),
        ("4310", f"a10131a2020101{device_clock(1)}"),
    ]
    assert settled(screen_saver_theme=0, screen_saver_theme_id=0x0E1AB88E) == [
        ("430b", f"a10131a20201c0{theme_id}{device_clock(1)}")
    ]
    assert settled(screen_saver_switch=False, holiday_screensaver_switch=False) == [
        ("430b", f"a10131a2020100{theme_id}{device_clock(2)}")
    ]
    assert settled(screen_timeout=2) == []


def test_a2345_screen_report_waits_out_the_debounce() -> None:
    """``screen_report_dirty_debounce``: a report goes > 4 ticks (~500 ms) after.

    prime-a2345-a91b2.md "Screen reports", "Senders".
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    early = unit_change(device, client, screen_brightness=40)
    still = advance(device, client, clock, 0.4)
    due = advance(device, client, clock, 0.1)

    assert (early, still) == ([], [])
    assert [cmd for cmd, _ in due] == ["430a"]


def test_a2345_brightness_set_unchanged_on_the_unit_still_reports() -> None:
    """``screen_brightness_set`` marks ``030a`` pending even when unchanged.

    The other settings report only a change (prime-a2345-a91b2.md "Senders").
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(screen_brightness=40, screen_timeout=2)
    advance(device, client, clock, REPORT_WAIT)

    device.set_values(screen_brightness=40, screen_timeout=2)
    again = advance(device, client, clock, REPORT_WAIT)

    assert [cmd for cmd, _ in again] == ["430a"]


def test_a2345_repeated_brightness_writes_keep_pushing_030a_out() -> None:
    """Each ``*_mark_pending`` is pending 1, count 0: a write restarts its count.

    prime-a2345-a91b2.md "Senders": writes at 0, 0.3 and 0.6 s, the same value,
    send one ``030a`` 0.5 s after the last.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    held = []

    for _ in range(3):
        device.set_values(screen_brightness=40)
        held += advance(device, client, clock, 0.3)
    due = advance(device, client, clock, 0.3)

    assert held == []
    assert [cmd for cmd, _ in due] == ["430a"]


#: An on-unit change, and the app setter of the same report's setting.
APP_WRITES_IN_THE_DEBOUNCE = [
    pytest.param({"screen_brightness": 40}, 0x204, b"\x01\x32", id="030a 4204"),
    pytest.param({"screen_timeout": 2}, 0x203, b"\x01\x01", id="0309 4203"),
    pytest.param({"button_rotate_direction": 1}, 0x20E, b"\x01\x00", id="0310 420e"),
    pytest.param({"screen_saver_theme": 1}, 0x205, b"\x01\x82", id="030b 4205"),
    # Acked, then ignored by the setter's range check: the byte is still written.
    pytest.param({"screen_timeout": 2}, 0x203, b"\x01\x05", id="0309 4203 range"),
    pytest.param(
        {"button_rotate_direction": 1}, 0x20E, b"\x01\x02", id="0310 420e range"
    ),
    pytest.param({"screen_saver_theme": 1}, 0x205, b"\x01\x00", id="030b clock off"),
]
#: An on-unit change, and an app command that writes no source byte: ``04`` for
#: ``a2``'s length, or a network ``4205`` missing ``a3``-``a7``.
APP_COMMANDS_THAT_KEEP_THE_REPORT = [
    pytest.param({"screen_timeout": 2}, 0x203, [], "4309", id="4203 no a2"),
    pytest.param(
        {"screen_timeout": 2}, 0x203, [(0xA2, b"\x01\x00\x01")], "4309", id="len 3"
    ),
    pytest.param(
        {"screen_saver_theme": 1}, 0x205, [(0xA2, b"\x01\x83")], "430b", id="bare 83"
    ),
]


@pytest.mark.parametrize(("unit", "msgtype", "a2"), APP_WRITES_IN_THE_DEBOUNCE)
def test_a2345_an_app_write_inside_the_debounce_drops_the_unit_report(
    unit: dict[str, Value], msgtype: int, a2: bytes
) -> None:
    """The app's handler sets the report's source byte to 1; 1 drops it at send.

    prime-a2345-a91b2.md "Screen reports": an on-unit change, then the app's
    setter 0.2 s on, sends no ``03xx`` at 0.5 s or after.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    device.set_values(**unit)
    advance(device, client, clock, 0.2)
    replies = command(device, client, msgtype, [(0xA2, a2)])
    later = advance(device, client, clock, 0.3) + advance(device, client, clock, 1)

    assert [cmd for cmd, _ in replies] == [f"4a{msgtype & 0xFF:02x}"]
    assert later == []


def test_a2345_an_unit_change_after_an_app_write_still_reports() -> None:
    """The on-unit change writes the source byte 2 last, so its report goes."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    command(device, client, 0x204, [(0xA2, b"\x01\x32")])
    advance(device, client, clock, 0.2)
    device.set_values(screen_brightness=40)
    reports = advance(device, client, clock, REPORT_WAIT)

    assert [cmd for cmd, _ in reports] == ["430a"]


@pytest.mark.parametrize(
    ("unit", "msgtype", "fields", "report"), APP_COMMANDS_THAT_KEEP_THE_REPORT
)
def test_a2345_an_app_command_writing_no_source_byte_keeps_the_report(
    unit: dict[str, Value],
    msgtype: int,
    fields: list[tuple[int, bytes]],
    report: str,
) -> None:
    """prime-a2345-a91b2.md "Source-byte order": these leave the byte alone.

    A ``04`` for ``a2``'s length, or a network ``4205`` missing ``a3``-``a7``
    (acked ``00``, logged ``0x41c``-``0x41f``), so the on-unit report goes.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    device.set_values(**unit)
    advance(device, client, clock, 0.2)
    command(device, client, msgtype, fields)
    reports = advance(device, client, clock, 0.3)

    assert [cmd for cmd, _ in reports] == [report]


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        pytest.param(100, 100, id="100 stored"),
        pytest.param(101, 40, id="101 ignored"),
        pytest.param(255, 40, id="255 ignored"),
    ],
)
def test_a2345_4204_over_100_is_acked_and_ignored(value: int, stored: int) -> None:
    """``screen_brightness_set`` 0x00023f54: a value over 100 is ignored, acked 00."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    device.set_values(screen_brightness=40)

    replies = command(device, client, 0x204, [(0xA2, b"\x01" + bytes([value]))])

    assert [(cmd, payload[:2]) for cmd, payload in replies] == [("4a04", "00")]
    assert decode_field(snapshot(device), "b3", ui(1)) == stored


def test_a2345_app_screen_settings_draw_no_report() -> None:
    """A ``42xx`` from the app sets the screen with only its ack.

    An app ``4205``'s ``0a05`` ack draws no ``030b`` (prime-a2345-a91b2.md
    "Screen reports").
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    brightness = command(device, client, 0x204, [(0xA2, b"\x01\x28")])
    timeout = command(device, client, 0x203, [(0xA2, b"\x01\x02")])
    knob = command(device, client, 0x20E, [(0xA2, b"\x01\x01")])
    theme = command(device, client, 0x205, [(0xA2, b"\x01\x81")])

    assert [cmd for cmd, _ in [*brightness, *timeout, *knob, *theme]] == [
        "4a04",
        "4a03",
        "4a0e",
        "4a05",
    ]
    assert decode_field(snapshot(device), "b3", ui(1)) == 40


#: ``bd`` / ``be`` with no identity: ``{fffa, fffb}`` and ``{ffff, ffff}``.
NO_DEVICE = (0xFFFA, 0xFFFB, 0xFFFF, 0xFFFF)


def test_a2345_plugged_device_is_identified_then_resolved() -> None:
    """``0304`` at once; ``bd`` 4 s after attach, ``be`` one frame later.

    doc ``bd`` row and "``bb`` Maximum Compatibility" (C1 ``05ac:7117``,
    20:05:26.6 -> 30.6 -> 31.6); ``be`` ``{1, 54}`` per "AI device identity".
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    plugged = pushed_by(device, client, lambda: device.plug(1, 0x05AC, 0x7117))
    early = advance(device, client, clock, 3.9)
    before = usbc_identity(device, 1)
    advance(device, client, clock, 0.1)
    identified = usbc_identity(device, 1), usbc_identity(device, 1, 0x303)
    advance(device, client, clock, 1)

    # bit 1 too: the recording has a device on C2 (bd 04b4:f665)
    assert plugged == [("4304", f"a10131a203030300{device_clock(0)}")]
    assert early == []
    assert before == NO_DEVICE
    assert identified == ((0x05AC, 0x7117, 0xFFFF, 0xFFFF),) * 2
    assert usbc_identity(device, 1) == (0x05AC, 0x7117, 1, 54)
    assert usbc_identity(device, 1, 0x303) == (0x05AC, 0x7117, 1, 54)
    assert decode_field(snapshot(device), "a4", OutputPort).output_status == 1


def test_a2345_max_compatibility_on_holds_identity_until_it_is_off() -> None:
    """With ``bb`` 0 a port stays unidentified; ``bb`` 0 -> 1 identifies it 6 s on.

    doc "``bb`` Maximum Compatibility": ``bb`` 0 keeps ``{fffa, fffb}``;
    0 -> 1 identifies already-attached devices ~6 s after the first ``0214``.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    command(device, client, 0x214, [(0xA2, b"\x01\x00")])

    device.plug(4, 0x05AC, 0x7311)
    held = advance(device, client, clock, 10), usbc_identity(device, 4)
    command(device, client, 0x214, [(0xA2, b"\x01\x01")])
    advance(device, client, clock, 5.9)
    early = usbc_identity(device, 4)
    advance(device, client, clock, 1.1)

    assert held == ([], NO_DEVICE)
    assert early == NO_DEVICE
    assert usbc_identity(device, 4) == (0x05AC, 0x7311, 1, 0xFFFF)


@pytest.mark.parametrize(
    ("vid", "pid", "ext", "code"),
    [
        pytest.param(0x05AC, 0x7117, 0x012C, 55, id="7117 ext 012c"),
        pytest.param(0x05AC, 0x7117, 0x017C, 56, id="7117 ext 017c"),
        pytest.param(0x05AC, 0x7519, 0x00A0, 32, id="7519 two variants to 32"),
        pytest.param(0x05AC, 0x7519, 0x00BE, 33, id="7519 ext 00be"),
        pytest.param(0x05AC, 0x7117, None, 54, id="no ext read"),
        pytest.param(0x05AC, 0x7117, 0xFFFF, 54, id="ext ffff"),
        pytest.param(0x05AC, 0x7117, 0x0000, 54, id="ext 0"),
        pytest.param(0x05AC, 0x7117, 0x0118, 54, id="no matching row"),
        pytest.param(0x05AC, 0x7504, 0x012C, 1, id="PID with one row"),
        pytest.param(0x291A, 0x7117, 0x012C, 54, id="class 10 takes the default"),
    ],
)
def test_a2345_identity_picks_the_variant_row_by_the_extended_identity(
    vid: int, pid: int, ext: int | None, code: int
) -> None:
    """``be``'s code: the PID's ``ffff`` row, or a class 1 variant row by ``ext``.

    The row whose variant is the controller's extended identity word wins (doc
    "AI device identity (``be``)", ``usbc_identity_resolve``).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    device.plug(1, vid, pid, ext=ext)
    advance(device, client, clock, 5)

    assert usbc_identity(device, 1)[3] == code


def test_a2345_identity_survives_max_compatibility_turned_on() -> None:
    """A port identified before ``bb`` -> 0 keeps its identity (doc ``bb`` table)."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.plug(1, 0x05AC, 0x7519)
    advance(device, client, clock, 5)

    command(device, client, 0x214, [(0xA2, b"\x01\x00")])
    advance(device, client, clock, 10)

    assert usbc_identity(device, 1) == (0x05AC, 0x7519, 1, 31)


def test_a2345_unplug_reports_and_clears_the_port() -> None:
    """``0304`` at once; the attach flag, power and identity go back to idle."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.plug(3, 0x05AC, 0x7117)
    advance(device, client, clock, 5)
    device.set_values(pd3_output_power=20)

    unplugged = pushed_by(device, client, lambda: device.unplug(3))

    block = decode_field(snapshot(device), "a6", OutputPort)
    assert unplugged == [("4304", f"a10131a203030200{device_clock(5)}")]
    assert (block.output_status, block.output_power) == (0, 0)
    assert usbc_identity(device, 3) == NO_DEVICE


def test_a2345_unplug_before_identification_drops_it() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    device.plug(4, 0x05AC, 0x7117)
    advance(device, client, clock, 2)
    device.unplug(4)
    advance(device, client, clock, 5)

    assert usbc_identity(device, 4) == NO_DEVICE


@pytest.mark.parametrize(
    ("vid", "pid", "resolved"),
    [
        pytest.param(0x05AC, 0x7519, (1, 31), id="Apple 7519"),
        pytest.param(0x05AC, 0x7311, (1, 0xFFFF), id="Apple PID not in the table"),
        pytest.param(0x05AC, 0x7800, (0xFFFF, 0xFFFF), id="Apple 7800"),
        pytest.param(0x291A, 0x2345, (10, 11), id="Anker A2345"),
        pytest.param(0x17EF, 0xA5A6, (14, 1), id="Lenovo"),
    ],
)
def test_a2345_resolves_a_device_identity_by_its_vid_and_pid(
    vid: int, pid: int, resolved: tuple[int, int]
) -> None:
    """``be`` = class by VID, code by PID's default row (doc "AI device identity")."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    device.plug(1, vid, pid)
    advance(device, client, clock, 5)

    assert usbc_identity(device, 1)[2:] == resolved


#: A 500-byte baseline JPEG in three 192-byte chunks, the last short, two to a
#: window.
IMAGE = baseline_jpeg()
#: Its theme id (the 102026-2 recording's ``030b`` theme, not the snapshot's
#: own) and the ``4311`` fields naming it.
THEME_ID = 0x0E1AB88E
THEME_4311 = "a10131a205048eb81a0e"


def test_a2345_unstored_theme_is_uploaded_window_by_window_then_shown() -> None:
    """WiFi down: ``4311`` 4, then ``4220`` and ``4221`` until ``4311`` 1 and ``10``.

    doc "``4311`` theme image state" and "Theme upload": ``00`` + the next index
    after each window, ``10`` + the count on the last chunk with its CRC
    matching, ``4311`` 1 before that reply (app_log 2026-09-03 22:01:12.512 and
    22:01:30.509, ``4311`` 4 then 1; ``4a21`` ``00a10131a203020a00``).
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    selected = command(device, client, 0x205, theme_select(IMAGE, THEME_ID))
    shown_before = decode_field(snapshot(device), "af", Theme)
    prepared = command(
        device, client, 0x220, upload_prepare(IMAGE, THEME_ID, window=2, chunk=192)
    )
    chunks = [
        command(device, client, 0x221, upload_chunk(IMAGE, i, 192)) for i in range(3)
    ]

    assert [cmd for cmd, _ in selected] == ["4a05", "4311"]
    assert selected[1][1].startswith(f"{THEME_4311}a3020104a4020103")
    assert shown_before.screen_saver_theme_id != THEME_ID
    assert prepared == [("4a20", "00a10131")]
    assert chunks[0] == []
    assert chunks[1] == [("4a21", "00a10131a203020200")]
    assert [cmd for cmd, _ in chunks[2]] == ["4311", "4a21"]
    assert chunks[2][0][1].startswith(f"{THEME_4311}a3020101a4020103")
    assert chunks[2][1] == ("4a21", "10a10131a203020300")
    af = decode_field(snapshot(device), "af", Theme)
    assert (af.screen_saver_switch, af.screen_saver_theme) == (True, 3)
    assert af.screen_saver_theme_id == THEME_ID


def test_a2345_stored_theme_is_shown_at_once() -> None:
    """A ``4205`` for a stored id with its CRC shows it and reports ``4311`` 1."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    command(device, client, 0x205, theme_select(IMAGE, THEME_ID))
    command(device, client, 0x220, upload_prepare(IMAGE, THEME_ID, window=2, chunk=192))
    for i in range(3):
        command(device, client, 0x221, upload_chunk(IMAGE, i, 192))
    command(device, client, 0x205, [(0xA2, b"\x01\x81")])

    again = command(device, client, 0x205, theme_select(IMAGE, THEME_ID))

    assert [cmd for cmd, _ in again] == ["4a05", "4311"]
    assert again[1][1].startswith(f"{THEME_4311}a3020101a4020103")
    assert decode_field(snapshot(device), "af", Theme).screen_saver_theme == 3


def test_a2345_upload_asks_for_a_resend_only_at_a_window_end() -> None:
    """A wrong index is dropped; at a window's last slot it gets ``12``.

    ``12`` carries the expected index, to resend from (doc "Theme upload").
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    command(device, client, 0x220, upload_prepare(IMAGE, THEME_ID, window=2, chunk=192))

    first = command(device, client, 0x221, upload_chunk(IMAGE, 0, 192))
    skipped = command(device, client, 0x221, upload_chunk(IMAGE, 2, 192))
    window_end = command(device, client, 0x221, upload_chunk(IMAGE, 3, 192))
    resent = command(device, client, 0x221, upload_chunk(IMAGE, 1, 192))

    assert first == skipped == []
    assert window_end == [("4a21", "12a10131a203020100")]
    assert resent == [("4a21", "00a10131a203020200")]


def test_a2345_upload_with_a_bad_crc_ends_11_and_shows_nothing() -> None:
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    had = decode_field(snapshot(device), "af", Theme)
    command(device, client, 0x220, upload_prepare(IMAGE, THEME_ID, window=2, chunk=192))
    corrupt = IMAGE[:-1] + b"\x00"

    replies = [
        command(device, client, 0x221, upload_chunk(corrupt, i, 192)) for i in range(3)
    ]

    assert replies[2] == [("4a21", "11a10131a203020300")]
    assert decode_field(snapshot(device), "af", Theme) == had


def test_a2345_upload_of_an_unknown_kind_gets_no_reply() -> None:
    """``4220 a2`` other than 0 (network) or 2 (custom) gets no reply."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    replies = command(
        device,
        client,
        0x220,
        upload_prepare(IMAGE, THEME_ID, window=2, chunk=192, kind=1),
    )

    assert replies == []


def test_a2345_upload_requests_missing_a_field_are_refused() -> None:
    """``4220`` needs ``a3``; ``4221`` needs its index and chunk: ``04`` without."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    prepare = upload_prepare(IMAGE, THEME_ID, window=2, chunk=192)

    no_id = command(device, client, 0x220, [f for f in prepare if f[0] != 0xA3])
    no_chunk = command(device, client, 0x221, upload_chunk(IMAGE, 0, 192)[:1])

    assert no_id == [("4a20", "04a10131")]
    assert no_chunk == [("4a21", "04a10131")]


def test_a2345_theme_without_its_url_is_acked_and_dropped() -> None:
    """A network ``4205`` with a URL under 16 characters acks and changes nothing."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    had = decode_field(snapshot(device), "af", Theme)

    replies = command(device, client, 0x205, theme_select(IMAGE, THEME_ID, url=b"x"))

    assert replies == [("4a05", "00a10131")]
    assert decode_field(snapshot(device), "af", Theme) == had


#: The two theme photos' ids, each its own CRC32, as the bench sent them.
PHOTO_ID = 0x5ADF8AA6
PHOTO_4311 = "a10131a20504a68adf5a"
PHOTO_2_ID = 0xEBBF1C3E
PHOTO_2_4311 = "a10131a205043e1cbfeb"


def test_a2345_unpadded_custom_photo_fails_its_first_draw() -> None:
    """The upload ends ``10`` and ``4311`` 1; then the draw erases it, screen 1.

    doc "Theme upload", bench 2026-10-30 04:42-04:43: a 65-byte last
    chunk leaves the chained CBC's last block cut short in flash, so the decrypt
    misses the CRC. The repeat ``4205`` gets ``4311`` 4; ``4222`` draws nothing.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    custom = theme_select(CUSTOM_PHOTO, PHOTO_ID, theme=0x85)

    selected = command(device, client, 0x205, custom)
    replies = upload_theme(device, client, CUSTOM_PHOTO, PHOTO_ID, kind=2)
    advance(device, client, clock, 0)
    af = decode_field(snapshot(device), "af", Theme)
    draws = module.values["_screen_draws"]
    again = command(device, client, 0x205, custom)
    time_display = [
        command(device, client, 0x222, [(0xA2, bytes((1, value)))]) for value in (0, 1)
    ]

    assert selected[1] == ("4311", f"{PHOTO_4311}a3020104a4020105")
    assert replies[0] == [("4a20", "00a10131")]
    assert replies[10] == [("4a21", "00a10131a203020a00")]
    assert replies[-1] == [
        ("4311", f"{PHOTO_4311}a3020101a4020105"),
        ("4a21", "10a10131a203022c00"),
    ]
    assert (af.screen_saver_theme, af.screen_saver_theme_id) == (1, PHOTO_ID)
    assert f"_theme_crc_{PHOTO_ID}" not in module.values
    assert again[1] == ("4311", f"{PHOTO_4311}a3020104a4020105")
    assert time_display == [[], []]
    assert (module.values["time_display"], module.values["_screen_draws"]) == (
        1,
        draws,
    )


def test_a2345_padded_custom_photo_stays_and_time_display_redraws_it() -> None:
    """Padded to 192, it decrypts to its CRC: screen 5, kept, redrawn by ``4222``.

    Bench 2026-10-30 05:14:53-05:19:55: the repeat ``4205`` drew ``4311`` 1 with
    no ``4220``; ``4222`` redraws only on a change, and only on screen 5.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    custom = theme_select(CUSTOM_PHOTO_2, PHOTO_2_ID, theme=0x85)
    command(device, client, 0x205, custom)

    replies = upload_theme(device, client, CUSTOM_PHOTO_2, PHOTO_2_ID, kind=2, pad=True)
    advance(device, client, clock, 0)
    af = decode_field(snapshot(device), "af", Theme)
    again = command(device, client, 0x205, custom)
    draws = [int(module.values["_screen_draws"])]
    for value in (0, 0, 1):
        command(device, client, 0x222, [(0xA2, bytes((1, value)))])
        draws.append(int(module.values["_screen_draws"]))

    assert replies[-1][1] == ("4a21", "10a10131a203026400")
    assert (af.screen_saver_theme, af.screen_saver_theme_id) == (5, PHOTO_2_ID)
    assert again == [
        ("4a05", "00a10131"),
        ("4311", f"{PHOTO_2_4311}a3020101a4020105"),
    ]
    assert [count - draws[0] for count in draws] == [0, 1, 1, 2]


def test_a2345_custom_photo_in_chunks_not_a_multiple_of_16_fails_its_draw() -> None:
    """Every chunk but the last must be a 16-multiple for the chained CBC.

    doc "Theme upload": one AES-CBC stream per ``4220``.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    command(device, client, 0x205, theme_select(CUSTOM_PHOTO, PHOTO_ID, theme=0x85))

    replies = upload_theme(
        device, client, CUSTOM_PHOTO, PHOTO_ID, kind=2, chunk=100, pad=True
    )
    advance(device, client, clock, 0)

    assert replies[-1][1] == ("4a21", "10a10131a203025400")
    assert decode_field(snapshot(device), "af", Theme).screen_saver_theme == 1


def test_a2345_unpadded_network_photo_is_stored_as_sent() -> None:
    """Kind 0 isn't encrypted: an unpadded last chunk draws fine on screen 3."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    command(device, client, 0x205, theme_select(CUSTOM_PHOTO, PHOTO_ID))

    upload_theme(device, client, CUSTOM_PHOTO, PHOTO_ID, kind=0)
    advance(device, client, clock, 0)

    af = decode_field(snapshot(device), "af", Theme)
    assert (af.screen_saver_theme, af.screen_saver_theme_id) == (3, PHOTO_ID)
    assert module.values[f"_theme_crc_{PHOTO_ID}"] == PHOTO_ID


@pytest.mark.parametrize(
    ("image", "shown"),
    [
        pytest.param(baseline_jpeg(), True, id="baseline 4:2:0"),
        pytest.param(baseline_jpeg(luma=0x11), True, id="4:4:4"),
        pytest.param(baseline_jpeg(luma=0x21), True, id="4:2:2"),
        pytest.param(
            baseline_jpeg(before_scan=b"\xff\xfe\x00\x05hey"), True, id="COM skipped"
        ),
        pytest.param(
            baseline_jpeg(before_scan=b"\xff\xdd\x00\x04\x00\x10"),
            True,
            id="DRI skipped",
        ),
        pytest.param(
            baseline_jpeg(before_scan=b"\xff\xe1\x00\x06Exif"), True, id="APP1 skipped"
        ),
        pytest.param(baseline_jpeg(sof=0xC2), False, id="progressive SOF2"),
        pytest.param(baseline_jpeg(sof=0xC1), False, id="extended SOF1"),
        pytest.param(baseline_jpeg(components=1), False, id="grayscale"),
        pytest.param(baseline_jpeg(precision=12), False, id="12-bit"),
        pytest.param(baseline_jpeg(luma=0x41), False, id="luma 4x1"),
        pytest.param(baseline_jpeg(quant=0x10), False, id="16-bit DQT"),
        pytest.param(baseline_jpeg(quant=0x03), False, id="DQT id 3"),
        pytest.param(baseline_jpeg(huffman=0x02), False, id="DHT id 2"),
        pytest.param(baseline_jpeg(spectral=b"\x01\x3f\x00"), False, id="Ss 1"),
        pytest.param(baseline_jpeg(before_scan=b"\xff\xd0"), False, id="RST0"),
        pytest.param(baseline_jpeg(before_scan=b"\xff"), False, id="fill byte"),
        pytest.param(baseline_jpeg(before_scan=b"\x00"), False, id="stray byte"),
        pytest.param(b"\x00" + baseline_jpeg()[1:], False, id="no SOI"),
        pytest.param(baseline_jpeg()[:108], False, id="ends before SOS"),
        pytest.param(baseline_jpeg()[:100], False, id="cut inside SOF0"),
    ],
)
def test_a2345_draw_checks_the_jpeg_markers(image: bytes, shown: bool) -> None:  # noqa: FBT001
    """Doc "Theme image display": a failing header erases the slot, screen 1."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    command(device, client, 0x205, theme_select(image, THEME_ID))

    upload_theme(device, client, image, THEME_ID, kind=0)
    advance(device, client, clock, 0)

    af = decode_field(snapshot(device), "af", Theme)
    stored = f"_theme_crc_{THEME_ID}" in module.values
    assert (af.screen_saver_theme, stored) == ((3, True) if shown else (1, False))


@pytest.mark.parametrize(
    ("size", "shown"),
    [
        pytest.param(65534, True, id="65 534 B"),
        pytest.param(65535, False, id="65 535 B"),
    ],
)
def test_a2345_draw_checks_the_stored_size(size: int, shown: bool) -> None:  # noqa: FBT001
    """A custom image over 65 534 B fits its 128 KB slot but never draws."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    image = baseline_jpeg(size)
    command(device, client, 0x205, theme_select(image, THEME_ID, theme=0x85))

    upload_theme(device, client, image, THEME_ID, kind=2, pad=True)
    advance(device, client, clock, 0)

    theme = decode_field(snapshot(device), "af", Theme).screen_saver_theme
    assert theme == (5 if shown else 1)


def test_a2345_secret_feature_1_fires_on_20_screen_switches_in_10_s() -> None:
    """``0305 a2`` 04 when the 20th-newest focus is within 10 s of the newest.

    doc "Secret Features": a sliding window, then a 60 s cooldown that drops a
    trigger.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    def switch() -> list[tuple[str, str]]:
        return pushed_by(device, client, lambda: device.unit_event("switch_screen"))

    def burst(gap: float, times: int) -> list[str]:
        pushes = []
        for _ in range(times):
            pushes += switch()
            pushes += advance(device, client, clock, gap)
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    slow = burst(0.55, 20)  # the 20th-newest 10.45 s before the newest
    clock.advance(30)
    fired = burst(0.5, 20)
    cooling = burst(0.5, 20)
    clock.advance(60)
    again = burst(0.5, 20)
    again += burst(1, 1)  # sent on the grid shifted by the first one's overlay

    assert slow == cooling == []
    assert fired == again == ["a10131a2020104"]


def test_a2345_secret_feature_2_fires_on_10_plugs_of_one_port_in_60_s() -> None:
    """``0305 a2`` 05 on a USB-C port's 10th attach within 60 s of its first.

    doc "Secret Features": the 60 s window is fixed from the first rise.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    def plug_cycles(times: int) -> list[tuple[str, str]]:
        pushes = []
        for _ in range(times):
            pushes += pushed_by(device, client, lambda: device.plug(1, 0x05AC, 0x7117))
            pushes += advance(device, client, clock, 0.5)
            device.unplug(1)
            pushes += advance(device, client, clock, 0.5)
        return pushes

    nine = plug_cycles(9)
    clock.advance(60)
    late = plug_cycles(1)
    clock.advance(400)
    ten = plug_cycles(10)

    assert "4305" not in [cmd for cmd, _ in nine + late]
    assert [payload[:14] for cmd, payload in ten if cmd == "4305"] == ["a10131a2020105"]


def test_a2345_feature_expert_fires_on_every_10th_designation() -> None:
    """``0305 a2`` 01 on every 10th counted designation (doc "Secret Features").

    ``420c`` counts one per set bit, but none made with 2 or more ports
    already designated.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    def designate(mask: int) -> list[str]:
        replies = command(device, client, 0x20C, [(0xA2, bytes([1, mask]))])
        replies += advance(device, client, clock, 1)
        return [payload[:14] for cmd, payload in replies if cmd == "4305"]

    three = designate(0b0111)  # counts 2: the third has 2 already designated
    pushes = []
    for _ in range(6):  # 6 more, one each: 8 counted
        designate(0)
        pushes += designate(0b0001)
    tenth = (designate(0), designate(0b1001))  # 2 more: the 9th and 10th

    assert three == pushes == []
    assert tenth == ([], ["a10131a2020101"])


def test_a2345_power_expert_fires_21_s_after_a_third_high_voltage_port() -> None:
    """``0305 a2`` 02 when 3 USB-C ports run above 18 V, once their class settles.

    doc "Secret Features" Power Expert: the class is 0 until pass 70, the
    300 ms pass that sees the active-port count change being pass 1; each rise
    of the count to 3 fires, no cooldown. The recording has C2 active at
    19 760 mV.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    def power_expert(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    two = unit_change(device, client, pd1_output_status=1, pd1_output_voltage=20000)
    two_settled = advance(device, client, clock, 30)
    three = unit_change(device, client, pd3_output_status=1, pd3_output_voltage=20000)
    settling = advance(device, client, clock, 20.9)
    settled = advance(device, client, clock, 1.2)  # settled 20.7 s on, sent at 51 s
    idle = advance(device, client, clock, 2)
    dip = unit_change(device, client, pd1_output_voltage=5000)
    dip += advance(device, client, clock, 0.3)  # a pass sees the dip
    recovered = unit_change(device, client, pd1_output_voltage=20000)
    recovered += advance(device, client, clock, 4)  # held by the first one's overlay

    assert power_expert(two + two_settled + three + settling + idle + dip) == []
    assert power_expert(settled) == power_expert(recovered) == ["a10131a2020102"]


def test_a2345_device_expert_fires_when_all_six_ports_are_active() -> None:
    """``0305 a2`` 03 on each change into all six ports active; then 24 h off.

    Leaving the state inside the cooldown is dropped too. Each change is held
    past a 300 ms pass, which is what sees it.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    ports = ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")

    def all_active() -> list[str]:
        pushes = unit_change(device, client, pd1_output_status=0)
        pushes += advance(device, client, clock, 0.3)
        pushes += unit_change(
            device, client, **{f"{port}_output_status": 1 for port in ports}
        )
        pushes += advance(device, client, clock, 1)
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    first = all_active()
    clock.advance(3600)
    cooling = all_active()
    clock.advance(24 * 3600)
    again = all_active()

    assert first == again == ["a10131a2020103"]
    assert cooling == []


def test_a2345_secret_features_are_sent_in_their_order() -> None:
    """Two pending at once: Device Expert (3), then Feature 2 (5) behind its overlay.

    doc "Secret Features": one pending feature serviced on each grid point, in
    the order ``420d``, 1, 2, 3, 4, 5; Device Expert's 150-frame overlay holds
    the count, so the next goes 10 passes after it ends, 5.9 s after the send.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd3_output_status=1, pd4_output_status=1, usb1_output_status=1)
    for _ in range(9):
        device.plug(1, 0x05AC, 0x7117)
        advance(device, client, clock, 0.5)
        device.unplug(1)
        advance(device, client, clock, 0.5)
    device.set_values(usb2_output_status=1)

    clock.advance(0.5)

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    tenth = pushed_by(device, client, lambda: device.plug(1, 0x05AC, 0x7117))
    first_point = advance(device, client, clock, 0.5)
    held = advance(device, client, clock, 5.85)
    next_point = advance(device, client, clock, 0.1)

    assert secret(tenth) == secret(held) == []
    assert secret(first_point) == ["a10131a2020103"]
    assert secret(next_point) == ["a10131a2020105"]


@pytest.mark.parametrize(
    ("first", "code", "gap"),
    [
        pytest.param("device_expert", "03", 5.9, id="Device Expert, 150 frames"),
        pytest.param("switch_screen", "04", 5.1, id="Feature 1, 125 frames"),
    ],
)
def test_a2345_a_feature_overlay_holds_the_next_service(
    first: str, code: str, gap: float
) -> None:
    """A ``420d`` pending behind a feature's overlay goes 10 passes after it ends.

    doc "Secret Features": frames x 33 ms, 4.95 s for 1-3, 4.125 s for 4 and
    5; the count resumes on the next 100 ms tick. Sent at 10 s here.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    clock.advance(9.0)
    if first == "device_expert":
        device.set_values(
            **{
                f"{port}_output_status": 1
                for port in ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")
            }
        )
    else:
        for _ in range(20):
            device.unit_event("switch_screen")
            advance(device, client, clock, 0.01)

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    sent = advance(device, client, clock, 10.0 - clock.now())
    clock.advance(1.0)
    command(device, client, 0x20D, [(0xA2, b"\x02\x01")])
    held = advance(device, client, clock, gap - 1.05)
    due = advance(device, client, clock, 0.1)

    assert secret(sent) == [f"a10131a20201{code}"]
    assert secret(held) == []
    assert secret(due) == ["a10131a2020100"]


@pytest.mark.parametrize(
    ("stop", "sent_at"),
    [
        pytest.param(None, 15.9, id="no stop: the overlay's end"),
        pytest.param(
            lambda device, client: device.unit_event("knob_activity"),
            15.9,
            id="knob activity in plain state 0: runs on",
        ),
        pytest.param(
            lambda device, client: command(
                device, client, 0x203, [(0xA2, b"\x01\x01")]
            ),
            15.9,
            id="a waking 4203 in plain state 0: runs on",
        ),
        pytest.param(
            lambda device, client: (
                device.unit_event("knob_hold"),
                device.unit_event("knob_activity"),
            ),
            12.6,
            id="knob activity with the inhibit set, read on the 11.6 s tick",
        ),
        pytest.param(
            lambda device, client: (
                device.unit_event("knob_hold"),
                command(device, client, 0x203, [(0xA2, b"\x01\x01")]),
            ),
            12.5,
            id="a waking 4203 with the inhibit set, at 11.55 s",
        ),
        pytest.param(
            lambda device, client: device.unit_event("knob_click"),
            12.6,
            id="a click: state 3 ends at 11.65 s, bound",
        ),
    ],
)
def test_a2345_an_early_overlay_stop_restarts_the_service_count(
    stop: Callable[[A2345, AppClient], object] | None, sent_at: float
) -> None:
    """``secret_overlay_stop?`` ends Device Expert's overlay; 10 passes on, the next.

    doc "Secret Features": the knob and app-setting wakes stop it only from
    1-3 or with the inhibit set (an overlay runs in 0); the display's 3 -> 0
    exit stops it. ``secret_features_tick`` runs first in a tick, so a stop on
    tick N gives the service at N + 10. Device Expert is sent at 10 s and a
    ``420d`` waits behind it.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    exchange(device.negotiated_module, client, 0x023, [(0xA2, ACCOUNT)])
    clock.advance(9.5)
    device.set_values(
        **{
            f"{port}_output_status": 1
            for port in ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")
        }
    )
    advance(device, client, clock, 0.5)
    clock.advance(1.0)
    command(device, client, 0x20D, [(0xA2, b"\x02\x01")])
    clock.advance(0.55)
    if stop is not None:
        stop(device, client)

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    held = advance(device, client, clock, sent_at - 0.05 - clock.now())
    due = advance(device, client, clock, 0.1)

    assert secret(held) == []
    assert secret(due) == ["a10131a2020100"]


@pytest.mark.parametrize(
    ("last", "sent"),
    [
        pytest.param(0.05, False, id="59.85 s after the send: dropped"),
        pytest.param(0.15, True, id="59.95 s after the send: sent"),
    ],
)
def test_a2345_a_cooldown_runs_from_the_send(last: float, *, sent: bool) -> None:
    """Feature 1's 600-pass cooldown is loaded at its send and wipes a raise.

    doc "Secret Features": the send's own pass counts one, so the last pass
    that wipes is 59.9 s after the send. Sent at 10 s (its 20th switch at
    9.5 s); the re-trigger's point is 70.1 s, on the grid its overlay shifted.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    def burst(times: int) -> list[str]:
        pushes = []
        for _ in range(times):
            pushes += pushed_by(
                device, client, lambda: device.unit_event("switch_screen")
            )
            pushes += advance(device, client, clock, 0.5)
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    fired = burst(20)
    clock.advance(50.3)
    nineteen = burst(19)
    clock.advance(last)
    retriggered = burst(1)
    later = advance(device, client, clock, 1)

    assert fired == ["a10131a2020104"]
    assert nineteen == []
    assert retriggered == (["a10131a2020104"] if sent else [])
    assert "4305" not in [cmd for cmd, _ in later]


def test_a2345_device_expert_fires_on_leaving_all_six_active() -> None:
    """``0305 a2`` 03 on a change out of all six active, once the 24 h are over.

    doc "Secret Features" Device Expert: raised on each change into or out of it.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    ports = ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    entered = unit_change(device, client, **{f"{p}_output_status": 1 for p in ports})
    entered += advance(device, client, clock, 1)
    clock.advance(24 * 3600)
    left = unit_change(device, client, pd1_output_status=0)
    left += advance(device, client, clock, 1)

    assert secret(entered) == secret(left) == ["a10131a2020103"]


def test_a2345_device_expert_is_raised_on_the_300ms_pass() -> None:
    """All six active at 0.95 s: the 1.2 s pass raises it, so the 2 s point sends it.

    doc "Scheduler groups": ``port_activity_scan_dispatch`` runs every 300 ms
    from boot, and ``device_expert_check`` on the pass that sees the change.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    ports = ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    clock.advance(0.95)
    changed = unit_change(device, client, **{f"{p}_output_status": 1 for p in ports})
    early = advance(device, client, clock, 1.0)
    due = advance(device, client, clock, 0.1)

    assert secret(changed) == secret(early) == []
    assert secret(due) == ["a10131a2020103"]


@pytest.mark.parametrize(
    ("at", "sent_at"),
    [
        pytest.param(29.95, 51.0, id="30 s pass: classed at 50.7 s, sent at 51 s"),
        pytest.param(30.05, 52.0, id="30.3 s pass: classed at 51 s, sent at 52 s"),
    ],
)
def test_a2345_power_expert_settles_70_passes_after_the_pass_that_sees_it(
    at: float, sent_at: float
) -> None:
    """A third port at ``at``: the next 300 ms pass is pass 1, pass 70 classes it.

    doc Power Expert row: 69 passes (20.7 s) after the pass that sees the
    change, then counted on the next 100 ms tick. 21 s would make the first
    case 52 s; 20.7 s from the change itself would make the second 51 s.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    unit_change(device, client, pd1_output_status=1, pd1_output_voltage=20000)
    advance(device, client, clock, at)
    unit_change(device, client, pd3_output_status=1, pd3_output_voltage=20000)
    early = advance(device, client, clock, sent_at - 0.05 - at)
    due = advance(device, client, clock, 0.1)

    assert secret(early) == []
    assert secret(due) == ["a10131a2020102"]


def test_a2345_power_expert_counts_a_rise_after_its_pass_a_recovery_at_once() -> None:
    """A first rise above 18 V counts a tick after the 300 ms pass that classes it.

    doc Power Expert row: the class is set on the 300 ms pass and only rises,
    while ``secret_features_tick`` counts every 100 ms. C3 rises at 40.95 s:
    classed on the 41.1 s pass, counted at 41.2 s, sent at 42 s. After a dip
    at 50 s, the classed C3 counts again on the next tick: back at 50.75 s,
    counted at 50.8 s and sent at the 50.9 s point (the first overlay moved
    the grid).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    unit_change(
        device,
        client,
        pd1_output_status=1,
        pd1_output_voltage=20000,
        pd3_output_status=1,
        pd3_output_voltage=15000,
    )
    settled = advance(device, client, clock, 40.95)
    unit_change(device, client, pd3_output_voltage=20000)
    unclassed = advance(device, client, clock, 1.0)
    first = advance(device, client, clock, 0.1)
    clock.advance(7.95)
    unit_change(device, client, pd3_output_voltage=15000)
    dipped = advance(device, client, clock, 0.75)
    unit_change(device, client, pd3_output_voltage=20000)
    held = advance(device, client, clock, 0.1)
    recovered = advance(device, client, clock, 0.1)

    assert secret(settled) == secret(unclassed) == secret(dipped) == []
    assert secret(held) == []
    assert secret(first) == secret(recovered) == ["a10131a2020102"]


def test_a2345_a_feature_2_rise_on_a_service_tick_goes_out_on_it() -> None:
    """The 10th rise read on the 10 s tick is served on that tick.

    doc Feature 2 row: the edge loop runs before ``secret_features_tick``'s
    service check. Plugged at 9.95 s; one raised outside the tick waits 1 s.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    for _ in range(9):
        unit_change(device, client, pd1_output_status=1)
        advance(device, client, clock, 0.5)
        unit_change(device, client, pd1_output_status=0)
        advance(device, client, clock, 0.5)
    clock.advance(0.95)
    unit_change(device, client, pd1_output_status=1)
    early = advance(device, client, clock, 0.04)
    due = advance(device, client, clock, 0.02)

    assert secret(early) == []
    assert secret(due) == ["a10131a2020105"]


@pytest.mark.parametrize(
    ("before_fall", "fires"),
    [
        pytest.param(1, True, id="fall at count 1: window restarts at 50.1 s"),
        pytest.param(2, False, id="fall at count 2: window ends 60.6 s on"),
    ],
)
def test_a2345_a_feature_2_fall_at_count_1_restarts_the_window(
    before_fall: int, *, fires: bool
) -> None:
    """A detach that leaves the count at 1 restarts the 60 s window.

    doc Feature 2 row: after any edge with the count at 1, the window resets;
    a detach never counts or fires. ``before_fall`` rises, a detach at 50 s,
    then the rest of 10 rises from 100 s.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    pushes = []

    def cycle() -> None:
        pushes.extend(unit_change(device, client, pd1_output_status=1))
        pushes.extend(advance(device, client, clock, 0.5))
        pushes.extend(unit_change(device, client, pd1_output_status=0))
        pushes.extend(advance(device, client, clock, 0.5))

    for _ in range(before_fall - 1):
        cycle()
    unit_change(device, client, pd1_output_status=1)
    advance(device, client, clock, 50.0 - clock.now())
    unit_change(device, client, pd1_output_status=0)
    advance(device, client, clock, 100.0 - clock.now())
    for _ in range(10 - before_fall):
        cycle()
    pushes.extend(advance(device, client, clock, 2))

    sent = [payload[:14] for cmd, payload in pushes if cmd == "4305"]
    assert sent == (["a10131a2020105"] if fires else [])


@pytest.mark.parametrize(
    ("start", "hold"),
    [
        pytest.param("black", False, id="black"),
        pytest.param("black", True, id="black, inhibit set"),
        pytest.param("transition", False, id="transition screen"),
    ],
)
def test_a2345_a_secret_feature_render_wakes_the_display(
    start: str, *, hold: bool
) -> None:
    """Each service's render (``display_wake_refresh_mark?``) moves 1-3 to 0.

    doc "Display state" row 0: it restarts the ``b0`` count and leaves the
    screen-off inhibit as it is. ``b0`` 1 (30 s); a ``420d`` at 31 s is
    served at 32 s, so the screen goes dark again at 62 s.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(screen_timeout=1)
    advance(device, client, clock, 31.0)
    if start == "transition":
        device.unit_event("knob_click")
    if hold:
        device.unit_event("knob_hold")
    waiting = module.values[DISPLAY]
    command(device, client, 0x20D, [(0xA2, b"\x02\x01")])
    advance(device, client, clock, 1.0)
    woken = (module.values[DISPLAY], module.values.get(SCREENSAVER_INHIBIT, 0))
    advance(device, client, clock, 29.9)
    still_on = module.values[DISPLAY]
    advance(device, client, clock, 0.2)

    assert waiting != 0
    assert woken == (0, int(hold))
    assert still_on == 0
    assert module.values[DISPLAY] != 0


def test_a2345_a_port_change_wakes_the_display_on_the_300ms_pass() -> None:
    """A plug wakes 1-3 to 0 on the 300 ms pass that sees the changed bitmap.

    doc "Display state" row 0 (``port_activity_scan_dispatch`` ->
    ``display_wake_refresh_mark?``) and "Scheduler groups": a plug and unplug
    between two passes wakes nothing; a plug at 31.35 s wakes on the 31.5 s pass.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(screen_timeout=1)
    advance(device, client, clock, 31.0)
    unit_change(device, client, pd3_output_status=1)
    unit_change(device, client, pd3_output_status=0)
    advance(device, client, clock, 0.35)
    glitched = module.values[DISPLAY]
    unit_change(device, client, pd3_output_status=1)
    advance(device, client, clock, 0.1)
    early = module.values[DISPLAY]
    advance(device, client, clock, 0.1)

    assert glitched != 0
    assert early != 0
    assert module.values[DISPLAY] == 0


def test_a2345_420d_of_zero_acks_and_prompts_nothing() -> None:
    """A ``420d`` whose first value byte is 0 is acked with no ``0305``."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    zero = command(device, client, 0x20D, [(0xA2, b"\x02\x00")])
    later = advance(device, client, clock, 2)

    assert (zero, later) == ([("4a0d", "00a10131")], [])


def test_a2345_420d_prompt_goes_out_through_the_secret_feature_service() -> None:
    """A nonzero ``420d``'s ``0305`` (trigger 0) waits for the next grid point.

    doc "Secret Features": one pending feature a grid point, ``420d`` first, so
    it goes out ahead of a Device Expert pending before it.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    prompt = [(0xA2, b"\x02\x01")]

    alone = command(device, client, 0x20D, prompt)
    waiting = advance(device, client, clock, 0.9)
    sent = advance(device, client, clock, 0.1)
    clock.advance(5.5)
    unit_change(
        device,
        client,
        **{
            f"{port}_output_status": 1
            for port in ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")
        },
    )
    behind = command(device, client, 0x20D, prompt)
    first_point = advance(device, client, clock, 0.5)
    next_point = advance(device, client, clock, 1)

    assert alone == behind == [("4a0d", "00a10131")]
    assert waiting == []
    assert sent == [("4305", f"a10131a2020100{device_clock(1)}")]
    assert first_point == [("4305", f"a10131a2020100{device_clock(7)}")]
    assert [payload[:14] for cmd, payload in next_point if cmd == "4305"] == [
        "a10131a2020103"
    ]


@pytest.mark.parametrize(
    ("phase", "at", "gap"),
    [
        pytest.param(0.05, 0.1, 0.95, id="0.95 s, as recorded"),
        pytest.param(0.5, 0.6, 0.9, id="phase 0.5"),
        pytest.param(0.0, 2.75, 0.25, id="phase 0"),
    ],
)
def test_a2345_a_420d_0305_lands_on_the_next_1s_grid_point(
    phase: float, at: float, gap: float
) -> None:
    """A ``420d``'s ``0305`` waits for the next point of the 1 s grid from boot.

    doc "Secret Features": the pass counter resets on every 10th 100 ms tick,
    idle or not, so the points sit at whole seconds from boot (``systick_phase``).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(systick_phase=phase)
    clock.advance(at)

    def secret(pushes: list[tuple[str, str]]) -> list[str]:
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    acked = command(device, client, 0x20D, [(0xA2, b"\x02\x01")])
    early = advance(device, client, clock, gap - 0.01)
    due = advance(device, client, clock, 0.02)

    assert secret(acked) == secret(early) == []
    assert secret(due) == ["a10131a2020100"]


@pytest.mark.parametrize(
    ("layout", "msgtype", "mqtt"),
    [
        pytest.param(A2345Layout(), 0x302, ("64",), id="0302 to param_info"),
        pytest.param(A2345Layout(), 0x303, ("34",), id="0303 to device update"),
        pytest.param(A2345Layout(), 0x304, ("64", "44"), id="0304 sent twice"),
        pytest.param(A2345Layout(), 0x308, ("64",), id="A2345 0308"),
        pytest.param(A91B2Layout(), 0x308, ("34",), id="A91B2 0308"),
        pytest.param(A2345Layout(), 0x300, ("32",), id="0300 to state_info only"),
    ],
)
def test_a_prime_routes_a_push_by_its_link_state(
    layout: A2345Layout | A91B2Layout, msgtype: int, mqtt: tuple[str, ...]
) -> None:
    """BLE alone: ``a1 31`` on the link; WiFi up: MQTT only (doc "Push routing")."""
    payload = bytes.fromhex("a10131a2020100fe0503933cd66a")
    wifi = {"_wifi_up": True}

    copies = layout.mqtt_routed(msgtype, payload, wifi)

    assert layout.sends(msgtype, {}) == (msgtype != 0x300)
    assert not layout.sends(msgtype, wifi)
    assert layout.mqtt_routed(msgtype, payload, {}) == ()
    assert [(push, body.hex()) for push, body in copies] == [
        (msgtype, f"a101{route}a2020100fe0503933cd66a") for route in mqtt
    ]


def test_a_prime_push_the_routing_table_doesnt_name_stays_on_ble() -> None:
    """``0406`` (literal ``31`` on any link) isn't routed: BLE always, no MQTT copy."""
    layout = A2345Layout()
    payload = bytes.fromhex("a10131fe0503933cd66a")

    assert layout.sends(0x406, {"_wifi_up": True})
    assert layout.mqtt_routed(0x406, payload, {"_wifi_up": True}) == ()


@pytest.mark.parametrize(
    ("model", "version"),
    [
        pytest.param(A2345, "4408", id="A2345 v2.1.1.6"),
        pytest.param(A91B2, "6404", id="A91B2 v1.1.2.4"),
    ],
)
def test_a_prime_wifi_coming_up_publishes_0301(
    model: type[A2345 | A91B2], version: str
) -> None:
    """``0301``: the firmware version, published at ``64``; nothing on BLE.

    As recorded: prime-a2345-a91b2.md "MCU pushes" ``0301`` row.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)

    on_ble = pushed_by(device, client, lambda: device.set_wifi(True))

    assert on_ble == []
    assert published(device) == [(0x301, f"a10164a20302{version}{device_clock(0)}")]


def test_a_prime_push_leaves_ble_for_mqtt_while_wifi_is_up() -> None:
    """With WiFi up the change pushes publish at ``64``, ``0303`` at ``34``.

    The replies stay on BLE (doc "Push routing"); with WiFi down again the
    pushes come back to BLE and nothing more is published.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")

    device.set_wifi(True)
    switched = command(
        device, client, 0x207, [(0xA2, b"\x01\x01"), (0xA3, b"\x01\x00")]
    )
    command(device, client, 0x209, [port, arm(1)])
    expired = advance(device, client, clock, 2)
    stream = command(device, client, 0x20B, []), advance(device, client, clock, 5)
    while_up = published(device)
    device.set_wifi(False)
    back = command(device, client, 0x207, [port, (0xA3, b"\x01\x01")])

    assert [cmd for cmd, _ in switched] == ["4a07"]
    assert expired == []
    assert ([cmd for cmd, _ in stream[0]], stream[1]) == (["4a0b"], [])
    assert [cmd for cmd, _ in back] == ["4a07"]
    assert published(device) == while_up
    assert [(msgtype, payload[:6]) for msgtype, payload in while_up] == [
        (0x301, "a10164"),
        (0x307, "a10164"),
        (0x307, "a10164"),
        (0x302, "a10164"),
        *[(0x303, "a10134")] * 6,
    ]


def test_a_prime_plug_with_wifi_up_publishes_0304_twice() -> None:
    """``0304`` goes once per cloud lane, ``64`` then ``44`` (doc ``0304`` row)."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_wifi(True)

    on_ble = pushed_by(device, client, lambda: device.plug(4, 0x05AC, 0x7117))

    mask = "a203030a00"  # C2 recorded active, C4 plugged
    assert on_ble == []
    assert published(device)[1:] == [
        (0x304, f"a10164{mask}{device_clock(0)}"),
        (0x304, f"a10144{mask}{device_clock(0)}"),
    ]


@pytest.mark.parametrize(
    ("model", "ports", "stats"),
    [
        pytest.param(
            A2345,
            {"pd1": (1, 1987), "pd2": (1, 15), "pd4": (1, 10)},
            "a20e040bbc070a0000000a0000000000a30302d007",
            id="A2345 rounds down to 0.1 W, as recorded",
        ),
        pytest.param(
            A91B2,
            {"pd3": (1, 205)},
            "a20e040400000000cd00000000000000a30302cd00",
            id="A91B2 unrounded",
        ),
    ],
)
def test_a_prime_publishes_0300_every_10_minutes_while_wifi_is_up(
    model: type[A2345 | A91B2], ports: dict[str, tuple[int, int]], stats: str
) -> None:
    """``0300`` at ``32``: active ports, six port powers, the total, UTC hours.

    doc "``state_info`` (MQTT)": ``a10132a20e040bbc070a0000000a0000000000a30302d007
    a40201fc`` (2026-06-01 17:51:02); the A91B2 sends its power unrounded.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(
        **{
            f"{port}_output_{field}": ports.get(port, (0, 0))[index]
            for port in ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")
            for index, field in enumerate(("status", "power"))
        }
    )
    device.set_wifi(True)

    advance(device, client, clock, 599)
    early = published(device)[1:]
    on_ble = advance(device, client, clock, 1) + advance(device, client, clock, 600)
    device.set_wifi(False)
    advance(device, client, clock, 1200)

    assert (early, on_ble) == ([], [])
    assert published(device)[1:] == [
        (0x300, f"a10132{stats}a40201fc{device_clock(600)}"),
        (0x300, f"a10132{stats}a40201fc{device_clock(1200)}"),
    ]


@pytest.mark.parametrize(
    ("holiday", "country", "sent"),
    [
        pytest.param(True, b"\x04us", True, id="holiday switch on, country US"),
        pytest.param(False, b"\x04us", False, id="holiday switch off"),
        pytest.param(True, None, False, id="no country"),
    ],
)
def test_a2345_publishes_0312_every_minute_with_the_holiday_switch_on(
    holiday: bool,  # noqa: FBT001  # a parameter
    country: bytes | None,
    sent: bool,  # noqa: FBT001  # a parameter
) -> None:
    """``0312`` at ``32``: the ``420a`` country and the UTC offset in hours.

    Gates: holiday switch on, WiFi up, a country (doc "Periodic pushes"); as
    recorded, ``a10132a203045553a30503fcfffffffe0503171fd86a`` (2026-10-20).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(holiday_screensaver_switch=holiday)
    if country is not None:
        command(device, client, 0x20A, [(0xA2, country), (0xA3, b"\x04" + ACCOUNT)])
    device.set_wifi(True)

    advance(device, client, clock, 60)
    advance(device, client, clock, 60)

    expected = [
        f"a10132a203045553a30503fcffffff{device_clock(seconds)}"
        for seconds in (60, 120)
    ]
    assert [p for m, p in published(device) if m == 0x312] == (expected if sent else [])


#: The recorded self-fetched theme: Rhythmic Hue, catalog id 496957652.
RHYTHMIC_HUE = 0x1D9EF8D4


def test_a2345_fetches_a_network_theme_itself_with_wifi_up() -> None:
    """``0311`` 2, 3, then 1 at 0.38, 1.73 and 8.6 s; the theme then shows.

    doc "``4311`` image state": 2026-09-08 16:17:43 MQTT ``a10164a20504d4f89e1d
    a3020102a4020103`` (2), 44.809 (3), 51.694 (1); the app sends nothing.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_wifi(True)

    selected = command(device, client, 0x205, theme_select(IMAGE, RHYTHMIC_HUE))
    states = []
    for seconds in (0.37, 0.02, 1.33, 0.02, 6.85, 0.02):
        advance(device, client, clock, seconds)
        states.append([p[26:28] for m, p in published(device) if m == 0x311])
    again = command(device, client, 0x205, theme_select(IMAGE, RHYTHMIC_HUE))

    values = device.negotiated_module.values
    assert [cmd for cmd, _ in selected] == [cmd for cmd, _ in again] == ["4a05"]
    assert states == [
        [],
        ["02"],
        ["02"],
        ["02", "03"],
        ["02", "03"],
        ["02", "03", "01"],
    ]
    assert published(device)[1][1] == "a10164a20504d4f89e1da3020102a4020103"
    assert (values["screen_saver_theme"], values["screen_saver_theme_id"]) == (
        3,
        RHYTHMIC_HUE,
    )


def test_a2345_theme_request_during_a_fetch_answers_0() -> None:
    """``4205`` while a fetch is busy: ``0311`` 0 with the request's id and kind.

    ``push_0311(a3, 0, a2 & 0x3f)``; the fetch still completes.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_wifi(True)

    command(device, client, 0x205, theme_select(IMAGE, RHYTHMIC_HUE))
    advance(device, client, clock, 1)
    command(device, client, 0x205, theme_select(IMAGE, THEME_ID, theme=0x85))
    advance(device, client, clock, 8)

    assert [
        (p[6:20], p[26:28], p[34:36]) for m, p in published(device) if m == 0x311
    ] == [
        ("a20504d4f89e1d", "02", "03"),
        ("a205048eb81a0e", "00", "05"),
        ("a20504d4f89e1d", "03", "03"),
        ("a20504d4f89e1d", "01", "03"),
    ]
    assert device.negotiated_module.values["screen_saver_theme_id"] == RHYTHMIC_HUE


def test_a2345_4212_fetches_the_holiday_image_with_wifi_up() -> None:
    """``4212``: no reply, no ``0311``; the record lands with the ``420a`` country.

    The fetch sends no message, so 8.6 s (the recorded network self-fetch) stands
    in for when the image lands (doc ``4212`` row, "Theme upload").
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    command(device, client, 0x20A, [(0xA2, b"\x04us"), (0xA3, b"\x04" + ACCOUNT)])
    device.set_wifi(True)
    now = module.rtc
    window = holiday_schedule(RHYTHMIC_HUE, now, now + 60)

    scheduled = command(device, client, 0x212, window)
    early = advance(device, client, clock, 8.5), module.values.get("_holiday_id")
    landed = advance(device, client, clock, 0.2)

    record = {
        name: module.values[name] for name in module.values if "_holiday_" in name
    }
    assert (scheduled, early, landed) == ([], ([], None), [])
    assert published(device)[1:] == []
    assert record == {
        "_holiday_id": RHYTHMIC_HUE,
        "_holiday_crc": 7,
        "_holiday_a4": 8,
        "_holiday_start": local_datetime(now),
        "_holiday_end": local_datetime(now + 60),
        "_holiday_url": HOLIDAY_URL,
        "_holiday_country": b"US",
        "_holiday_fetches": 1,
    }


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(None, id="WiFi down: dropped"),
        pytest.param(
            [(0xA2, b"\x03" + ui(4).build(RHYTHMIC_HUE))], id="a field missing"
        ),
        pytest.param([(0xA3, b"\x03" + ui(4).build(7))], id="no a2"),
    ],
)
def test_a2345_4212_that_fetches_nothing(
    fields: list[tuple[int, bytes]] | None,
) -> None:
    """With WiFi down, or a field missing, nothing is stored, now or later."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    if fields is not None:
        device.set_wifi(True)

    window = holiday_schedule(RHYTHMIC_HUE, module.rtc, module.rtc + 60)
    replies = command(device, client, 0x212, window if fields is None else fields)
    advance(device, client, clock, 20)

    assert replies == []
    assert [name for name in module.values if "_holiday_" in name] == []


@pytest.mark.parametrize(
    "value", [pytest.param(7, id="int"), pytest.param(True, id="bool")]
)
def test_a2345_stored_bytes_refuses_a_value_of_another_type(value: int) -> None:
    """A hidden record value read as bytes that isn't raises, naming it."""
    with pytest.raises(TypeError, match="_holiday_url is a bytes value"):
        stored_bytes({"_holiday_url": value}, "_holiday_url")


def test_a2345_4212_a2_0_erases_the_holiday_record() -> None:
    """``a2 = 0`` erases the slot (``theme_slot_erase(0xff0000)``), WiFi up or down."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_wifi(True)
    window = holiday_schedule(RHYTHMIC_HUE, module.rtc, module.rtc + 60)
    command(device, client, 0x212, window)
    advance(device, client, clock, 9)
    device.set_wifi(False)

    erased = command(device, client, 0x212, [(0xA2, b"\x03" + ui(4).build(0))])

    assert erased == []
    assert {
        name: module.values[name]
        for name in module.values
        if "_holiday_" in name and name != "_holiday_fetches"
    } == {
        "_holiday_id": 0,
        "_holiday_crc": 0,
        "_holiday_a4": 0,
        "_holiday_start": b"",
        "_holiday_end": b"",
        "_holiday_url": b"",
        "_holiday_country": b"",
    }


def test_a2345_4212_a2_0_mid_fetch_erases_but_the_fetch_still_lands() -> None:
    """The erase doesn't cancel a fetch in flight: it lands and writes the record.

    doc "Theme upload": the busy flag, kind and slot base stay set, a race.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_wifi(True)
    window = holiday_schedule(RHYTHMIC_HUE, module.rtc, module.rtc + 60)
    command(device, client, 0x212, window)
    advance(device, client, clock, 4)

    command(device, client, 0x212, [(0xA2, b"\x03" + ui(4).build(0))])
    erased = module.values["_holiday_id"]
    advance(device, client, clock, 5)

    assert (erased, module.values["_holiday_id"]) == (0, RHYTHMIC_HUE)


def test_a2345_holiday_fetch_lands_across_a_wifi_drop_with_no_0311() -> None:
    """WiFi down mid-fetch: the module resumes at its offset, and it still lands.

    doc "Theme upload"; the retry budget ending it with status 3 isn't modeled.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_wifi(True)
    window = holiday_schedule(RHYTHMIC_HUE, module.rtc, module.rtc + 60)
    command(device, client, 0x212, window)

    on_ble = advance(device, client, clock, 3)
    device.set_wifi(False)
    on_ble += advance(device, client, clock, 3)
    device.set_wifi(True)
    on_ble += advance(device, client, clock, 3)

    assert module.values["_holiday_id"] == RHYTHMIC_HUE
    assert [cmd for cmd, _ in on_ble if cmd.endswith("311")] == []
    assert [m for m, _ in published(device) if m == 0x311] == []


def test_a2345_holiday_theme_shows_inside_its_window_on_the_local_rtc() -> None:
    """``4a02``: ``a4`` the fetch URL and ``a5 01`` from start to end, inclusive.

    Shown with the holiday switch on and the record's country the stored one
    (doc "``4a02`` screensaver reply", ``holiday_window_active``), as the
    screensaver evaluates the flag each tick (doc "Display state").
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)
    command(device, client, 0x20A, [(0xA2, b"\x04us"), (0xA3, b"\x04" + ACCOUNT)])
    device.set_wifi(True)
    start = module.rtc + 10
    command(device, client, 0x212, holiday_schedule(RHYTHMIC_HUE, start, start + 10))
    shown = []

    for seconds in (9, 1, 10, 1):
        advance(device, client, clock, seconds)
        fields = screensaver(device)
        shown.append((fields["a4"]["value"], "a5" in fields))

    recorded = b"1234567890123456"
    assert shown == [
        (recorded, False),
        (HOLIDAY_URL, True),
        (HOLIDAY_URL, True),
        (recorded, False),
    ]


def test_a2345_holiday_theme_reply_carries_a5_after_a4() -> None:
    """While shown: ``a2 c3 · a3 · a4 <url> · a5 01 · a6 · fe``, in that order."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)
    command(device, client, 0x20A, [(0xA2, b"\x04us"), (0xA3, b"\x04" + ACCOUNT)])
    device.set_wifi(True)
    window = holiday_schedule(1, module.rtc - 600, module.rtc + 600)
    command(device, client, 0x212, window)
    advance(device, client, clock, 9)

    fields = screensaver(device)

    assert list(fields)[1:] == ["a2", "a3", "a4", "a5", "a6", "fe"]
    assert (fields["a2"]["value"].hex(), fields["a5"]["value"].hex()) == ("c3", "01")


def test_a2345_theme_report_carries_the_holiday_record_while_it_shows() -> None:
    """``030b`` adds ``a4 ... 04 <holiday theme record>`` while the holiday shows.

    prime-a2345-a91b2.md "Screen reports" (``push_030b`` 0x00023194): the
    record is the holiday slot's (``0xfffe00``), the string ``4a02``'s ``a4``
    carries; outside the window ``030b`` keeps ``a2`` / ``a3`` only.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)
    command(device, client, 0x20A, [(0xA2, b"\x04us"), (0xA3, b"\x04" + ACCOUNT)])
    device.set_wifi(True)
    window = holiday_schedule(1, module.rtc - 600, module.rtc + 600)
    command(device, client, 0x212, window)
    advance(device, client, clock, 9)
    device.set_wifi(False)

    device.set_values(screen_saver_theme=1)
    shown = advance(device, client, clock, REPORT_WAIT)
    advance(device, client, clock, 600)
    device.set_values(screen_saver_theme=2)
    after = advance(device, client, clock, REPORT_WAIT)

    fields = Parameters.parse(bytes.fromhex(shown[0][1]))
    assert [cmd for cmd, _ in shown] == ["430b"]
    assert list(fields) == ["a1", "a2", "a3", "a4", "fe"]
    assert (fields["a4"]["type"], fields["a4"]["value"]) == (0x04, HOLIDAY_URL)
    assert list(Parameters.parse(bytes.fromhex(after[0][1]))) == [
        "a1",
        "a2",
        "a3",
        "fe",
    ]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("country", id="420a country changed"),
        pytest.param("switch", id="holiday switch off"),
        pytest.param("erase", id="4212 a2 0"),
    ],
)
def test_a2345_holiday_theme_hides(change: str) -> None:
    """A later ``420a`` country, the holiday switch off, or the erase hides it.

    At the screensaver's next evaluation, one display tick on.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)
    command(device, client, 0x20A, [(0xA2, b"\x04us"), (0xA3, b"\x04" + ACCOUNT)])
    device.set_wifi(True)
    window = holiday_schedule(1, module.rtc - 600, module.rtc + 600)
    command(device, client, 0x212, window)
    advance(device, client, clock, 9)
    before = "a5" in screensaver(device)

    if change == "country":
        command(device, client, 0x20A, [(0xA2, b"\x04gb"), (0xA3, b"\x04" + ACCOUNT)])
    elif change == "switch":
        device.set_values(holiday_screensaver_switch=False)
    else:
        command(device, client, 0x212, [(0xA2, b"\x03" + ui(4).build(0))])
    still = "a5" in screensaver(device)
    advance(device, client, clock, DISPLAY_TICK)

    assert (before, still, "a5" in screensaver(device)) == (True, True, False)


def test_a2345_display_blanks_at_its_b0_timeout_then_shows_the_screensaver() -> None:
    """Display state 0 -> 2 at the ``b0`` timeout, then 1 at the next tick.

    prime-a2345-a91b2.md "Display state": ``b0`` 1 = 300 ticks (30 s); the
    recorded window (every day) and clock switch let black move on.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(screen_timeout=1)
    states = []

    for seconds in (29.9, 0.1, DISPLAY_TICK):
        advance(device, client, clock, seconds)
        states.append(module.values.get(DISPLAY, 0))

    assert states == [0, 2, 1]


@pytest.mark.parametrize(
    "written", [pytest.param(1, id="unchanged"), pytest.param(2, id="changed")]
)
def test_a2345_any_b0_write_on_the_unit_restarts_the_count(written: int) -> None:
    """``screen_timeout_set`` zeroes the blank counter on any valid ``b0`` (< 5).

    prime-a2345-a91b2.md "Display state", state 0: even an unchanged write.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(screen_timeout=1)
    advance(device, client, clock, 20)

    device.set_values(screen_timeout=written)
    advance(device, client, clock, 29.9)

    assert module.values.get(DISPLAY, 0) == 0


def test_a2345_a_b0_of_0_never_blanks() -> None:
    """``b0`` 0 is never: the display stays on."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(screen_timeout=0)

    advance(device, client, clock, 86400)

    assert device.negotiated_module.values.get(DISPLAY, 0) == 0


#: App setting commands acked ``00`` whose handlers call ``display_wake_to_active?``
#: (prime-a2345-a91b2.md): ``4206``-``4209`` also on a change their setter drops.
#: An off-time half, Sunday 23:26.
SUNDAY_2326 = (0xA4, b"\x03\x01\x17\x1a\x40")
WAKING_COMMANDS = [
    pytest.param(0x203, [(0xA2, b"\x01\x01")], id="4203"),
    pytest.param(0x204, [(0xA2, b"\x01\x32")], id="4204"),
    pytest.param(0x206, [(0xA2, b"\x01\x01")], id="4206"),
    pytest.param(0x206, [(0xA2, b"\x01\x05")], id="4206 mode 5 without a3 a4"),
    pytest.param(0x207, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x00")], id="4207"),
    pytest.param(0x207, [(0xA2, b"\x01\x05"), (0xA3, b"\x01\x00")], id="4207 port 5"),
    pytest.param(
        0x208, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x00"), SUNDAY_2326], id="4208"
    ),
    pytest.param(
        0x208, [(0xA2, b"\x01\x00"), (0xA3, b"\x01\x02"), SUNDAY_2326], id="4208 a3 2"
    ),
    pytest.param(0x209, [(0xA2, b"\x01\x00"), arm(600)], id="4209"),
    pytest.param(0x209, [(0xA2, b"\x01\x05"), arm(600)], id="4209 unknown port"),
    pytest.param(0x20E, [(0xA2, b"\x01\x00")], id="420e"),
    pytest.param(0x213, screensaver_window((0, 0), (0, 0), 0x7F), id="4213"),
    pytest.param(0x223, [(0xA2, b"\x01\x01")], id="4223"),
]


@pytest.mark.parametrize(("msgtype", "fields"), WAKING_COMMANDS)
def test_a2345_an_app_setting_command_wakes_the_display_and_restarts_b0(
    msgtype: int, fields: list[tuple[int, bytes]]
) -> None:
    """From the screensaver to 0, and the ``b0`` count starts over there."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)

    command(device, client, msgtype, fields)
    woken = module.values[DISPLAY]
    advance(device, client, clock, 29.8)
    still_on = module.values[DISPLAY]
    advance(device, client, clock, 0.5)

    assert (woken, still_on, module.values[DISPLAY]) == (0, 0, 1)


@pytest.mark.parametrize(
    ("msgtype", "fields"),
    [
        pytest.param(0x203, [], id="4203 refused 04"),
        pytest.param(0x207, [(0xA2, b"\x01\x00")], id="4207 refused 04"),
        pytest.param(0x205, [(0xA2, b"\x01\x82")], id="4205 with the inhibit 0"),
        pytest.param(0x20F, [(0xA2, b"\x01\x01")], id="420f"),
        pytest.param(0x202, [], id="4202 read"),
    ],
)
def test_a2345_other_commands_and_unit_changes_leave_the_screensaver(
    msgtype: int, fields: list[tuple[int, bytes]]
) -> None:
    """A ``04``, a handler that doesn't call the wake, and an on-unit setting.

    ``cmd205`` wakes only on a clock-on ``a2`` while the screen-off inhibit is 1
    (the knob hold's).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)

    command(device, client, msgtype, fields)
    device.set_values(screen_brightness=40)
    advance(device, client, clock, 1)

    assert module.values[DISPLAY] == 1


def test_a2345_a_knob_hold_keeps_black_from_the_screensaver() -> None:
    """``knob_hold_screen_off?`` sets the screen-off inhibit, which holds 2 from 1.

    prime-a2345-a91b2.md "Display state" (state 2) and the knob-hold bullet.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module

    device.unit_event("knob_hold")
    into_screensaver(device, client, clock)
    advance(device, client, clock, 1)

    assert (module.values[DISPLAY], module.values[SCREENSAVER_INHIBIT]) == (2, 1)


def test_a2345_an_app_setting_wake_clears_the_screen_off_inhibit() -> None:
    """``display_wake_to_active?`` clears it: black moves on again after its count."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.unit_event("knob_hold")
    into_screensaver(device, client, clock)

    command(device, client, 0x204, [(0xA2, b"\x01\x32")])
    woken = (module.values[DISPLAY], module.values[SCREENSAVER_INHIBIT])
    advance(device, client, clock, SCREENSAVER_AFTER)

    assert woken == (0, 0)
    assert module.values[DISPLAY] == 1


@pytest.mark.parametrize(
    ("a2", "shown"),
    [
        pytest.param(0x82, 0, id="clock on: woken"),
        pytest.param(0x02, 2, id="clock off: left black"),
    ],
)
def test_a2345_4205_clock_on_wakes_while_the_inhibit_is_1(a2: int, shown: int) -> None:
    """``cmd205`` calls the wake only on a clock-on ``a2`` while the inhibit is 1.

    prime-a2345-a91b2.md "Display state" wake bullet (0x00043b5c).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.unit_event("knob_hold")
    into_screensaver(device, client, clock)

    command(device, client, 0x205, [(0xA2, bytes([1, a2]))])

    assert module.values[DISPLAY] == shown
    assert module.values[SCREENSAVER_INHIBIT] == (0 if shown == 0 else 1)


def test_a2345_a_knob_click_shows_the_transition_screen_until_its_timeout() -> None:
    """A click (events 1 / 0xb) -> 3, where ``b0`` isn't counted; > 0x707 ticks -> 0.

    prime-a2345-a91b2.md "Display state" state 3: clicked at 10 s, the 30 s
    blank doesn't come; at 190 s (1 800 ticks on) it is back to 0, its count
    started over. The screen-request exit isn't modeled.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(screen_timeout=1)
    advance(device, client, clock, 10)

    device.unit_event("knob_click")
    states = [module.values[DISPLAY]]
    for seconds in (25, 154.9, 0.2, 29.7, 0.5):
        advance(device, client, clock, seconds)
        states.append(module.values[DISPLAY])

    assert states == [3, 3, 3, 0, 0, 1]


def test_a2345_an_app_wake_ends_the_transition_screen_early() -> None:
    """A wake in state 3 puts it on; the 180 s timeout then changes nothing.

    Woken at 1 s, the display blanks at 31 s and shows the screensaver, which
    the stale timeout at 180 s leaves.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(screen_timeout=1)
    device.unit_event("knob_click")
    advance(device, client, clock, 1)

    command(device, client, 0x204, [(0xA2, b"\x01\x32")])
    woken = module.values[DISPLAY]
    advance(device, client, clock, 180)

    assert (woken, module.values[DISPLAY]) == (0, 1)


def test_a2345_a_bound_unit_leaves_the_transition_screen_on_the_next_tick() -> None:
    """Bound, the screen-request snapshot reads 5, so state 3 ends a tick on.

    prime-a2345-a91b2.md "Display state" state 3: the bind handler keeps 5.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    exchange(module, client, 0x023, [(0xA2, ACCOUNT)])

    device.unit_event("knob_click")
    shown = module.values[DISPLAY]
    advance(device, client, clock, 0.15)

    assert (shown, module.values[DISPLAY]) == (3, 0)


def test_a2345_a_420a_while_unbound_ends_the_transition_screen() -> None:
    """``cmd20a`` requests screen 4 while unbound; state 3 reads it a tick on."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.unit_event("knob_click")
    advance(device, client, clock, 10)
    waiting = module.values[DISPLAY]

    command(device, client, 0x20A, [(0xA2, b"\x02US"), (0xA3, b"\x04" + ACCOUNT)])
    advance(device, client, clock, 0.15)

    assert (waiting, module.values[DISPLAY]) == (3, 0)


def test_a2345_a_bind_ends_the_transition_screen_on_the_next_tick() -> None:
    """Unbound in state 3, a ``0x023`` bind makes the snapshot read 5 a tick on."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.unit_event("knob_click")
    advance(device, client, clock, 10)
    waiting = module.values[DISPLAY]

    exchange(module, client, 0x023, [(0xA2, ACCOUNT)])
    advance(device, client, clock, 0.15)

    assert (waiting, module.values[DISPLAY]) == (3, 0)


def test_a2345_a_420a_while_bound_leaves_the_screen_request() -> None:
    """``cmd20a`` requests screen 4 only while unbound; a bound unit keeps 5."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    exchange(module, client, 0x023, [(0xA2, ACCOUNT)])

    command(device, client, 0x20A, [(0xA2, b"\x02US"), (0xA3, b"\x04" + ACCOUNT)])

    assert SCREEN_REQUEST not in module.values


@pytest.mark.parametrize(
    ("phase", "tick"),
    [(0.0, 0.07), (0.05, 0.02)],
    ids=["phase 0: tick at 30.3 s", "phase 0.05: tick at 30.25 s"],
)
def test_a2345_knob_activity_wakes_on_the_next_100ms_tick(
    phase: float, tick: float
) -> None:
    """``knob_activity_wake?``: from black with the inhibit, to 0 a tick on.

    The inhibit clears and the ``b0`` count starts over at the tick.

    prime-a2345-a91b2.md "Knob activity flag": the flag is consumed on the
    ``tick_100ms_group`` tick; the knob-hold bullet: it clears the inhibit.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(systick_phase=phase)
    device.unit_event("knob_hold")
    into_screensaver(device, client, clock)
    advance(device, client, clock, 0.03)

    device.unit_event("knob_activity")
    at_once = module.values[DISPLAY]
    advance(device, client, clock, tick - 0.01)
    before_tick = module.values[DISPLAY]
    advance(device, client, clock, 0.02)
    woken = (module.values[DISPLAY], module.values[SCREENSAVER_INHIBIT])
    advance(device, client, clock, 29.97)
    still_on = module.values[DISPLAY]
    advance(device, client, clock, 0.2)

    assert (at_once, before_tick, woken) == (2, 2, (0, 0))
    assert (still_on, module.values[DISPLAY]) == (0, 1)


def test_a2345_knob_activity_while_on_without_the_inhibit_changes_nothing() -> None:
    """``knob_activity_wake?`` acts from states 1-3 or with the inhibit set only."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(screen_timeout=1)
    advance(device, client, clock, 20)

    device.unit_event("knob_activity")
    advance(device, client, clock, 10.2)

    assert module.values[DISPLAY] == 1


def test_a2345_a_full_knob_press_wakes_at_press_down_then_the_click_wins() -> None:
    """Press-down wakes 1 to 0 on the next tick; the click on release goes to 3.

    prime-a2345-a91b2.md "Knob activity flag": LVGL fires the click on release.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)

    device.unit_event("knob_activity")
    advance(device, client, clock, 0.15)
    pressed = module.values[DISPLAY]
    device.unit_event("knob_click")
    advance(device, client, clock, 0.3)

    assert (pressed, module.values[DISPLAY]) == (0, 3)


def test_a2345_the_clock_switch_moves_the_screensaver_to_black_and_back() -> None:
    """Clock switch off in 1 -> 2; on again, black meets the gate -> 1."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)

    device.set_values(screen_saver_switch=False)
    advance(device, client, clock, DISPLAY_TICK)
    off = module.values[DISPLAY]
    device.set_values(screen_saver_switch=True)
    advance(device, client, clock, DISPLAY_TICK)

    assert (off, module.values[DISPLAY]) == (2, 1)


def test_a2345_a_clock_switch_back_on_before_the_tick_keeps_the_screensaver() -> None:
    """The move to black reads the switch at its tick: on again, it stays 1."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)

    device.set_values(screen_saver_switch=False)
    device.set_values(screen_saver_switch=True)
    advance(device, client, clock, DISPLAY_TICK)

    assert module.values[DISPLAY] == 1


#: 4213 windows against the RTC (Sunday 23:25:15, then Monday 00:25:15 an hour
#: on), and the phase each reads: 2 inside, 1 outside. Weekdays bit 0 Monday,
#: bit 6 Sunday.
WINDOW_PHASES = [
    pytest.param((23, 0), (23, 30), 0x40, 0, 2, id="inside Sunday"),
    pytest.param((23, 0), (23, 25), 0x40, 0, 1, id="end exclusive"),
    pytest.param((23, 25), (23, 30), 0x40, 0, 2, id="start inclusive"),
    pytest.param((23, 0), (23, 30), 0x3F, 0, 1, id="Sunday unset"),
    pytest.param((22, 0), (2, 0), 0x40, 0, 2, id="wrap, before midnight"),
    pytest.param((22, 0), (2, 0), 0x20, 0, 1, id="wrap, today's bit unset"),
    pytest.param((22, 0), (2, 0), 0x40, 3600, 2, id="wrap, after midnight"),
    pytest.param((22, 0), (2, 0), 0x01, 3600, 1, id="wrap, yesterday's bit unset"),
    pytest.param((22, 0), (0, 25), 0x40, 3600, 2, id="wrap, end minute included"),
    pytest.param((22, 0), (0, 24), 0x40, 3600, 1, id="wrap, a minute past the end"),
    pytest.param((0, 0), (0, 0), 0x7F, 0, 2, id="start = end, all day"),
    pytest.param((23, 0), (23, 30), 0, 0, 2, id="once, inside"),
    pytest.param((23, 30), (23, 40), 0, 0, 1, id="once, ahead"),
    pytest.param((23, 30), (23, 40), 0, 300, 2, id="once, reached"),
    pytest.param((23, 0), (23, 30), 0, 86400, 1, id="once, past"),
    pytest.param((23, 0), (23, 20), 0, 0, 1, id="once, ended today"),
    pytest.param((23, 0), (23, 20), 0, 84905, 2, id="once, dated tomorrow"),
]


@pytest.mark.parametrize(("start", "end", "weekdays", "later", "phase"), WINDOW_PHASES)
def test_a2345_the_4213_window_phase(
    start: tuple[int, int],
    end: tuple[int, int],
    weekdays: int,
    later: int,
    phase: int,
) -> None:
    """prime-a2345-a91b2.md "``4213`` window": minute resolution, start inclusive.

    End exclusive, wrapping past midnight; a once window (weekdays 0) is dated.
    Read after the first evaluation, a pass on.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    advance(device, client, clock, WINDOW_FIRST_WAIT)

    command(device, client, 0x213, screensaver_window(start, end, weekdays))
    advance(device, client, clock, later + PASS_WAIT)

    assert device.negotiated_module.values[WINDOW_PHASE] == phase


@pytest.mark.parametrize(
    ("start", "end", "dates", "phase"),
    [
        pytest.param((23, 30), (23, 40), (20260913, 20260913), 1, id="before both"),
        pytest.param(
            (23, 30), (1, 0), (20260913, 20260914), 1, id="before both, end <= start"
        ),
        pytest.param((9, 0), (12, 0), (20260914, 20260914), 1, id="after both"),
        pytest.param(
            (12, 0), (9, 0), (20260913, 20260914), 2, id="after both, start >= end"
        ),
        pytest.param((23, 0), (23, 40), (20260913, 20260913), 2, id="in it"),
        pytest.param((23, 30), (10, 0), (20260913, 20260914), 1, id="before the start"),
    ],
)
def test_a2345_a_once_window_is_dated_and_phased_by_now_against_its_ends(
    start: tuple[int, int], end: tuple[int, int], dates: tuple[int, int], phase: int
) -> None:
    """``screensaver_window_store``: the doc's four cases, at Sunday 2026-09-13 23:25.

    prime-a2345-a91b2.md "``4213`` window dates": the dates as ``b4a0`` /
    ``b4aa`` (year, month, day); a once window's phase set at the store.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    advance(device, client, clock, WINDOW_FIRST_WAIT)

    command(device, client, 0x213, screensaver_window(start, end, 0))
    advance(device, client, clock, DISPLAY_TICK)

    stored = (module.values[WINDOW_START_DATE], module.values[WINDOW_END_DATE])
    assert (stored, module.values[WINDOW_PHASE]) == (dates, phase)


@pytest.mark.parametrize(
    ("start", "end", "phase", "clock_on"),
    [
        pytest.param((23, 55), (0, 10), 2, False, id="an end on Oct 32 has passed"),
        pytest.param((23, 30), (23, 45), 1, True, id="a start on Oct 32 is skipped"),
    ],
)
def test_a2345_a_once_window_rolled_past_month_end_reads_passed_from_the_1st(
    start: tuple[int, int], end: tuple[int, int], phase: int, *, clock_on: bool
) -> None:
    """Every roll is a plain day + 1, and ``date_cmp?`` compares year, month, day.

    prime-a2345-a91b2.md "``4213`` window dates": stored at Oct 31 23:50, a
    roll lands on (2026, 10, 32), which is passed at Nov 1 00:00. An end found
    passed turns the clock off with the phase left inside; a start found
    passed is skipped for good.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    advance(device, client, clock, WINDOW_FIRST_WAIT)
    local = int(datetime(2026, 10, 31, 23, 50, tzinfo=UTC).timestamp())
    moved = ui(4).build(local + ui(4).parse(UTC_OFFSET))
    exchange(module, client, 0x022, [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)])

    command(device, client, 0x213, screensaver_window(start, end, 0))
    advance(device, client, clock, 11 * 60)

    assert (module.values[WINDOW_START_DONE], module.values[WINDOW_PHASE]) == (1, phase)
    assert bool(module.values.get("screen_saver_switch", True)) is clock_on


def test_a2345_the_window_is_first_evaluated_on_the_10th_pass() -> None:
    """``clock_holiday_autoscreen?``: first on the 10th pass with the RTC valid.

    prime-a2345-a91b2.md "``4213`` window": valid from boot, that is 1.5 s.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module

    advance(device, client, clock, 1.49)
    unevaluated = WINDOW_PHASE not in module.values
    advance(device, client, clock, 0.02)

    assert unevaluated
    assert module.values[WINDOW_PHASE] == 2


def test_a2345_a_window_edge_lands_on_the_first_pass_in_its_minute() -> None:
    """The window is evaluated on every 150 ms pass, so its start lands on one.

    prime-a2345-a91b2.md "``4213`` window": at phase 0.05 the passes run at
    0.05 + 0.15 k; 23:26 starts at 45 s, so the 45.05 s pass goes inside.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(systick_phase=0.05)
    advance(device, client, clock, 2)
    command(device, client, 0x213, screensaver_window((23, 26), (23, 40), 0x7F))

    advance(device, client, clock, 43.02)
    waiting = module.values[WINDOW_PHASE]
    advance(device, client, clock, 0.04)

    assert (waiting, module.values[WINDOW_PHASE]) == (1, 2)


def test_a2345_a_weekly_window_written_before_the_10th_pass_is_judged_on_it() -> None:
    """The phase stays 0 until the seed block re-stores and judges it in one pass.

    prime-a2345-a91b2.md "``4213`` first evaluation": a weekly 23:30-23:40 at
    0.5 s leaves the phase 0 (the store returns early); the 1.5 s pass judges
    it outside.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    advance(device, client, clock, 0.5)

    command(device, client, 0x213, screensaver_window((23, 30), (23, 40), 0x7F))
    advance(device, client, clock, 0.99)
    unjudged = WINDOW_PHASE not in module.values
    advance(device, client, clock, 0.02)

    assert unjudged
    assert module.values[WINDOW_PHASE] == 1


def test_a2345_a_weekly_window_store_leaves_the_phase_to_the_next_pass() -> None:
    """``screensaver_window_store`` sets no weekly phase; the next pass does."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    advance(device, client, clock, 2.06)

    command(device, client, 0x213, screensaver_window((23, 30), (23, 40), 0x7F))
    stored = module.values[WINDOW_PHASE]
    advance(device, client, clock, 0.15)

    assert (stored, module.values[WINDOW_PHASE]) == (2, 1)


def test_a2345_a_once_window_whose_start_a_clock_set_passes_is_skipped() -> None:
    """``DAT_2000b49e``: a start found passed is latched as handled, never entered.

    prime-a2345-a91b2.md "``4213`` inside rule": a clock set past the start.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    advance(device, client, clock, WINDOW_FIRST_WAIT)
    command(device, client, 0x213, screensaver_window((23, 30), (23, 40), 0))

    moved = ui(4).build(ui(4).parse(TIMESTAMP) + 360)
    exchange(module, client, 0x022, [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)])
    skipped = module.values[WINDOW_PHASE]
    advance(device, client, clock, 600)

    assert (skipped, module.values[WINDOW_PHASE]) == (1, 1)


def test_a2345_a_once_window_end_turns_the_clock_off() -> None:
    """At the exact end minute: outside, the clock off (``af[0]`` bit 7), ``030b``.

    ``screen_target_set(0, theme)`` with source 2, so the screensaver goes to
    black and the report goes out after its debounce.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    advance(device, client, clock, WINDOW_FIRST_WAIT)
    command(device, client, 0x213, screensaver_window((23, 25), (23, 27), 0))
    into_screensaver(device, client, clock)

    advance(device, client, clock, 72)
    shown = (module.values[WINDOW_PHASE], module.values[DISPLAY])
    pushes = advance(device, client, clock, 3)

    reports = [
        Parameters.parse(bytes.fromhex(payload))["a2"]["value"][0]
        for cmd, payload in pushes
        if cmd == "430b"
    ]
    assert shown == (2, 1)
    assert (module.values[WINDOW_PHASE], module.values[DISPLAY]) == (1, 2)
    assert module.values["screen_saver_switch"] is False
    assert [value & 0x80 for value in reports] == [0]


def test_a2345_a_once_window_end_with_the_clock_already_off_reports_nothing() -> None:
    """The screen target doesn't change, so no ``030b`` and no move."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    advance(device, client, clock, WINDOW_FIRST_WAIT)
    command(device, client, 0x213, screensaver_window((23, 25), (23, 27), 0))
    device.set_values(screen_saver_switch=False)
    advance(device, client, clock, REPORT_WAIT)

    pushes = advance(device, client, clock, 106)

    assert module.values[WINDOW_PHASE] == 1
    assert [cmd for cmd, _ in pushes if cmd == "430b"] == []


def test_a2345_a_once_window_end_passed_by_a_clock_set_turns_the_clock_off() -> None:
    """``DAT_2000b4a8`` latched past the end: the clock off, the phase left inside.

    ``clock_holiday_autoscreen?``: only the exact end minute sets the phase to 1.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    advance(device, client, clock, WINDOW_FIRST_WAIT)
    command(device, client, 0x213, screensaver_window((23, 25), (23, 27), 0))

    moved = ui(4).build(ui(4).parse(TIMESTAMP) + 300)
    exchange(module, client, 0x022, [(0xA1, moved), (0xA3, UTC_OFFSET), (0xA5, TZ)])
    advance(device, client, clock, PASS_WAIT)

    assert module.values[WINDOW_PHASE] == 2
    assert module.values["screen_saver_switch"] is False


def test_a2345_black_waits_outside_the_window_and_moves_at_its_start() -> None:
    """Black stays black outside the ``4213`` window; its start minute moves it."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(screen_timeout=1)
    command(device, client, 0x213, screensaver_window((23, 27), (23, 40), 0x7F))

    advance(device, client, clock, 104)
    waiting = module.values[DISPLAY]
    advance(device, client, clock, 1.5)

    assert (waiting, module.values[DISPLAY]) == (2, 1)


def test_a2345_the_holiday_window_pushes_030b_at_its_edges_in_the_screensaver() -> None:
    """``holiday_theme_change_push_030b``: 2 -> 1 at start, 1 -> 2 past the end.

    Pushed directly, with no source gate or debounce: ``a4`` the holiday
    record at start, none past the end (prime-a2345-a91b2.md "Holiday ``030b``").
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    into_screensaver(device, client, clock)
    command(device, client, 0x20A, [(0xA2, b"\x04us"), (0xA3, b"\x04" + ACCOUNT)])
    device.set_wifi(True)
    start = module.rtc + 20
    command(device, client, 0x212, holiday_schedule(1, start, start + 20))
    advance(device, client, clock, 9)
    device.set_wifi(False)

    landed = advance(device, client, clock, 10)
    started = advance(device, client, clock, 2)
    ended = advance(device, client, clock, 21)

    def reports(pushes: list[tuple[str, str]]) -> list[list[str]]:
        return [
            list(Parameters.parse(bytes.fromhex(payload)))
            for cmd, payload in pushes
            if cmd == "430b"
        ]

    assert reports(landed) == []
    assert reports(started) == [["a1", "a2", "a3", "a4", "fe"]]
    assert reports(ended) == [["a1", "a2", "a3", "fe"]]


def test_a2345_a_holiday_edge_crossed_on_shows_at_the_next_evaluation() -> None:
    """The first evaluation pushes nothing; an edge crossed in 0 at the next one.

    ``4a02`` reads the last evaluated flag, so it doesn't show the holiday
    before the screensaver has evaluated it.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    module = device.negotiated_module
    command(device, client, 0x20A, [(0xA2, b"\x04us"), (0xA3, b"\x04" + ACCOUNT)])
    device.set_wifi(True)
    window = holiday_schedule(1, module.rtc, module.rtc + 60)
    command(device, client, 0x212, window)
    advance(device, client, clock, 9)
    device.set_wifi(False)
    unevaluated = "a5" in screensaver(device)

    first = into_screensaver(device, client, clock)
    shown = "a5" in screensaver(device)
    command(device, client, 0x223, [(0xA2, b"\x01\x01")])
    advance(device, client, clock, 25)
    again = into_screensaver(device, client, clock)

    assert (unevaluated, shown) == (False, True)
    assert [cmd for cmd, _ in first if cmd == "430b"] == []
    assert [cmd for cmd, _ in again if cmd == "430b"] == ["430b"]
    assert "a5" not in screensaver(device)


def test_a2345_420a_stores_the_country_uppercased() -> None:
    """``420a`` stores ``a2``'s two characters, uppercased (doc "``420a``")."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    command(device, client, 0x20A, [(0xA2, b"\x04gb"), (0xA3, b"\x04" + ACCOUNT)])

    assert device.negotiated_module.values["_country"] == b"GB"


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


@pytest.mark.parametrize("device", [A2345, A91B2])
def test_the_snapshot_carries_the_port_fault_bitmap_at_0(
    device: type[A2345 | A91B2],
) -> None:
    """``a3``: the u16 port-fault bitmap, 0 in every recorded snapshot."""
    fields = snapshot(device())

    assert (fields["a3"]["type"], fields["a3"]["value"]) == (0x02, b"\x00\x00")
