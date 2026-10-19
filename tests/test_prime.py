# Copyright (c) 2026 Shawn Stricker
"""The Prime devices (A110B, A2345, A25X7, A2687, A91B2) over bleak."""

from bleak import BleakClient

from anker_ble_emulator import (
    A25X7,
    A91B2,
    A110B,
    A2345,
    A2687,
    EmulatedBleakBackend,
    Outer,
)
from anker_ble_emulator.devices import COMPANY_ID
from anker_ble_emulator.devices.a91b2 import ClockTheme as A91B2ClockTheme
from anker_ble_emulator.devices.a91b2 import Display, Timer
from anker_ble_emulator.devices.a2345 import (
    SCREENSAVER_FIELDS,
    Schedule,
    Screensaver,
    Theme,
)
from anker_ble_emulator.layouts import PortStatus, ui
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

    assert replies[0x827].status == 0x09
    assert authorized
    assert cmd_hex(snapshot[0].frame) == "4a00"
    assert (
        snapshot[0].plaintext == device.negotiated_module.mcu.respond(0x200)[0].payload
    )
    assert sum(1 for data in link.raw if len(data) == 253) >= 1


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

    assert [cmd_hex(reply.frame) for reply in ac] == ["4a07"]
    assert ac[0].plaintext == bytes.fromhex("00a10131")
    assert usbc[0].plaintext == bytes.fromhex("00a10131")
    assert out_of_range[0].plaintext == bytes.fromhex("04a10131")
    assert bad_switch[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    # usbc_1 (a4) has real PortStatus telemetry; ac_1/ac_2 have none modeled
    # yet (PORTS_0A00 covers only usbc/usba), so the ac switch above acks
    # but has nothing to verify.
    assert decode_field(params, "a4", PortStatus).status == 1


def test_a91b2_a_named_value_reaches_both_messages_that_carry_it() -> None:
    device = A91B2()

    device.set_values(usba_1_power=42)

    pushed = device.mcu.push(0x303, values=device.negotiated_module.values)
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    pushed_port = decode_field(Parameters.parse(pushed.payload), "a6", PortStatus)
    snapshot_port = decode_field(Parameters.parse(snapshot.payload), "a8", PortStatus)
    assert pushed_port.power == snapshot_port.power == 42


def test_a91b2_display_charging_and_clock_fields_are_all_settable_by_name() -> None:
    """The 0a00-only single-instance and scalar fields, not just ports/timers.

    PiField/docs/re/prime-a2345-a91b2.md "4a00 snapshot" (A91B2): ac/ad/ae/af
    and the b4/b5 scalars.
    """
    device = A91B2()

    device.set_values(
        clock_switch=True,
        theme_index=5,
        display_brightness=10,
        charging_mode=1,
        clock_format=1,
        ac_led=1,
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
    device.set_values(display_brightness=2, display_timeout_mode=0)
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
    device.set_values(ac_1_timer_switch=True, ac_1_timer_seconds=10800)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        disarm = await link.send(
            0x209,
            [(0xA1, b"\x21"), (0xA2, b"\x01\x00"), (0xA3, b"\x04\x00\x30\x2a\x00\x00")],
            SESSION,
        )

    assert disarm[0].plaintext == bytes.fromhex("00a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    timer = decode_field(params, "aa", Timer)
    assert not timer.timer_switch
    assert timer.timer_seconds == 10800


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

    assert armed[0].plaintext == bytes.fromhex("00a10131")
    assert bad_port[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    timer = decode_field(params, "aa", Timer)
    assert timer.timer_switch
    assert timer.timer_seconds == 120


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

    assert on_time[0].plaintext == bytes.fromhex("00a10131")
    assert bad_port[0].plaintext == bytes.fromhex("04a10131")
    assert bad_slot[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    usbc_1 = decode_field(params, "b0", Timer)
    start = (
        usbc_1.start_switch,
        usbc_1.start_hour,
        usbc_1.start_minute,
        usbc_1.start_weekdays,
    )
    assert start == (1, 7, 0x1E, 0x1F)


async def test_a91b2_screen_wake_clock_format_and_ac_led_commands_validate() -> None:
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        wake = await link.send(0x201, [(0xA1, b"\x21")], SESSION)
        clock_format = await link.send(
            0x210, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        bad_clock_format = await link.send(
            0x210, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )
        ac_led = await link.send(0x214, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        bad_ac_led = await link.send(
            0x214, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION
        )

    assert wake[0].plaintext == bytes.fromhex("00a10131")
    assert clock_format[0].plaintext == bytes.fromhex("00a10131")
    assert bad_clock_format[0].plaintext == bytes.fromhex("04a10131")
    assert ac_led[0].plaintext == bytes.fromhex("00a10131")
    assert bad_ac_led[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "b4", ui(1)) == 1
    assert decode_field(params, "b5", ui(1)) == 1


async def test_a91b2_brightness_and_clock_switch_commands_set_their_fields() -> None:
    device = A91B2()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        brightness = await link.send(
            0x204, [(0xA1, b"\x21"), (0xA2, b"\x01\x14")], SESSION
        )
        clock_switch = await link.send(
            0x205, [(0xA1, b"\x21"), (0xA2, b"\x01\x86")], SESSION
        )

    assert brightness[0].plaintext == bytes.fromhex("00a10131")
    assert clock_switch[0].plaintext == bytes.fromhex("00a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "ad", Display).display_brightness == 4  # 0x14 & 0x0f
    clock = decode_field(params, "ac", A91B2ClockTheme)
    assert clock.clock_switch
    assert clock.theme_index == 6


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
    assert theme_byte.clock_switch
    assert theme_byte.theme_index == 2


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
    assert decode_field(params, "af", Theme).holiday


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

    assert switch[0].plaintext == bytes.fromhex("00a10131")
    assert bad_port[0].plaintext == bytes.fromhex("04a10131")
    assert on_time[0].plaintext == bytes.fromhex("00a10131")
    assert bad_schedule_port[0].plaintext == bytes.fromhex("04a10131")
    assert bad_slot[0].plaintext == bytes.fromhex("04a10131")
    assert countdown[0].plaintext == bytes.fromhex("00a10131")
    assert disarm[0].plaintext == bytes.fromhex("00a10131")
    assert bad_countdown_port[0].plaintext == bytes.fromhex("04a10131")
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    assert decode_field(params, "a8", PortStatus).status == 1  # usba_1 switched on
    assert decode_field(params, "a9", PortStatus).status == 1  # usba_2 switched on
    usbc_1 = decode_field(params, "aa", Schedule)
    start = (
        usbc_1.start_switch,
        usbc_1.start_hour,
        usbc_1.start_minute,
        usbc_1.start_weekdays,
    )
    assert start == (1, 9, 0x1E, 0x1F)
    assert usbc_1.timer_switch
    assert usbc_1.timer_seconds == 300
    assert not decode_field(params, "ab", Schedule).timer_switch  # usbc_2 disarmed


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
        decode_field(params, tag, Schedule).priority for tag in ("aa", "ab", "ac", "ad")
    ]
    assert priorities == [1, 2, 1, 1]  # only usbc_2 designated


async def test_a2345_knob_clock_format_screensaver_and_switches_validate() -> None:
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
        clock_format = await link.send(
            0x210, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        bad_clock_format = await link.send(
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
    assert clock_format[0].plaintext == bytes.fromhex("00a10131")
    assert bad_clock_format[0].plaintext == bytes.fromhex("04a10131")
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

    device.set_values(usbc_1_power=42)

    pushed = device.mcu.push(0x303, values=device.negotiated_module.values)
    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    pushed_port = decode_field(Parameters.parse(pushed.payload), "a2", PortStatus)
    snapshot_port = decode_field(Parameters.parse(snapshot.payload), "a4", PortStatus)
    assert pushed_port.power == snapshot_port.power == 42


def test_a2345_set_values_validates_port_schedule_single_and_scalar_fields() -> None:
    """The 0a00-only schedule/theme/screensaver/scalar fields, not just ports.

    PiField/docs/re/prime-a2345-a91b2.md "4a00 snapshot" (A2345): aa-ae
    schedules, af theme, b9 screensaver, and the plain-byte scalars.
    """
    device = A2345()

    device.set_values(
        usbc_1_start_switch=1,
        usbc_1_start_hour=7,
        clock_switch=True,
        theme_index=2,
        screensaver_start_hour=8,
        display_timeout_mode=2,
        charging_mode=3,
        display_brightness=50,
        knob_direction=1,
        clock_format=1,
        compatibility_switch=1,
        device_identification_switch=1,
    )

    snapshot = device.mcu.respond(0x200, values=device.negotiated_module.values)[0]
    params = Parameters.parse(snapshot.payload)
    usbc_1 = decode_field(params, "aa", Schedule)
    assert (usbc_1.start_switch, usbc_1.start_hour) == (1, 7)
    theme_byte = decode_field(params, "af", Theme)
    assert theme_byte.clock_switch
    assert theme_byte.theme_index == 2
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
