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
    ModuleBuild,
)
from anker_ble_emulator.devices import COMPANY_ID, Profile, solix_c_gen2
from anker_ble_emulator.layouts import LayoutError, ui
from anker_ble_emulator.parameters import Parameters, decode_field
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


if TYPE_CHECKING:
    from collections.abc import Callable

    from anker_ble_emulator import EmulatedDevice

    Model: TypeAlias = Callable[..., EmulatedDevice]

TOKEN = b"owner-token"
#: The A1783's recorded ``0830``: v0.3.3.0, v1.2.1.6 and its OTA type names.
A1783_VERSIONS = bytes.fromhex(
    "00a10876302e332e332e30a20876312e322e312e36a30941313738335f6c6f77"
    "a40d41313738335f6d63755f6c6f77a50f41313738335f65737033325f6c6f77"
)
MODELS = [A1763, A1765, A1783, A1785]
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
    assert ModuleBuild.V0_2_9_7.session_ops == {0x20, 0x28, 0x2D, 0x2F, 0x30, 0x36}


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
    assert settings.ac_output_timeout_seconds == 300
    assert settings.ac_input_limit == 1200
    assert settings.ac_frequency == 60
    assert settings.ac_output_mode == 1
    assert settings.ac_fast_charge_switch == 1
    assert settings.ac_input_disable_state == 1


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
    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    assert decode_field(status, "b2", solix_c_gen2.DcOutput).dc_output_status == 0
    settings = decode_field(status, "a4", solix_c_gen2.Settings)
    assert settings.dc_output_timeout_seconds == 3600
    assert settings.dc_12v_output_mode == 0  # overwritten by the last send


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
    assert settings.display_switch == 1
    assert settings.display_mode == 2
    assert settings.display_timeout_seconds == 0x0708
    assert settings.temp_unit_fahrenheit == 1
    assert settings.device_timeout_minutes == 60
    assert settings.port_memory_switch == 1
    assert settings.display_language == 0
    assert (settings.max_soc, settings.min_soc) == (100, 1)  # 80 <= 80 resets both
    assert settings.outage_alert_switch
    assert (
        settings.ac_input_disable_state == 1
    )  # the recording's, untouched by the alert


async def test_a1783_power_status_ports_are_settable_by_name() -> None:
    device = A1783()

    device.set_values(pd2_power=500, usba_status=1, dc_input_power=42)

    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    pd2 = decode_field(status, "ab", solix_c_gen2.PowerStatus)
    assert (pd2.status, pd2.power) == (1, 500)
    assert decode_field(status, "ae", solix_c_gen2.PowerStatus).status == 1
    assert decode_field(status, "a8", solix_c_gen2.PowerStatus).power == 42


def test_a1783_layout_rejects_a_msgtype_with_no_command() -> None:
    device = A1783()

    assert device.layout is not None
    assert device.layout.check(0x999, b"") == 0x04


def test_a1783_layout_ignores_a_msgtype_with_no_telemetry() -> None:
    """``build``/``update`` touch only the typed messages; any other is a no-op."""
    device = A1783()

    assert device.layout is not None
    assert device.layout.build(0x999, {"display_switch": 1}) == b""
    assert device.layout.update(0x999, b"payload", {"display_switch": 1}) == b"payload"


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
    assert tou["usage_mode"] == 1
    assert tou["backup_soc"] == 25
    assert tou["tou_mode_schedule"] == bytes.fromhex("02030017011718")
    assert (tou["backup_charge_soc"], tou["backup_discharge_soc"]) == (95, 5)
    settings = decode_field(status, "a4", solix_c_gen2.Settings)
    assert (settings.max_soc, settings.min_soc) == (95, 5)


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
    assert screen["day_start_minutes"] == 480
    assert screen["lcd_repeat_cycle"] == 0x24
    assert screen["day_brightness_level"] == 1
    assert screen["night_end_minutes"] == 480  # untouched by the write
    fields = Parameters.parse(read[0].plaintext)
    head = decode_field(fields, "a2", solix_c_gen2.ClockScreenHead)
    assert (head.screen_saver_switch, head.lcd_theme) == (True, 2)
    assert head.day_start_minutes == 480
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
    assert flags.support_pps_tou


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
    device.set_values(exp_1_soc=42, device_1_mode=1, device_1_output_power=300)
    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    block = decode_field(status, "c0", solix_c_gen2.Expansion)
    assert status["c0"]["length"] == 44  # the attached shape is kept
    assert block["exp_1_switch_status"] == 0
    assert block["exp_1_soc"] == 42
    assert block["exp_1_sn"] == b"APCDKJM0000000002"
    assert block["exp_1_type"] == b"A1783_2kWh"
    alternator = decode_field(status, "ce", solix_c_gen2.Alternator)
    assert alternator["device_1_mode"] == 1
    assert alternator["device_1_output_power"] == 300


def test_a1785_unattached_expansion_block_keeps_its_short_shape() -> None:
    device = A1785()

    device.set_values(exp_1_temperature=23, exp_1_connection_status=1)

    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    status = Parameters.parse(push.payload)
    block = decode_field(status, "c0", solix_c_gen2.Expansion)
    assert status["c0"]["length"] == 35
    assert block["exp_1_sn"] == bytes(16)
    assert block["exp_1_temperature"] == 23
    assert block["exp_1_connection_status"] == 1
    assert block["exp_1_type"] == b"  "


def test_the_c1000_gen_2_has_no_expansion_port() -> None:
    """``c0``/``ce``/``4104`` are the C2000 chassis's; ``0490`` still names the pack."""
    c1000, c2000 = A1763(), A1783()

    assert c1000.layout is not None
    assert c2000.layout is not None
    assert not c1000.layout.has_command(0x104)
    assert c1000.layout.locate("exp_1_soc") == ()
    assert c1000.layout.locate("device_1_mode") == ()
    with pytest.raises(LayoutError):
        c1000.set_values(device_1_mode=1)
    assert c2000.layout.has_command(0x104)
    assert c2000.layout.locate("exp_1_soc") == (0x421, solix_c_gen2.MSGTYPE_900)


def test_partial_tou_and_clock_writes_mirrors_and_a_malformed_schedule() -> None:
    device = A1783()
    layout = device.layout
    assert layout is not None

    # a7 alone derives the count; a6 alone leaves the periods zero.
    assert layout.state_changes(0x090, bytes.fromhex("a10121a2020101")) == {
        "usage_mode": 1
    }
    assert layout.state_changes(0x090, bytes.fromhex("a10121a70404030017")) == {
        "tou_mode_schedule": bytes.fromhex("01030017")
    }
    assert layout.state_changes(0x090, bytes.fromhex("a10121a6020101")) == {
        "tou_mode_schedule": bytes.fromhex("01000000")
    }
    # A locate blink changes no field; a theme-id write touches only its leaf.
    assert layout.state_changes(0x104, bytes.fromhex("a10121a5020101")) == {}
    assert layout.state_changes(0x091, bytes.fromhex("a10121a3050304030201")) == {
        "clock_theme_id": 0x01020304
    }
    device.set_values(backup_charge_soc=90)
    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    settings = decode_field(Parameters.parse(push.payload), "a4", solix_c_gen2.Settings)
    assert settings.max_soc == 90
    with pytest.raises(TypeError):
        device.set_values(tou_mode_schedule=b"\x02\x03")
    with pytest.raises(TypeError):
        device.set_values(tou_mode_schedule=2)


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

    device.set_values(ota_allowed=0, ota_block_reason=4)

    values = device.negotiated_module.values
    push = device.negotiated_module.mcu.push(0x489, values=values)
    reply = device.negotiated_module.mcu.respond(0x089, values=values)[0]
    for payload in (push.payload, reply.payload):
        fields = Parameters.parse(payload)
        assert decode_field(fields, "a2", ui(1)) == 0
        assert decode_field(fields, "a3", ui(1)) == 4
    assert reply.payload.startswith(b"\x00\xa1\x01\x31")
