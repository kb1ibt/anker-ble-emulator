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
from anker_ble_emulator.devices.a91b2 import A91B2Layout, Display, Timer
from anker_ble_emulator.devices.a91b2 import ClockTheme as A91B2ClockTheme
from anker_ble_emulator.devices.a2345 import (
    SCREENSAVER_FIELDS,
    A2345Layout,
    Schedule,
    Screensaver,
    ScreensaverRead,
    Theme,
)
from anker_ble_emulator.devices.base import data_resource
from anker_ble_emulator.devices.prime import OutputPort
from anker_ble_emulator.layouts import ui
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
    advance,
    arm,
    command,
    device_clock,
    prime_client,
    published,
    pushed_by,
    snapshot,
    theme_select,
    unit_change,
    upload_chunk,
    upload_prepare,
    usbc_identity,
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

    assert [cmd_hex(reply.frame) for reply in ac] == ["4a07", "4302"]
    assert ac[0].plaintext == bytes.fromhex("00a10131a2020100")
    assert ac[1].plaintext == bytes.fromhex("a10131a2020100a3020100")
    assert [cmd_hex(reply.frame) for reply in out_of_range] == ["4a07"]
    assert usbc[0].plaintext == bytes.fromhex("00a10131a2020102")
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
    device = A2345()
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
            0x20D,
            "a203020001",
            [(0xA0D, "00a10131")],
            id="420d reads only its first value byte",
        ),
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
        already_on = await link.send(
            0x207, [*port, (0xA3, b"\x01\x01"), CLOCK], SESSION
        )
        off = await link.send(0x207, [*port, (0xA3, b"\x01\x00"), CLOCK], SESSION)
        again = await link.send(0x207, [*port, (0xA3, b"\x01\x00"), CLOCK], SESSION)
        usb = await link.send(
            0x207, [(0xA1, b"\x21"), (0xA2, b"\x01\x04"), (0xA3, b"\x01\x00")], SESSION
        )

    assert [cmd_hex(reply.frame) for reply in already_on] == ["4a07"]
    assert [(cmd_hex(reply.frame), reply.plaintext.hex()) for reply in off] == [
        ("4a07", "00a10131a2020100fe0503933cd66a"),
        ("4302", "a10131a2020100a3020100fe0503933cd66a"),
    ]
    assert [cmd_hex(reply.frame) for reply in again] == ["4a07"]
    assert usb[1].plaintext.hex() == "a10131a2020104a3020100"


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


@pytest.mark.parametrize(
    ("model", "select", "record", "expired", "left"),
    [
        pytest.param(
            A2345, 0, "aa", "000300000000000000", 0, id="A2345 zeroes the remaining"
        ),
        pytest.param(
            A91B2, 0, "aa", "000300000003000000", 3, id="A91B2 AC keeps the seconds"
        ),
        pytest.param(
            A91B2, 3, "b1", "000000000003000000", 0, id="A91B2 USB-C zeroes it"
        ),
    ],
)
def test_a_prime_countdown_runs_down_then_switches_the_port_off(
    model: type[A2345 | A91B2], select: int, record: str, expired: str, left: int
) -> None:
    """Each second takes one off the remaining time; at 0 the port switches off.

    Then ``0302`` (the port, off) and the ``0307`` disarm, remaining as a
    ``4209`` disarm leaves it (doc "MCU pushes" ``0302``/``0307``, A91B2 ``0307``).
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

    a2 = f"a20201{select:02x}"
    stamp = device_clock(3)
    assert [cmd for cmd, _ in armed] == ["4a09", "4307"]
    assert timeline == [
        ((True, True, 3), []),
        ((True, True, 2), []),
        (
            (True, True, 1),
            [
                ("4302", f"a10131{a2}a3020100{stamp}"),
                ("4307", f"a10131{a2}a30a04{expired}{stamp}"),
            ],
        ),
    ]
    assert countdown() == (False, False, left)


def test_a_countdown_expiry_on_a_port_already_off_sends_no_0302() -> None:
    """The switch watcher reports a change only (doc "MCU pushes" ``0302``)."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")
    command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])

    command(device, client, 0x209, [port, arm(1)])
    expired = advance(device, client, clock, 1)

    assert [cmd for cmd, _ in expired] == ["4307"]


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

    assert early == later == []
    assert [cmd for cmd, _ in disarm] == ["4a09", "4307"]
    assert decode_field(snapshot(device), "aa", Timer).relay_switch


def test_a_countdown_rearm_counts_down_again_from_its_seconds() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, b"\x01\x00")

    command(device, client, 0x209, [port, arm(5)])
    advance(device, client, clock, 3)
    rearm = command(device, client, 0x209, [port, arm(5)])
    restarted = decode_field(snapshot(device), "aa", Timer)
    early = advance(device, client, clock, 4)
    expired = advance(device, client, clock, 1)

    assert [cmd for cmd, _ in rearm] == ["4a09"]
    assert restarted.countdown_task_remaining_seconds == 5
    assert early == []
    assert [(cmd, payload[-14:]) for cmd, payload in expired] == [
        ("4302", device_clock(8)),
        ("4307", device_clock(8)),
    ]


def test_a_countdown_remaining_set_by_a_test_counts_down_from_there() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    command(device, client, 0x209, [(0xA2, b"\x01\x00"), arm(600)])
    device.set_values(pd1_countdown_task_remaining_seconds=2)
    early = advance(device, client, clock, 1)
    expired = advance(device, client, clock, 1)

    assert early == []
    assert [cmd for cmd, _ in expired] == ["4302", "4307"]


@pytest.mark.parametrize(
    ("model", "select", "record"),
    [
        pytest.param(A2345, 0, "aa", id="A2345 usbc1"),
        pytest.param(A91B2, 2, "b0", id="A91B2 usbc1"),
    ],
)
def test_a_prime_timed_task_switches_its_port_at_its_time_each_week(
    model: type[A2345 | A91B2], select: int, record: str
) -> None:
    """An enabled off-time at Sunday 23:26 switches the port off then, by the RTC.

    The firmware's per-minute run fires it and ``0302`` reports it (doc "MCU
    pushes" ``0302``); a repeating half stays enabled and fires a week on.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    port = (0xA2, bytes([1, select]))
    sunday_2326 = (0xA4, b"\x03\x01\x17\x1a\x40")

    write = command(device, client, 0x208, [port, (0xA3, b"\x01\x00"), sunday_2326])
    early = advance(device, client, clock, 44)
    fired = advance(device, client, clock, 1)
    timer = decode_field(snapshot(device), record, Timer)
    command(device, client, 0x207, [port, (0xA3, b"\x01\x01")])
    week_early = advance(device, client, clock, 7 * 86400 - 1)
    week = advance(device, client, clock, 1)

    off = f"a10131a20201{select:02x}a3020100"
    assert [cmd for cmd, _ in write] == ["4a08"]
    assert early == week_early == []
    assert fired == [("4302", off + device_clock(45))]
    assert not timer.relay_switch
    assert timer.timed_task_end_switch == 1
    assert week == [("4302", off + device_clock(45 + 7 * 86400))]


def test_a_once_timed_task_fires_at_its_next_time_then_disables_itself() -> None:
    """A once on-time (weekdays 0) fires at its next hh:mm, then ``0308`` en 00.

    The firmware keeps a once half's date off the wire; the emulator fires it
    at its next hh:mm (doc "MCU pushes" ``0308``: a fired once slot).
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
    week = advance(device, client, clock, 7 * 86400)

    clock_field = device_clock(45)
    assert fired == [
        ("4302", f"a10131a2020100a3020101{clock_field}"),
        ("4308", f"a10131a2020100a3020101a4050400171a00{clock_field}"),
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
        pytest.param(60, 7 * 86400 - 15, id="past it waits a week"),
    ],
)
def test_a_clock_set_re_times_a_queued_timed_task(shift: int, due: int) -> None:
    """A ``0022`` that moves the RTC re-times a queued half by the new clock."""
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

    assert early == []
    assert fired == [("4302", f"a10131a2020100a3020100{device_clock(shift + due)}")]


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

    The ``0308`` comes before the ack (doc ``4208`` row, MQTT 2026-09-09
    23:24:32). launches/2026-09-09_210425.106__3.23.0.log:3392 -> :3407.
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
        ("4308", f"a10131a20201{select:02x}a3020100a40504000c0000fe0503933cd66a"),
        ("4a08", f"00a10131a20201{select:02x}a3020101fe0503933cd66a"),
    ]
    assert [cmd_hex(reply.frame) for reply in nine] == ["4a08"]
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    end_switch = decode_field(Parameters.parse(snapshot.payload), record, Timer)
    assert end_switch.timed_task_end_switch == 0


def test_a2345_pushes_030f_at_once_and_030d_after_the_mode_settle() -> None:
    """430f carries the designated mask at once, 430d the mode about 3.3 s later.

    430d carries the mode, ``b8`` and the mask, after the fixed settle counter,
    for 420c and 4206 alike. launches/2026-09-08_161516.236__3.23.0.log:12376
    (430f), :12481 (430d); leaving Port Priority clears the designations:
    :13935 (430d mode 1, a4 0); doc "MCU pushes" 030f.
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
        ("430f", "a10131a3020101fe0503933cd66a"),
    ]
    assert early == []
    assert settled == [
        ("430d", f"a10131a2020102{profile}a4020101{device_clock(3)}"),
    ]
    assert [cmd for cmd, _ in again] == ["4a0c"]
    assert [cmd for cmd, _ in same_mode] == ["4a06"]
    assert smart == [
        ("4a06", "00a10131"),
        ("430f", "a10131a3020100fe0503933cd66a"),
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

    Each goes out when its setting changes on the unit (doc "Screen reports").
    ``030b`` as recorded (collector.mqtt-102026-2.log.gz :13974 ``a2 c0``,
    :14009 ``a2 00``, both ``a3 8eb81a0e``), on BLE's ``a1 31``.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    fe = device_clock(0)
    theme_id = "a305048eb81a0e"

    assert unit_change(device, client, screen_brightness=40) == [
        ("430a", f"a10131a2020128{fe}")
    ]
    assert unit_change(device, client, screen_timeout=2, button_rotate_direction=1) == [
        ("4309", f"a10131a2020102{fe}"),
        ("4310", f"a10131a2020101{fe}"),
    ]
    assert unit_change(
        device, client, screen_saver_theme=0, screen_saver_theme_id=0x0E1AB88E
    ) == [("430b", f"a10131a20201c0{theme_id}{fe}")]
    assert unit_change(
        device, client, screen_saver_switch=False, holiday_screensaver_switch=False
    ) == [("430b", f"a10131a2020100{theme_id}{fe}")]
    assert unit_change(device, client, screen_brightness=40) == []


def test_a2345_app_screen_settings_draw_no_report() -> None:
    """A ``42xx`` from the app sets the screen with only its ack.

    collector.mqtt-102026-2.log.gz: the ``0a05`` acks at 18:50:56, 18:52:48,
    18:53:31 and 18:56:52 draw no ``030b``.
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


#: A 500-byte image in three 192-byte chunks, the last short, two to a window.
IMAGE = bytes(i % 251 for i in range(500))
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

    def burst(gap: float, times: int) -> list[tuple[str, str]]:
        pushes = []
        for _ in range(times):
            pushes += switch()
            advance(device, client, clock, gap)
        return pushes

    slow = burst(0.55, 20)  # the 20th-newest 10.45 s before the newest
    clock.advance(30)
    fired = burst(0.5, 20)
    cooling = burst(0.5, 20)
    clock.advance(60)
    again = burst(0.5, 20)

    assert slow == cooling == []
    assert [(cmd, payload[:14]) for cmd, payload in fired] == [
        ("4305", "a10131a2020104")
    ]
    assert [cmd for cmd, _ in again] == ["4305"]


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
            device.unplug(1)
            advance(device, client, clock, 1)
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
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    def designate(mask: int) -> list[str]:
        replies = command(device, client, 0x20C, [(0xA2, bytes([1, mask]))])
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

    doc "Secret Features" Power Expert: the class is 0 for 21 s after the
    active-port count changes; each rise of the count to 3 fires, no cooldown.
    The recording has C2 active at 19 760 mV.
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
    settled = advance(device, client, clock, 0.2)
    idle = advance(device, client, clock, 2)
    dip = unit_change(device, client, pd1_output_voltage=5000)
    recovered = unit_change(device, client, pd1_output_voltage=20000)

    assert power_expert(two + two_settled + three + settling + idle + dip) == []
    assert power_expert(settled) == power_expert(recovered) == ["a10131a2020102"]


def test_a2345_device_expert_fires_when_all_six_ports_are_active() -> None:
    """``0305 a2`` 03 on each change into all six ports active; then 24 h off."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    ports = ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")

    def all_active() -> list[str]:
        device.set_values(pd1_output_status=0)
        pushes = unit_change(
            device, client, **{f"{port}_output_status": 1 for port in ports}
        )
        return [payload[:14] for cmd, payload in pushes if cmd == "4305"]

    first = all_active()
    clock.advance(3600)
    cooling = all_active()
    clock.advance(24 * 3600)
    again = all_active()

    assert first == again == ["a10131a2020103"]
    assert cooling == []


def test_a2345_secret_features_are_sent_one_a_second_in_their_order() -> None:
    """Two pending at once: Device Expert (3) at once, Feature 2 (5) 1 s on.

    doc "Secret Features": one pending feature serviced a second, in the order
    ``420d``, 1, 2, 3, 4, 5.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    device.set_values(pd3_output_status=1, pd4_output_status=1, usb1_output_status=1)
    for _ in range(9):
        device.plug(1, 0x05AC, 0x7117)
        device.unplug(1)
        advance(device, client, clock, 1)
    device.set_values(usb2_output_status=1)

    tenth = pushed_by(device, client, lambda: device.plug(1, 0x05AC, 0x7117))
    next_second = advance(device, client, clock, 1)

    assert [payload[:14] for cmd, payload in tenth if cmd == "4305"] == [
        "a10131a2020103"
    ]
    assert [payload[:14] for cmd, payload in next_second if cmd == "4305"] == [
        "a10131a2020105"
    ]


def test_a2345_420d_of_zero_acks_and_prompts_nothing() -> None:
    """A ``420d`` whose first value byte is 0 is acked with no ``0305``."""
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    zero = command(device, client, 0x20D, [(0xA2, b"\x02\x00")])
    later = advance(device, client, clock, 2)

    assert (zero, later) == ([("4a0d", "00a10131")], [])


def test_a2345_420d_prompt_goes_out_through_the_secret_feature_service() -> None:
    """A nonzero ``420d``'s ``0305`` (trigger 0) waits for the service tick.

    doc "Secret Features": one pending feature a second, ``420d`` first. Idle,
    it goes out at once; behind Device Expert, a second later.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    prompt = [(0xA2, b"\x02\x01")]

    alone = command(device, client, 0x20D, prompt)
    clock.advance(5)
    unit_change(
        device,
        client,
        **{
            f"{port}_output_status": 1
            for port in ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")
        },
    )
    behind = command(device, client, 0x20D, prompt)
    next_second = advance(device, client, clock, 1)

    assert alone == [
        ("4a0d", "00a10131"),
        ("4305", f"a10131a2020100{device_clock(0)}"),
    ]
    assert behind == [("4a0d", "00a10131")]
    assert next_second == [("4305", f"a10131a2020100{device_clock(6)}")]


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

    As recorded: collector.mqtt-101626.log.gz :5298 ``a10164a203024408fe05...``.
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
    switched = command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])
    command(device, client, 0x209, [port, arm(1)])
    expired = advance(device, client, clock, 1)
    stream = command(device, client, 0x20B, []), advance(device, client, clock, 5)
    while_up = published(device)
    device.set_wifi(False)
    back = command(device, client, 0x207, [port, (0xA3, b"\x01\x01")])

    assert [cmd for cmd, _ in switched] == ["4a07"]
    assert expired == []
    assert ([cmd for cmd, _ in stream[0]], stream[1]) == (["4a0b"], [])
    assert [cmd for cmd, _ in back] == ["4a07", "4302"]
    assert published(device) == while_up
    assert [(msgtype, payload[:6]) for msgtype, payload in while_up] == [
        (0x301, "a10164"),
        (0x302, "a10164"),
        (0x307, "a10164"),
        (0x307, "a10164"),
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


def test_a2345_4212_stores_the_holiday_schedule_and_a2_0_clears_it() -> None:
    """``4212``: id, ``a3``/``a4`` u32, ``a5``/``a6`` 7-byte datetimes, URL; no reply.

    ``a2 = 0`` clears the schedule (doc ``4212`` row).
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    url = b"https://example.invalid/holiday.jpg"

    scheduled = command(
        device,
        client,
        0x212,
        [
            (0xA2, b"\x03" + ui(4).build(RHYTHMIC_HUE)),
            (0xA3, b"\x03" + ui(4).build(7)),
            (0xA4, b"\x03" + ui(4).build(8)),
            (0xA5, b"\x04" + bytes(range(1, 8))),
            (0xA6, b"\x04" + bytes(range(2, 9))),
            (0xA7, b"\x04" + url),
        ],
    )
    values = device.negotiated_module.values
    stored = {name: values[name] for name in values if name.startswith("_holiday")}
    cleared = command(device, client, 0x212, [(0xA2, b"\x03" + ui(4).build(0))])
    partial_request = [(0xA2, b"\x03" + ui(4).build(RHYTHMIC_HUE))]
    incomplete = command(device, client, 0x212, partial_request)

    assert scheduled == cleared == incomplete == []
    assert stored == {
        "_holiday_id": RHYTHMIC_HUE,
        "_holiday_a3": 7,
        "_holiday_a4": 8,
        "_holiday_a5": bytes(range(1, 8)),
        "_holiday_a6": bytes(range(2, 9)),
        "_holiday_url": url,
    }
    assert device.negotiated_module.values["_holiday_id"] == 0


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
