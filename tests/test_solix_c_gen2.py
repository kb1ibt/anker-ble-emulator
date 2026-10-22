# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C Gen 2 line (A1763, A1765, A1783, A1785): one build, four models."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

import pytest
from bleak import BleakClient
from construct import GreedyBytes

from anker_ble_emulator import (
    A1763,
    A1765,
    A1783,
    A1785,
    EmulatedBleakBackend,
    ManualClock,
    ModuleBuild,
)
from anker_ble_emulator.devices import COMPANY_ID, Profile, solix_c_gen2
from anker_ble_emulator.layouts import LayoutError, ui
from anker_ble_emulator.parameters import Parameters, decode_field
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import (
    SESSION,
    AppClient,
    BleakLink,
    cmd_hex,
    exchange,
    negotiate,
    settle,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    from anker_ble_emulator import EmulatedDevice
    from anker_ble_emulator.layouts import Value

    Model: TypeAlias = Callable[..., EmulatedDevice]

TOKEN = b"owner-token"
#: The A1783's recorded ``0830``: v0.3.3.0, v1.2.1.6 and its OTA type names.
A1783_VERSIONS = bytes.fromhex(
    "00a10876302e332e332e30a20876312e322e312e36a30941313738335f6c6f77"
    "a40d41313738335f6d63755f6c6f77a50f41313738335f65737033325f6c6f77"
)
MODELS = [A1763, A1765, A1783, A1785]
#: The bench's ``4090`` fields and the ``d9`` (mode, periods, active type) after each.
TOU_BENCH = [
    ("a2020100a6020102a70704030011011118", (0, "02030011011118", 0)),
    ("a2020100a7080402030011011118", (0, "02020300110111", 0)),
    (
        "a2020100a3020105a4020107a6020103a70a04030007020711011118",
        (0, "03030007020711011118", 0),
    ),
    ("a2020101a6020101a70404011516", (1, "01011516", 0)),
    ("a2020100", (0, "01011516", 0)),
    ("a2020101a6020101a70404010f10", (1, "01010f10", 0)),
]
#: The 2026-10-21 bench clock (a1783-control.md "TOU (4090)"): ``0022`` UTC
#: as sent, and EDT, 14400 s west.
UTC_15_44_00 = bytes.fromhex("0016d96a")
UTC_15_44_27 = bytes.fromhex("1b16d96a")
UTC_16_00_00 = bytes.fromhex("c019d96a")
UTC_16_21_00 = bytes.fromhex("ac1ed96a")
UTC_16_26_00 = bytes.fromhex("d81fd96a")
EDT = bytes.fromhex("40380000")
#: The bench unit: on WiFi, mains in at 1290 W with Battery-Only Power off,
#: main pack 95 %, min SoC 5 %.
TOU_UNIT = {
    "wifi_online": 1,
    "battery_level": 95,
    "discharge_limit_value": 5,
    "ac_input_status": 1,
    "ac_input_power": 1290,
    "ac_input_disable_switch": 0,
}
#: (``4090`` mode and record, the ``0022`` UTC, values over ``TOU_UNIT``) ->
#: (``d9[0]``, ``a7`` AC input power, ``a1``). The plan's reserve is 50 %.
TOU_HOLD_CASES = [
    pytest.param("01", "030018", UTC_16_21_00, {}, (3, 1290, "34"), id="off-peak"),
    pytest.param(
        "01", "010f10", UTC_15_44_27, {"wifi_online": 0}, (0, 1290, "31"), id="ble"
    ),
    pytest.param("01", "030f02", UTC_16_26_00, {}, (0, 1290, "34"), id="wrapping"),
    pytest.param("00", "010f10", UTC_15_44_27, {}, (0, 1290, "34"), id="standard"),
    pytest.param(
        "01",
        "010f10",
        UTC_15_44_27,
        {"power_stage_ac_input": 0},
        (0, 1290, "34"),
        id="no-power-stage-input",
    ),
    pytest.param(
        "01",
        "010f10",
        UTC_15_44_27,
        {"external_device_product_code": b"AS200".ljust(16, b"\0")},
        (0, 1290, "34"),
        id="alternator",
    ),
    pytest.param(
        "01",
        "010f10",
        UTC_15_44_27,
        {"ac_input_disable_switch": 2},
        (0, 1290, "34"),
        id="auto-off",
    ),
    pytest.param(
        "01",
        "010f10",
        UTC_15_44_27,
        {"ac_input_disable_switch": 1, "mcu_version": bytes.fromhex("01010201")},
        (1, 0, "34"),
        id="v1.2.1.1-battery-only",
    ),
    pytest.param(
        "01",
        "010f10",
        UTC_15_44_27,
        {"battery_level": 40},
        (1, 1290, "34"),
        id="doc-gate-below-threshold",
    ),
    pytest.param(
        "01", "020f10", UTC_15_44_27, {}, (2, 0, "34"), id="doc-gate-mid-peak"
    ),
]
#: The same day's cloud ``0090`` fields, in order, after ``TOU_BENCH``.
TOU_BENCH_CLOUD = [
    "a2020100a6020102a70704030011011118",
    "a2020101a6020101a70404030018",
    "a2020101a6020101a70404030f02",
    "a2020101a6020101a70404030018",
]
#: Class, advert name, manufacturer record, synthetic serial, PN in the frames.
IDENTITIES = [
    pytest.param(
        A1763,
        "SOLIX C1000 Gen 2",
        "02aa12deadbeef01b118444b393604",
        "AXDDK960000000001",
        b"\x05A1763",
        id="A1763",
    ),
    pytest.param(
        A1765,
        "SOLIX C1000X Gen 2",
        "02aa12deadbeef01b119444b393604",
        "AXDDK960000000001",
        b"\x05A1763",
        id="A1765",
    ),
    pytest.param(
        A1783,
        "SOLIX C2000 Gen 2",
        "02aa12deadbeef01b11a444b4b4504",
        "APCDKKE0000000001",
        b"\x05A1783",
        id="A1783",
    ),
    pytest.param(
        A1785,
        "SOLIX C2000X Gen 2",
        "02aa12deadbeef01b11b444b565004",
        "AXDDKVP0000000001",
        b"\x05A1783",
        id="A1785",
    ),
]


@pytest.mark.parametrize(("model", "name", "record", "serial", "pn"), IDENTITIES)
def test_each_model_advertises_its_own_identity(
    model: Model, name: str, record: str, serial: str, pn: bytes
) -> None:
    device = model()

    assert device.local_name == name
    assert device.serial == serial
    assert device.advertisement_data.manufacturer_data[COMPANY_ID].hex() == record
    assert pn in device.negotiated_module.mcu.respond(0x100)[0].payload


@pytest.mark.parametrize(("model", "name", "record", "serial", "pn"), IDENTITIES)
def test_status_frames_carry_the_model_serial(
    model: Model, name: str, record: str, serial: str, pn: bytes
) -> None:
    script = model().negotiated_module.mcu

    for frame in [*script.respond(0x100), script.push(0x421)]:
        assert serial.encode() in frame.payload


@pytest.mark.parametrize("model", MODELS)
def test_every_model_answers_the_line_s_commands(model: Model) -> None:
    script = model().negotiated_module.mcu

    for request, replies in solix_c_gen2.REPLIES.items():
        assert [frame.cmd.msgtype for frame in script.respond(request)] == list(
            replies
        ), f"{request:03x}"


@pytest.mark.parametrize("model", MODELS)
def test_every_model_pushes_the_series_0490(model: Model) -> None:
    push = model().negotiated_module.mcu.push(0x490)

    assert push.cmd.msgtype == 0x490
    assert push.payload.endswith(b"charging_pps_series_c_0009\x00")


@pytest.mark.parametrize("model", MODELS)
def test_recorded_frames_are_routed_to_ble(model: Model) -> None:
    device = model()
    script = device.negotiated_module.mcu
    frames = [
        frame for request in solix_c_gen2.REPLIES for frame in script.respond(request)
    ]
    assert isinstance(device.profile, Profile)
    frames += [script.push(msgtype) for msgtype in device.profile.pushes]

    assert all(
        frame.payload.startswith((b"\xa1\x01\x31", b"\x00\xa1\x01\x31"))
        for frame in frames
    )


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("build", list(ModuleBuild))
def test_only_module_v0_3_3_0_enforces(model: Model, build: ModuleBuild) -> None:
    device = model(module=build)

    assert device.module_build is build
    assert device.negotiated_module.config.enforce is (build is ModuleBuild.V0_3_3_0)
    assert model().module_build is ModuleBuild.V0_3_3_0


async def test_a1763_on_module_v0_3_0_6_negotiates_in_clear() -> None:
    device = A1763(module=ModuleBuild.V0_3_0_6)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        authorized = device.negotiated_module.authorized
        status = await link.send(0x100, [(0xA1, b"\x21")], SESSION)
        setter = await link.send(0x101, [(0xA1, b"\x21")], SESSION)

    assert authorized
    assert [cmd_hex(reply.frame) for reply in status] == ["4900", "4421"]
    assert [cmd_hex(reply.frame) for reply in setter] == ["4901", "4421"]
    assert setter[0].plaintext == bytes.fromhex("00a10131")


async def test_a1785_grants_by_button_then_answers_the_backup_plan() -> None:
    device = A1785()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        replies = await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        plan = await link.send(0x05E, [(0xA1, b"\x21")], SESSION)

    assert replies[0x827].status == 0x09
    assert [cmd_hex(reply.frame) for reply in plan] == ["485e", "4421"]


async def test_a1783_version_read_matches_the_recorded_reply() -> None:
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    assert [cmd_hex(reply.frame) for reply in version] == ["4830"]
    assert version[0].plaintext == A1783_VERSIONS


@pytest.mark.parametrize("build", list(ModuleBuild))
def test_version_read_reports_the_chosen_module_build(build: ModuleBuild) -> None:
    versions = A1785(module=build).negotiated_module.config.versions

    assert versions is not None
    assert versions.module == build.value.encode()
    assert versions.model == b"A1783_low"


def test_recorded_module_replies_come_only_with_their_build() -> None:
    replies = A1783().negotiated_module.config.session_replies

    assert set(replies) == {
        0x020,
        0x022,
        0x027,
        0x028,
        0x02E,
        0x02F,
        0x035,
        0x036,
        0x038,
    }
    assert replies[0x022] == bytes.fromhex("00a10109")
    assert replies[0x027] == bytes.fromhex("00a10100a20100")
    assert set(replies) | {0x030} <= ModuleBuild.V0_3_3_0.session_ops
    assert (
        A1783(module=ModuleBuild.V0_3_0_6).negotiated_module.config.session_replies
        == {}
    )
    assert A1763().negotiated_module.config.session_replies == {}


def test_each_module_build_lists_the_session_ops_it_answers() -> None:
    assert len(ModuleBuild.V0_3_3_0.session_ops) == 14
    assert ModuleBuild.V0_3_0_6.session_ops == ModuleBuild.V0_3_3_0.session_ops
    assert ModuleBuild.V0_2_9_7.session_ops == {
        0x20,
        0x22,
        0x23,
        0x24,
        0x25,
        0x28,
        0x2D,
        0x2F,
        0x30,
        0x36,
    }


async def test_a1783_ac_group_sets_timeout_input_limit_and_switches() -> None:
    """``4101``: PiField/docs/re/a1783-control.md "Groups 4101-4103"."""
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        setter = await link.send(
            0x101,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x01"),
                (0xA3, b"\x03\x2c\x01\x00\x00"),
                (0xA4, b"\x02\xb0\x04"),
                (0xA5, b"\x01\x3c"),
                (0xA6, b"\x01\x01"),
                (0xA7, b"\x01\x01"),
                (0xAB, b"\x02\x01\x00"),
            ],
            SESSION,
        )

    assert setter[0].plaintext == bytes.fromhex("00a10131")
    snapshot = device.negotiated_module.mcu.respond(
        0x100, values=device.negotiated_module.values
    )
    status = Parameters.parse(
        next(f for f in snapshot if f.cmd.msgtype == 0x421).payload
    )
    assert decode_field(status, "a7", solix_c_gen2.AcOutput).ac_output_status == 1
    settings = decode_field(status, "a4", solix_c_gen2.Settings)
    assert settings.ac_timer_value == 300
    assert settings.ac_recharge_power == 1200
    assert settings.ac_frequency == 60
    assert settings.ac_energy_saving_switch == 1
    assert settings.super_fast_charge_status == 1
    assert settings.ac_input_disable_switch == 1


async def test_a1783_dc_group_sets_car_socket_and_timeout() -> None:
    """``4102``: PiField/docs/re/a1783-control.md "Groups 4101-4103"."""
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        setter = await link.send(
            0x102,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x01"),
                (0xA3, b"\x03\x10\x0e\x00\x00"),
                (0xA4, b"\x01\x01"),
            ],
            SESSION,
        )
        switch_only = await link.send(
            0x102, [(0xA1, b"\x21"), (0xA2, b"\x01\x00")], SESSION
        )
        no_switch = await link.send(
            0x102, [(0xA1, b"\x21"), (0xA4, b"\x01\x00")], SESSION
        )

    assert setter[0].plaintext == bytes.fromhex("00a10131")
    assert switch_only[0].plaintext == bytes.fromhex("00a10131")
    assert no_switch[0].plaintext == bytes.fromhex("00a10131")
    # The socket switched on at 6 W, then switched off by the 4102 above.
    frame = Parameters.parse(device.negotiated_module.mcu.push(0x421).payload)
    frame["b2"]["value"] = bytes.fromhex("010600")
    device.set_push(0x421, Parameters.build(frame))
    on = decode_field(
        Parameters.parse(device.negotiated_module.mcu.push(0x421).payload),
        "b2",
        solix_c_gen2.DcOutput,
    )
    device.set_values(car_charge1_output_power=0)
    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    socket = decode_field(status, "b2", solix_c_gen2.DcOutput)
    assert (on.car_charge1_output_status, on.car_charge1_output_power) == (1, 6)
    assert (socket.car_charge1_output_status, socket.car_charge1_output_power) == (
        0,
        0,
    )
    settings = decode_field(status, "a4", solix_c_gen2.Settings)
    assert settings.car_charger_timer_value == 3600
    assert (
        settings.car_charger_energy_saving_switch == 0
    )  # overwritten by the last send


async def test_a1783_system_group_applies_the_soc_firmware_clamp() -> None:
    """``4103``: max<=min resets to 100/1; max/min out of range each clamp."""
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        display = await link.send(
            0x103,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x01"),
                (0xA3, b"\x01\x02"),
                (0xA4, b"\x02\x08\x07"),
                (0xA5, b"\x01\x01"),
                (0xA6, b"\x02\x3c\x00"),
                (0xA8, b"\x01\x01"),
                (0xA9, b"\x01\x00"),
            ],
            SESSION,
        )
        reset_to_defaults = await link.send(
            0x103,
            [(0xA1, b"\x21"), (0xAA, b"\x01\x50"), (0xAB, b"\x01\x50")],
            SESSION,
        )
        clamp_high = await link.send(
            0x103, [(0xA1, b"\x21"), (0xAA, b"\x01\xff")], SESSION
        )
        clamp_zero = await link.send(
            0x103, [(0xA1, b"\x21"), (0xAB, b"\x01\x00")], SESSION
        )
        alert = await link.send(0x103, [(0xA1, b"\x21"), (0xB0, b"\x01\x01")], SESSION)

    assert display[0].plaintext == bytes.fromhex("00a10131")
    assert reset_to_defaults[0].plaintext == bytes.fromhex("00a10131")
    assert clamp_high[0].plaintext == bytes.fromhex("00a10131")
    assert clamp_zero[0].plaintext == bytes.fromhex("00a10131")
    assert alert[0].plaintext == bytes.fromhex("00a10131")
    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    settings = decode_field(Parameters.parse(push.payload), "a4", solix_c_gen2.Settings)
    assert settings.screen_switch == 1
    assert settings.lcd_backlight_brightness == 2
    assert settings.screen_off_time == 0x0708
    assert settings.temperature_unit == 1
    assert settings.device_standby_time == 60
    assert settings.memory_switch == 1
    assert settings.device_language == 0
    assert (settings.charging_limit_value, settings.discharge_limit_value) == (
        100,
        1,
    )  # 80 <= 80 resets both
    assert settings.outage_alert_switch
    assert (
        settings.ac_input_disable_switch == 1
    )  # the recording's, untouched by the alert


async def test_a1783_ports_are_settable_by_their_app_names() -> None:
    device = A1783()

    device.set_values(pd2_output_power=500, usb1_output_status=1, pv1_input_power=42)

    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    pd2 = decode_field(status, "ab", solix_c_gen2.OutputPort)
    assert (pd2.output_status, pd2.output_power) == (1, 500)
    assert decode_field(status, "ae", solix_c_gen2.OutputPort).output_status == 1
    assert decode_field(status, "a8", solix_c_gen2.DcInput).pv1_input_power == 42


def test_a1783_layout_rejects_a_msgtype_with_no_command() -> None:
    device = A1783()

    assert device.layout is not None
    assert device.layout.check(0x999, b"") == 0x04


def test_a1783_layout_ignores_a_msgtype_with_no_telemetry() -> None:
    """``build``/``update`` touch only the typed messages; any other is a no-op."""
    device = A1783()

    assert device.layout is not None
    assert device.layout.build(0x999, {"screen_switch": 1}) == b""
    assert device.layout.update(0x999, b"payload", {"screen_switch": 1}) == b"payload"


async def test_a1763_tou_setter_lands_in_d9_beside_the_soc_limits() -> None:
    """``4090``: a1783-control.md "TOU (4090)"; ``d9`` is the shared build's."""
    device = A1763()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        plan = await link.send(
            0x090,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x01"),
                (0xA5, b"\x01\x19"),
                (0xA6, b"\x01\x02"),
                (0xA7, b"\x04\x03\x00\x17\x01\x17\x18"),
            ],
            SESSION,
        )
        limits = await link.send(
            0x103, [(0xA1, b"\x21"), (0xAA, b"\x01\x5f"), (0xAB, b"\x01\x05")], SESSION
        )

    assert [cmd_hex(reply.frame) for reply in plan] == ["4890", "4421"]
    assert plan[0].plaintext == bytes.fromhex("00a10131")
    assert limits[0].plaintext == bytes.fromhex("00a10131")
    status = Parameters.parse(limits[1].plaintext)
    assert status["d9"]["length"] == 32  # type byte + 6 + 2 periods + 19
    tou = decode_field(status, "d9", solix_c_gen2.Tou)
    assert tou["backup_mode"] == 1
    assert tou["tou_reserve_power"] == 25
    assert tou["tou_periods"] == bytes.fromhex("02030017011718")
    assert (tou["charging_limit_value"], tou["discharge_limit_value"]) == (95, 5)
    settings = decode_field(status, "a4", solix_c_gen2.Settings)
    assert (settings.charging_limit_value, settings.discharge_limit_value) == (95, 5)


async def test_a1783_clock_screen_setter_shows_in_da_and_the_4092_read() -> None:
    """``4091``/``4092``: a1783-control.md "Clock screen (4091 -> da)"."""
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        setter = await link.send(
            0x091,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x82"),
                (0xA5, b"\x00Theme 3"),
                (0xA8, b"\x02\xe0\x01"),
                (0xAA, b"\x01\x24"),
                (0xAC, b"\x01\x01"),
            ],
            SESSION,
        )
        read = await link.send(0x092, [(0xA1, b"\x21")], SESSION)

    assert [cmd_hex(reply.frame) for reply in setter] == ["4891", "4421"]
    assert [cmd_hex(reply.frame) for reply in read] == ["4892", "4421"]
    screen = decode_field(
        Parameters.parse(setter[1].plaintext), "da", solix_c_gen2.ClockScreen
    )
    assert screen["screen_saver_switch"] is True
    assert screen["lcd_theme"] == 2
    assert screen["lcd_start_time"] == 480
    assert screen["lcd_repeat_cycle"] == 0x24
    assert screen["day_brightness_level"] == 1
    assert screen["night_end_time"] == 480  # untouched by the write
    fields = Parameters.parse(read[0].plaintext)
    head = decode_field(fields, "a2", solix_c_gen2.ClockScreenHead)
    assert (head.screen_saver_switch, head.lcd_theme) == (True, 2)
    assert head.lcd_start_time == 480
    assert head.lcd_repeat_cycle == 0x24
    assert fields["a3"]["type"] == 0x04
    assert decode_field(fields, "a3", GreedyBytes) == b"Theme 3"


def test_fault_versions_and_capability_flags_are_settable_by_name() -> None:
    """``dc``/``f9``/``fa``: a1783-telemetry.md; ``dc`` is its 2026-08-29 witness."""
    device = A1763()

    device.set_values(
        memory_switch_error_code=3,
        memory_switch_error_timestamp=1788020428,
        bms_version=bytes.fromhex("03030901"),
        support_ac_input_disable=True,
    )

    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    fault = decode_field(status, "dc", solix_c_gen2.MemorySwitch)
    assert fault.memory_switch_error_code == 3
    assert fault.memory_switch_error_timestamp == 1788020428
    versions = decode_field(status, "f9", solix_c_gen2.DeviceVersions)
    assert versions.bms_version == bytes.fromhex("03030901")
    flags = decode_field(status, "fa", solix_c_gen2.Capabilities)
    assert flags.support_ac_input_disable  # set by name
    assert flags.memory_switch_two  # the recording's own bits, kept
    assert flags.support_ppstou


async def test_a1783_expansion_switch_and_block_fields() -> None:
    """``4104``, ``c0``, ``ce``: a1783-control.md "4104", "Other local-state tags"."""
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()

        switch = await link.send(
            0x104,
            [
                (0xA1, b"\x21"),
                (0xA2, b"\x01\x01"),
                (0xA3, b"\x00APCDKJM0000000002"),
                (0xA4, b"\x01\x00"),
            ],
            SESSION,
        )

    assert [cmd_hex(reply.frame) for reply in switch] == ["4904", "4421"]
    device.set_values(
        sub_package_battery_level=42,
        external_device_mode=solix_c_gen2.ExternalDeviceMode.CHARGING,
        external_device_output_power=300,
    )
    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    block = decode_field(status, "c0", solix_c_gen2.Expansion)
    assert status["c0"]["length"] == 44  # the attached shape is kept
    assert block["sub_package_switch_status"] == 0
    assert block["sub_package_battery_level"] == 42
    assert block["sub_package_sn"] == b"APCDKJM0000000002"
    assert block["sub_package_type"] == b"A1783_2kWh"
    alternator = decode_field(status, "ce", solix_c_gen2.Alternator)
    assert (
        alternator["external_device_mode"] == solix_c_gen2.ExternalDeviceMode.CHARGING
    )
    assert alternator["external_device_output_power"] == 300


def test_a1785_unattached_expansion_block_keeps_its_short_shape() -> None:
    device = A1785()

    device.set_values(sub_package_temperature=23, sub_package_connection_status=1)

    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    block = decode_field(status, "c0", solix_c_gen2.Expansion)
    assert status["c0"]["length"] == 35
    assert block["sub_package_sn"] == bytes(16)
    assert block["sub_package_temperature"] == 23
    assert block["sub_package_connection_status"] == 1
    assert block["sub_package_type"] == b"  "


def test_the_c1000_gen_2_has_no_expansion_port() -> None:
    """``c0``/``ce``/``4104`` are the C2000 chassis's; ``0490`` still names the pack."""
    c1000, c2000 = A1763(), A1783()

    assert c1000.layout is not None
    assert c2000.layout is not None
    assert not c1000.layout.has_command(0x104)
    assert c1000.layout.locate("sub_package_battery_level") == ()
    assert c1000.layout.locate("external_device_mode") == ()
    with pytest.raises(LayoutError):
        c1000.set_values(external_device_mode=1)
    assert c2000.layout.has_command(0x104)
    assert c2000.layout.locate("sub_package_battery_level") == (
        0x421,
        solix_c_gen2.MSGTYPE_900,
    )


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("command", [0x051, 0x059])
def test_a_table_command_nothing_records_gets_the_display_board_ack(
    model: Model, command: int
) -> None:
    """``fid0f_ack_reply?``: a1783-control.md "Command map" ``4051``/``4059``."""
    frames = model().mcu.respond(command)

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == [
        (command | 0x800, "00a10131")
    ]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(0x055, id="4055 not in the table"),
        pytest.param(0x062, id="4062 not in the table"),
        pytest.param(0x064, id="4064 before an OTA start"),
        pytest.param(0x105, id="past the table"),
    ],
)
def test_the_display_board_drops_what_it_doesnt_answer(command: int) -> None:
    assert A1783().mcu.respond(command) == []


@pytest.mark.parametrize("model", MODELS)
def test_restore_defaults_writes_the_factory_block_and_turns_ultrafast_off(
    model: Model,
) -> None:
    """a1783-control.md "``4051`` factory defaults" (block v1.2.1.1 0x08035324)."""
    device = model()
    layout = device.layout
    assert layout is not None
    device.set_values(
        device_standby_time=0, charging_limit_value=80, super_fast_charge_status=1
    )
    request = bytes.fromhex("a10121")

    changes = layout.state_changes(
        0x051, request, device.mcu.state(device.negotiated_module.values)
    )
    device.set_values(**changes)

    assert changes == solix_c_gen2.FACTORY_SETTINGS
    state = device.mcu.state(device.negotiated_module.values)
    assert state["device_standby_time"] == 720
    assert state["charging_limit_value"] == 100
    assert state["super_fast_charge_status"] == 0
    assert state["tou_reserve_power"] == 6


@pytest.mark.parametrize("model", MODELS)
def test_the_log_size_read_reports_only_the_250_byte_header_chunk(
    model: Model,
) -> None:
    """a1783-control.md "Command map" ``4065``: total, chunk size, chunk count."""
    device = model()
    request = bytes.fromhex("a10121")
    layout = device.layout
    assert layout is not None

    frames = device.mcu.respond(0x065, request=request)

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == [
        (0x865, "00a10131a30503fa000000a40503fa000000a5050301000000")
    ]
    assert layout.state_changes(0x065, request, device.mcu.state({})) == {}


@pytest.mark.parametrize(
    ("model", "serial", "versions"),
    [
        pytest.param(
            A1763,
            b"AXDDK96000000000",
            b",11.2.6,11.2.6,0.0.0,5.0.5,5.0.5,10.3.5,0.0.0,2.2.0,0.0.0",
            id="A1763",
        ),
        pytest.param(
            A1765,
            b"AXDDK96000000000",
            b",11.2.6,11.2.6,0.0.0,5.0.5,5.0.5,10.3.5,0.0.0,2.2.0,0.0.0",
            id="A1765",
        ),
        pytest.param(
            A1783,
            b"APCDKKE000000000",
            b",12.1.6,12.1.6,0.0.0,1.2.6,1.2.6,19.3.3,19.3.3,3.3.0,0.0.0",
            id="A1783",
        ),
        pytest.param(
            A1785,
            b"AXDDKVP000000000",
            b",12.1.1,12.1.1,0.0.0,1.2.6,1.2.6,19.2.2,0.0.0,3.0.6,0.0.0",
            id="A1785",
        ),
    ],
)
def test_log_chunk_0_is_the_rtc_serial_and_version_header(
    model: Model, serial: bytes, versions: bytes
) -> None:
    """a1783-control.md "BLE log upload": RTC, SN[:16], nine versions, padded.

    The versions are the recorded ``f9``'s, in the firmware's slot order (MCU
    twice, aux/LCD, ACDC DSP, DCDC DSP, BMS, expansion, module, USB-PD); the
    aux/LCD and USB-PD slots are 0, which no frame reports.
    """
    header = (b"[6AA730DA]" + serial + versions).ljust(249, b" ") + b"\n"

    frames = model().mcu.respond(
        0x066, request=bytes.fromhex("a10121a203020000"), rtc=0x6AA730DA
    )

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == [
        (0x866, "00a10131a203020000a30302fa00a4fb04" + header.hex())
    ]


@pytest.mark.parametrize("index", ["0100", "0500"])
def test_a_log_chunk_past_the_header_replies_empty(index: str) -> None:
    """a1783-control.md "BLE log upload": an index >= the chunk count, no data."""
    request = bytes.fromhex(f"a10121a20302{index}")

    frames = A1783().mcu.respond(0x066, request=request)

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == [
        (0x866, f"00a10131a20302{index}a303020000a40104")
    ]


def test_the_log_header_carries_a_set_serial_and_needs_byte_values() -> None:
    device = A1783()
    layout = device.layout
    assert layout is not None
    device.set_values(device_sn=b"APCDKKE0000000009")
    request = bytes.fromhex("a10121a203020000")

    chunk = device.mcu.respond(
        0x066, request=request, values=device.negotiated_module.values
    )[0]
    state = {**device.mcu.state({}), "device_sn": 9, "rtc": 0}

    assert layout.locate("device_sn") == (0x421, solix_c_gen2.MSGTYPE_900)
    assert decode_field(Parameters.parse(chunk.payload), "a4", GreedyBytes)[10:26] == (
        b"APCDKKE000000000"
    )
    with pytest.raises(TypeError, match="device_sn is a bytes value"):
        layout.reply_fields(0x066, request, state)


async def test_the_log_header_reads_the_rtc_the_clock_set_put_on_local_time() -> None:
    """a1783-control.md "Clock": RTC = ``0022 a1`` UTC less ``a3``, seconds west.

    The handshake's ``0022`` sends 0x6aa7691a UTC at 14400 s west (EDT), so
    local time is 0x6aa730da; until then the RTC counts from 0.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    clock.advance(5)
    unset = device.negotiated_module.rtc
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        clock.advance(90)
        chunk = await link.send(
            0x066, [(0xA1, b"\x21"), (0xA2, b"\x02\x00\x00")], SESSION
        )

    assert unset == 5
    assert [cmd_hex(reply.frame) for reply in chunk] == ["4866"]
    data = decode_field(Parameters.parse(chunk[0].plaintext), "a4", GreedyBytes)
    assert data[:10] == b"[6AA73134]"


def test_a_clock_set_before_the_sync_floor_is_acked_but_never_reaches_the_rtc() -> None:
    """a1783-control.md "Clock": ``set_utc_time_to_mcu`` drops UTC < 0x6561aca0."""
    module = A1783(clock=ManualClock()).negotiated_module
    client = AppClient()
    module.connect()
    negotiate(module, client, TOKEN)
    synced = module.rtc

    early = exchange(
        module, client, 0x022, [(0xA1, b"\x9f\xac\x61\x65"), (0xA3, b"\0\0\0\0")]
    )
    after_early = module.rtc
    floor = exchange(
        module, client, 0x022, [(0xA1, b"\xa0\xac\x61\x65"), (0xA3, b"\0\0\0\0")]
    )

    assert synced == 0x6AA730DA
    assert [(reply.frame.cmd.msgtype, reply.status) for reply in early] == [(0x822, 0)]
    assert after_early == synced
    assert [(reply.frame.cmd.msgtype, reply.status) for reply in floor] == [(0x822, 0)]
    assert module.rtc == 0x6561ACA0


def test_partial_tou_and_clock_writes_mirrors_and_a_malformed_schedule() -> None:
    device = A1783()
    layout = device.layout
    assert layout is not None
    state = device.negotiated_module.mcu.state({})

    # The recorded plan is 030017 011718, zero past it. a7 alone overwrites from
    # the first period and keeps the count; a6 alone recounts the stored buffer,
    # which a lower count leaves in place.
    assert layout.state_changes(0x090, bytes.fromhex("a10121a2020101"), state) == {
        "backup_mode": 1
    }
    assert layout.state_changes(0x090, bytes.fromhex("a10121a70404011516"), state) == {
        "tou_periods": bytes.fromhex("02011516011718"),
        "tou_period_table": bytes.fromhex("02011516011718") + bytes(12),
    }
    assert layout.state_changes(0x090, bytes.fromhex("a10121a6020101"), state) == {
        "tou_periods": bytes.fromhex("01030017"),
        "tou_period_table": bytes.fromhex("01030017011718") + bytes(12),
    }
    assert layout.state_changes(0x090, bytes.fromhex("a10121a6020103"), state) == {
        "tou_periods": bytes.fromhex("03030017011718000000"),
        "tou_period_table": bytes.fromhex("03030017011718") + bytes(12),
    }
    # Seven periods overrun the handler's buffer: refused, nothing stored.
    seven = "a7" + "16" + "04" + "030017" * 7
    assert (
        layout.state_changes(0x090, bytes.fromhex(f"a10121a2020101{seven}"), state)
        == {}
    )
    # A count over six is refused the same way; six recounts the stored buffer.
    assert layout.state_changes(0x090, bytes.fromhex("a10121a6020107"), state) == {}
    assert layout.check(0x090, bytes.fromhex("a10121a6020107")) == 0x04
    assert layout.state_changes(0x090, bytes.fromhex("a10121a6020106"), state) == {
        "tou_periods": bytes.fromhex("06030017011718") + bytes(12),
        "tou_period_table": bytes.fromhex("06030017011718") + bytes(12),
    }
    # A locate blink changes no field; a theme-id write touches only its leaf.
    assert layout.state_changes(0x104, bytes.fromhex("a10121a5020101"), state) == {}
    assert layout.state_changes(
        0x091, bytes.fromhex("a10121a3050304030201"), state
    ) == {"network_theme_id": 0x01020304}
    device.set_values(charging_limit_value=90)
    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    settings = decode_field(Parameters.parse(push.payload), "a4", solix_c_gen2.Settings)
    assert settings.charging_limit_value == 90
    with pytest.raises(TypeError):
        device.set_values(tou_periods=b"\x02\x03")
    with pytest.raises(TypeError):
        device.set_values(tou_periods=2)


def test_a_tou_periods_set_writes_through_to_the_stored_plan() -> None:
    """``tou_periods`` sets the plan ``4090`` builds on; ``d9`` reads from the plan."""
    device = A1783()
    layout = device.layout
    assert layout is not None
    module = device.negotiated_module

    # The recorded d9 is the initial plan, zero past its periods.
    recorded = module.mcu.state({})
    assert recorded["tou_period_table"] == bytes.fromhex("02030017011718") + bytes(12)
    assert "tou_period_table" in layout.names(0x421)
    assert layout.implied({"charging_limit_value": 90}) == {}

    device.set_values(tou_periods=bytes.fromhex("01011516"))
    assert module.values["tou_period_table"] == bytes.fromhex("01011516") + bytes(15)
    # A bare a6 recounts the plan that was set, not the recorded one.
    changes = layout.state_changes(
        0x090, bytes.fromhex("a10121a6020102"), module.mcu.state(module.values)
    )
    assert changes["tou_periods"] == bytes.fromhex("02011516000000")

    # The plan set directly wins over a tou_periods given beside it.
    plan = bytes.fromhex("02030017011718") + bytes(12)
    device.set_values(tou_periods=bytes.fromhex("01011516"), tou_period_table=plan)
    push = module.mcu.push(0x421, values=module.values)
    tou = decode_field(Parameters.parse(push.payload), "d9", solix_c_gen2.Tou)
    assert tou.tou_periods == bytes.fromhex("02030017011718")
    with pytest.raises(TypeError):
        device.set_values(tou_period_table=bytes(18))
    with pytest.raises(TypeError):
        device.set_values(tou_period_table=2)


def test_a_lowered_count_keeps_its_records_for_a_later_raise() -> None:
    """``4090`` patches the stored plan, so a raised count shows the old records."""
    device = A1783()
    layout = device.layout
    assert layout is not None
    state = device.negotiated_module.mcu.state({})
    views = []
    for fields in ("a6020103a70a04030007020711011118", "a6020101", "a6020103"):
        changes = layout.state_changes(0x090, bytes.fromhex(f"a10121{fields}"), state)
        state = {**state, **changes}
        views.append(changes["tou_periods"])

    assert views == [
        bytes.fromhex("03030007020711011118"),
        bytes.fromhex("01030007"),
        bytes.fromhex("03030007020711011118"),
    ]


async def test_the_bench_day_leaves_records_a_bare_count_shows_again() -> None:
    """The 2026-10-21 ``4090``/``0090`` run, then a bare three-period count."""
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        for request in [*(sent for sent, _ in TOU_BENCH), *TOU_BENCH_CLOUD, "a6020103"]:
            fields = decode_fields(bytes.fromhex(request))
            _, status = await link.send(
                0x090, [(0xA1, b"\x21"), *fields.items()], SESSION
            )

    tou = decode_field(Parameters.parse(status.plaintext), "d9", solix_c_gen2.Tou)
    assert tou.tou_periods == bytes.fromhex("03030018011118011118")


async def test_the_bench_peak_holds_grid_charging_until_the_period_ends() -> None:
    """a1783-control.md "TOU (4090)", 2026-10-21: Peak 15-16 on WiFi, reserve 50 %.

    Battery-Only Power blocks the period; switched off at 15:44:27, Peak goes
    active and AC input reads 0 W; at 16:00 the period ends and charging resumes.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    plan = [
        (0xA1, b"\x21"),
        (0xA2, b"\x01\x01"),
        (0xA5, b"\x01\x32"),
        (0xA6, b"\x01\x01"),
        (0xA7, bytes.fromhex("04010f10")),
    ]
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        await link.send(0x022, [(0xA1, UTC_15_44_00), (0xA3, EDT)])
        await link.send(0x090, plan, SESSION)
        blocked = await link.send(
            0x101, [(0xA1, b"\x21"), (0xAB, b"\x02\x01\x00")], SESSION
        )
        clock.advance(27)
        held = await link.send(
            0x101, [(0xA1, b"\x21"), (0xAB, b"\x02\x00\x00")], SESSION
        )
        clock.advance(16 * 60 - 27)
        ended = await link.send(0x100, [(0xA1, b"\x21")], SESSION)

    states = []
    for replies in (blocked, held, ended):
        fields = Parameters.parse(replies[-1].plaintext)
        states.append(
            (
                decode_field(fields, "d9", solix_c_gen2.Tou).tou_system_status,
                decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power,
                decode_field(fields, "a1", GreedyBytes).hex(),
            )
        )
    assert states == [(0, 1290, "34"), (1, 0, "34"), (0, 1290, "34")]


@pytest.mark.parametrize(
    ("mode", "record", "utc", "values", "expected"), TOU_HOLD_CASES
)
async def test_the_active_period_and_its_charge_hold(
    mode: str,
    record: str,
    utc: bytes,
    values: dict[str, Value],
    expected: tuple[int, int, str],
) -> None:
    """a1783-control.md "TOU (4090)" conditions 1-6 and the Peak/Mid-Peak hold.

    The ``doc-gate`` cases follow the doc's charging gate; the bench never
    witnessed them.
    """
    device = A1783(clock=ManualClock())
    device.set_values(**{**TOU_UNIT, **values})
    plan = [
        (0xA1, b"\x21"),
        (0xA2, bytes.fromhex(f"01{mode}")),
        (0xA5, b"\x01\x32"),
        (0xA6, b"\x01\x01"),
        (0xA7, bytes.fromhex(f"04{record}")),
    ]
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        await link.send(0x022, [(0xA1, utc), (0xA3, EDT)])
        await link.send(0x090, plan, SESSION)
        status = await link.send(0x100, [(0xA1, b"\x21")], SESSION)

    fields = Parameters.parse(status[-1].plaintext)
    assert (
        decode_field(fields, "d9", solix_c_gen2.Tou).tou_system_status,
        decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power,
        decode_field(fields, "a1", GreedyBytes).hex(),
    ) == expected


def test_the_c1000_gen_2_shares_the_peak_hold() -> None:
    """The A1763 (no ``ce``, MainMcu v1.2.1.1) runs the line's own TOU check."""
    device = A1763()
    layout = device.layout
    assert layout is not None
    module = device.negotiated_module
    device.set_values(**TOU_UNIT)
    state = module.mcu.state(module.values)
    peak = layout.state_changes(
        0x090,
        bytes.fromhex("a10121a2020101a5020132a6020101a70404010015"),
        state,
    )
    device.set_values(**peak)
    push = module.mcu.push(0x421, values=module.values, rtc=10 * 3600)
    fields = Parameters.parse(push.payload)

    assert decode_field(fields, "d9", solix_c_gen2.Tou).tou_system_status == 1
    assert decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power == 0


def test_the_hidden_tou_inputs_are_switches() -> None:
    device = A1783()
    for name, value in (("wifi_online", 2), ("power_stage_ac_input", b"\x01")):
        with pytest.raises(TypeError):
            device.set_values(**{name: value})


async def test_a_count_over_six_periods_is_refused_and_telemetry_builds() -> None:
    """The app allows six periods; a larger ``a6`` stores nothing, breaks no frame."""
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        overrun = await link.send(
            0x090, [(0xA1, b"\x21"), (0xA7, b"\x04" + bytes(21))], SESSION
        )
        refused = [
            await link.send(
                0x090, [(0xA1, b"\x21"), (0xA6, bytes([0x01, count]))], SESSION
            )
            for count in (7, 77, 255)
        ]
        status = await link.send(0x100, [(0xA1, b"\x21")], SESSION)

    # The board answers a refused count as it does an a7 overrun: an 00 ack and
    # its status push, with nothing stored.
    assert [cmd_hex(reply.frame) for reply in overrun] == ["4890", "4421"]
    assert overrun[0].plaintext.hex() == "00a10131"
    for replies in refused:
        assert [cmd_hex(reply.frame) for reply in replies] == ["4890", "4421"]
        assert replies[0].plaintext.hex() == "00a10131"
        tou = decode_field(
            Parameters.parse(replies[1].plaintext), "d9", solix_c_gen2.Tou
        )
        assert tou.tou_periods == bytes.fromhex("02030017011718")
    assert [cmd_hex(reply.frame) for reply in status] == ["4900", "4421"]
    for reply in status:
        tou = decode_field(Parameters.parse(reply.plaintext), "d9", solix_c_gen2.Tou)
        assert tou.tou_periods == bytes.fromhex("02030017011718")


async def test_the_bench_tou_writes_leave_the_plans_the_unit_reported() -> None:
    """The A1783 bench run of 2026-10-21 over BLE (a1783-control.md "TOU (4090)").

    Each ``4090`` (after ``a10121``, its ``fe`` left out) and the ``d9`` mode,
    periods and active type the next ``c421`` carried.
    """
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        acks, plans = [], []
        for request, _ in TOU_BENCH:
            fields = decode_fields(bytes.fromhex(request))
            ack, status = await link.send(
                0x090, [(0xA1, b"\x21"), *fields.items()], SESSION
            )
            tou = decode_field(
                Parameters.parse(status.plaintext), "d9", solix_c_gen2.Tou
            )
            acks.append(ack.plaintext.hex())
            plans.append(
                (tou.backup_mode, tou.tou_periods.hex(), tou.tou_system_status)
            )

    assert acks == ["00a10131"] * len(TOU_BENCH)
    assert plans == [plan for _, plan in TOU_BENCH]


async def test_the_4100_a2_selects_the_silent_recharge_power() -> None:
    """a1783-telemetry.md ``a3[10:12]``: bit 0 of ``a2``'s sixth value byte."""
    device = A1783()
    app_a2 = (0xA2, bytes.fromhex("04010003150101000000"))
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        powers = []
        for fields in ([app_a2], [(0xA2, b"\x04\x01")], [app_a2], []):
            replies = await link.send(0x100, [(0xA1, b"\x21"), *fields], SESSION)
            params = Parameters.parse(replies[0].plaintext)
            powers.append(decode_field(params, "a3", solix_c_gen2.Status))

    assert [status.silent_recharge_power for status in powers] == [1000, 600, 1000, 600]


def test_the_v1_2_1_1_status_block_has_no_silent_recharge_power() -> None:
    """The A1763 recording's 10-byte ``a3`` stays 10 bytes."""
    module = A1763().negotiated_module
    module.values["silent_recharge_power"] = 1000

    push = module.mcu.push(0x421, values=module.values)

    assert Parameters.parse(push.payload)["a3"]["length"] == 11


def test_any_request_with_an_fd_sets_the_transaction_id() -> None:
    """a1783-telemetry.md ``fd``: up to 15 B, kept by a request without one."""
    device = A1783()
    layout = device.layout
    assert layout is not None
    state = device.negotiated_module.mcu.state({})
    stamp = b"\x00" + b"1799999999999"
    long_stamp = b"\x00" + b"1234567890abcdefgh"

    with_fd = layout.state_changes(
        0x103, bytes.fromhex("a10121a2020101") + b"\xfd\x0e" + stamp, state
    )
    unknown = layout.state_changes(0x0EE, b"\xa1\x01\x21\xfd\x13" + long_stamp, state)
    without = layout.state_changes(0x103, bytes.fromhex("a10121a2020101"), state)

    assert with_fd == {"screen_switch": 1, "transaction_id": b"1799999999999"}
    assert unknown == {"transaction_id": b"1234567890abcde"}
    assert without == {"screen_switch": 1}


def test_a1783_event_report_takes_window_epochs_or_the_grid_loss_soc() -> None:
    """``0425``: a1783-control.md "0425 event report", its 2026-10-06 witness."""
    device = A1783()

    device.set_values(
        event_code=0x0A,
        event_start_timestamp=0x6AAF10A8,
        event_end_timestamp=0xFFFFFFFF,
    )
    window = device.negotiated_module.mcu.push(
        0x425, values=device.negotiated_module.values
    )
    device.set_values(event_code=0x59, event_soc=42)
    grid_loss = device.negotiated_module.mcu.push(
        0x425, values=device.negotiated_module.values
    )

    assert window.payload == bytes.fromhex("a10131a202010aa30504a810af6aa40504ffffffff")
    assert grid_loss.payload == bytes.fromhex("a10131a2020159a302042a")


def test_ota_readiness_is_set_in_the_push_and_the_reply_alike() -> None:
    """``0489``/``4089``: a1783-control.md "4089" (``0 4`` = blocked, "connect AC")."""
    device = A1763()

    device.set_values(ota_enable=0, ota_block_reason=4)

    values = device.negotiated_module.values
    push = device.negotiated_module.mcu.push(0x489, values=values)
    reply = device.negotiated_module.mcu.respond(0x089, values=values)[0]
    for payload in (push.payload, reply.payload):
        fields = Parameters.parse(payload)
        assert decode_field(fields, "a2", ui(1)) == 0
        assert decode_field(fields, "a3", ui(1)) == 4
    assert reply.payload.startswith(b"\x00\xa1\x01\x31")
