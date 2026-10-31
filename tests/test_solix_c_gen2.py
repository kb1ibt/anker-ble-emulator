# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C Gen 2 line (A1763, A1765, A1783, A1785): one build, four models."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

import pytest
from bleak import BleakClient
from construct import ConstructError, GreedyBytes, Int8ul

from anker_ble_emulator import (
    A1763,
    A1765,
    A1783,
    A1785,
    EmulatedBleakBackend,
    ManualClock,
    ModuleBuild,
)
from anker_ble_emulator.c_g2_490 import MSGTYPE_490, PUSH_490_LAYOUT
from anker_ble_emulator.devices import COMPANY_ID, Profile, solix_c_gen2
from anker_ble_emulator.frame import decode
from anker_ble_emulator.layouts import LayoutError, ui
from anker_ble_emulator.parameters import Parameter, Parameters, decode_field
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.cgen2 import (
    authorized_client,
    backup_plan,
    backup_tail,
    command,
    next_0421,
    next_0490,
    pushed,
    socket_and_ports,
    tou_on_the_cloud,
)
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
    from anker_ble_emulator.module import Output

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
#: The bench unit: mains in at 1290 W with Battery-Only Power off, main pack
#: 95.0 % (c490 ``pack_soc``), min SoC 5 %.
TOU_UNIT = {
    "pack_soc": 950,
    "discharge_limit_value": 5,
    "ac_input_status": 1,
    "ac_input_power": 1290,
    "ac_input_disable_switch": 0,
}
#: (``4090`` mode and record, the ``0022`` UTC, values over ``TOU_UNIT``) ->
#: the cloud ``0421``'s (``d9[0]``, ``a7`` AC input power, ``a1``). The plan's
#: reserve is 50 %.
TOU_HOLD_CASES = [
    pytest.param("01", "030018", UTC_16_21_00, {}, (3, 1290, "34"), id="off-peak"),
    pytest.param("01", "030f02", UTC_16_26_00, {}, (0, 1290, "34"), id="wrapping"),
    pytest.param("00", "010f10", UTC_15_44_27, {}, (0, 1290, "34"), id="standard"),
    pytest.param(
        "01",
        "010f10",
        UTC_15_44_27,
        {"ac_input_status": 0},
        (0, 1290, "34"),
        id="no-mains",
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
        {"pack_soc": 400},
        (1, 1290, "34"),
        id="doc-gate-below-threshold",
    ),
    pytest.param(
        "01", "020f10", UTC_15_44_27, {}, (2, 0, "34"), id="doc-gate-mid-peak"
    ),
]
#: (values over ``TOU_UNIT``, None to leave one unset; the ``4090`` reserve) ->
#: the cloud ``0421``'s ``a7`` AC input power in an active Peak period, held
#: while ``pack_soc`` >= T = min(1000, 10 x max(reserve, min + 5)).
PACK_SOC_HOLD_CASES = [
    pytest.param({"pack_soc": 499}, 50, 1290, id="below-500"),
    pytest.param({"pack_soc": 500}, 50, 0, id="at-500"),
    pytest.param({"pack_soc": 1000, "battery_level": 0}, 50, 0, id="battery-level-low"),
    pytest.param(
        {"pack_soc": 499, "battery_level": 100}, 50, 1290, id="battery-level-high"
    ),
    pytest.param(
        {"pack_soc": 149, "discharge_limit_value": 10}, 5, 1290, id="below-min-plus-5"
    ),
    pytest.param(
        {"pack_soc": 150, "discharge_limit_value": 10}, 5, 0, id="at-min-plus-5"
    ),
    pytest.param({"pack_soc": 999}, 100, 1290, id="below-1000"),
    pytest.param({"pack_soc": 1000}, 100, 0, id="at-1000"),
    pytest.param({"pack_soc": 1000}, 0xFF, 0, id="the-1000-cap"),
]
#: What the unit publishes to MQTT on the cloud, and its ``a1``
#: (a1783-telemetry.md "MQTT channel routing").
MQTT_MESSAGES = [
    pytest.param(0x421, "34", id="0421"),
    pytest.param(0x489, "34", id="0489"),
    pytest.param(0x900, "34", id="0900"),
    pytest.param(0x889, "34", id="0889"),
    pytest.param(0x892, "34", id="0892"),
    pytest.param(0x425, "12", id="0425"),
]
#: a1783-telemetry.md "0503" worked example (2026-10-21 17:49:04 EDT), its
#: Time Save the packaged frames' fixed clock (``tools/sanitize_frames.py``).
MQTT_0503 = bytes.fromhex(
    "ff092f0003010f0503a10112a219040000000000000000d200000030070000820000006e"
    "140000a3050380a2bd6a92"
)
#: ``4090 a7`` Peak records: all day, and 15:00-16:00 (outside a 10:00 RTC).
TOU_ALL_DAY = "010018"
TOU_PEAK_15 = "010f10"
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
async def test_the_device_point_switch_gates_only_the_0490(model: Model) -> None:
    """a1783-telemetry.md "When the display board posts c490": the switch on.

    On by default; off holds back the ``0490`` push and no other; on again
    brings it back.
    """
    device = model()
    pushed = []
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        for switch in (None, 0, 1):
            if switch is not None:
                device.set_values(device_point_switch=switch)
            start = len(link.replies)
            device.push(0x490)
            device.push(0x421)
            await settle()
            pushed.append([reply.frame.cmd.msgtype for reply in link.replies[start:]])

    assert pushed == [[0x490, 0x421], [0x421], [0x490, 0x421]]


@pytest.mark.parametrize("model", MODELS)
async def test_a_4057_stop_holds_back_the_0490_and_a_start_leaves_it(
    model: Model,
) -> None:
    """a1783-telemetry.md "What stops and restores c490": ``4057`` ``a2 = 0``.

    A bare ``4057`` and a later start (``a2 = 1``) leave the switch as it was;
    each is acked with the recorded ``4857``.
    """
    device = model()
    requests = [
        [(0xA1, b"\x21")],
        [(0xA1, b"\x21"), (0xA2, b"\x01\x00")],
        [(0xA1, b"\x21"), (0xA2, b"\x01\x01"), (0xA3, bytes.fromhex("022c01"))],
    ]
    acks, pushed = [], []
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        for fields in requests:
            replies = await link.send(0x057, fields, SESSION)
            acks.append([(cmd_hex(reply.frame), reply.plaintext) for reply in replies])
            start = len(link.replies)
            device.push(0x490)
            device.push(0x421)
            await settle()
            pushed.append([reply.frame.cmd.msgtype for reply in link.replies[start:]])

    assert acks == [[("4857", bytes.fromhex("00a10131"))]] * 3
    assert pushed == [[0x490, 0x421], [0x421], [0x421]]


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("value", [2, b"\x01"])
def test_the_device_point_switch_is_a_0490_switch(model: Model, value: Value) -> None:
    """A hidden 0/1 input on the ``0490``, which no field carries."""
    device = model()
    layout = device.layout
    assert layout is not None

    assert layout.locate("device_point_switch") == (MSGTYPE_490,)
    assert "device_point_switch" in layout.names(MSGTYPE_490)
    with pytest.raises(TypeError, match="device_point_switch"):
        device.set_values(device_point_switch=value)


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
    stored = decode_field(Parameters.parse(setter[1].plaintext), "a4", GreedyBytes)
    assert int.from_bytes(stored[8:12], "little") == 3600
    # The socket switched off discards the timer (a1783-control.md "Output timers").
    settings = decode_field(status, "a4", solix_c_gen2.Settings)
    assert settings.car_charger_timer_value == 0
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
    ("model", "head"),
    [
        pytest.param(
            A1763,
            b"[6AA730DA]AXDDK96000000000,,;.2.6,;.2.6,1.9.6,5.0.5,5.0.5,:.3.5,0.0.0,"
            b"2.2.0,3.1.0",
            id="A1763-v1.1.2.6",
        ),
        pytest.param(
            A1765,
            b"[6AA730DA]AXDDK96000000000,,;.2.6,;.2.6,1.9.6,5.0.5,5.0.5,:.3.5,0.0.0,"
            b"2.2.0,3.1.0",
            id="A1765-v1.1.2.6",
        ),
        pytest.param(
            A1783,
            b"[6AA730DA\x00APCDKKE0000000001,1.2.1.6,1.2.1.6,1.0.0.0,0.0.0.0,0.1.2.6,"
            b"0.1.2.6,1.9.3.3,1.9.3.3,0.3.1.0,0.3.1.0,0.3.3.0,0.1.9.6,0.0.0.0",
            id="A1783-v1.2.1.6",
        ),
        pytest.param(
            A1785,
            b"[6AA730DA]AXDDKVP000000000,,<.1.1,<.1.1,1.9.6,1.2.6,1.2.6,C.2.2,0.0.0,"
            b"3.0.6,3.1.0",
            id="A1785-v1.2.1.1",
        ),
    ],
)
def test_log_chunk_0_follows_the_reported_mcu_version(
    model: Model, head: bytes
) -> None:
    """a1783-control.md "BLE log upload": chunk 0 in the reported build's layout.

    From MainMcu v1.2.1.6: ``[`` RTC ``00``, a 17-byte SN, 13 ``d.d.d.d`` fields
    (fw, indices 0-11). Below it: ``[RTC]``, 16 bytes of SN, ``,,``, 9 fields of
    ``'0'+v/100 . v%100/10 . v%10`` (fw, indices 0, 10, 3, 4, 5, 6, 9, 7).
    """
    header = head.ljust(249, b" ") + b"\n"

    frames = model().mcu.respond(
        0x066, request=bytes.fromhex("a10121a203020000"), rtc=0x6AA730DA
    )

    assert [(frame.cmd.msgtype, frame.payload.hex()) for frame in frames] == [
        (0x866, "00a10131a203020000a30302fa00a4fb04" + header.hex())
    ]


def test_log_chunk_0_fields_sit_at_the_v1_2_1_6_offsets() -> None:
    """a1783-control.md "BLE log upload" v1.2.1.6 column, field by field."""
    frames = A1783().mcu.respond(
        0x066, request=bytes.fromhex("a10121a203020000"), rtc=0x6AA730DA
    )
    data = decode_field(Parameters.parse(frames[0].payload), "a4", GreedyBytes)
    versions = [data[28 + 8 * k : 35 + 8 * k] for k in range(13)]

    assert len(data) == 250
    assert data[0:1] == b"["
    assert data[1:9] == b"6AA730DA"
    assert data[9] == 0
    assert data[10:27] == b"APCDKKE0000000001"
    assert [data[27 + 8 * k : 28 + 8 * k] for k in range(13)] == [b","] * 13
    assert versions == [
        b"1.2.1.6",  # fw (otaVer)
        b"1.2.1.6",  # 0 mcuVer
        b"1.0.0.0",  # 1 bootloader constant
        b"0.0.0.0",  # 2 no case
        b"0.1.2.6",  # 3 acdcVer = f9 inv_version
        b"0.1.2.6",  # 4 dcdcVer = f9 sub_mcu_version
        b"1.9.3.3",  # 5 bmsVer
        b"1.9.3.3",  # 6 SubBMS
        b"0.3.1.0",  # 7 pd1Ver
        b"0.3.1.0",  # 8 pd2Ver
        b"0.3.3.0",  # 9 bleVer = f9 module_version
        b"0.1.9.6",  # 10 lcdVer
        b"0.0.0.0",  # 11 lcdBoot, runtime only
    ]
    assert data[131:249] == b" " * 118
    assert data[249:] == b"\n"


def test_log_chunk_0_fields_sit_at_the_v1_2_1_1_offsets() -> None:
    """a1783-control.md "BLE log upload" v1.2.1.1 column, field by field."""
    frames = A1785().mcu.respond(
        0x066, request=bytes.fromhex("a10121a203020000"), rtc=0x6AA730DA
    )
    data = decode_field(Parameters.parse(frames[0].payload), "a4", GreedyBytes)
    versions = [data[28 + 6 * k : 33 + 6 * k] for k in range(9)]

    assert len(data) == 250
    assert data[0:10] == b"[6AA730DA]"
    assert data[10:26] == b"AXDDKVP000000000"
    assert data[26:28] == b",,"
    assert [data[27 + 6 * k : 28 + 6 * k] for k in range(9)] == [b","] * 9
    assert versions == [
        b"<.1.1",  # fw 1211: '0' + 12 is '<'
        b"<.1.1",  # 0 mcuVer
        b"1.9.6",  # 10 lcdVer
        b"1.2.6",  # 3 acdcVer
        b"1.2.6",  # 4 dcdcVer
        b"C.2.2",  # 5 bmsVer 1922: '0' + 19 is 'C'
        b"0.0.0",  # 6 SubBMS, no expansion
        b"3.0.6",  # 9 bleVer
        b"3.1.0",  # 7 pd1Ver
    ]
    assert data[81:249] == b" " * 168
    assert data[249:] == b"\n"


@pytest.mark.parametrize(
    ("mcu_version", "closing"),
    [
        pytest.param(bytes([5, 1, 2, 1]), b"]", id="v1.2.1.5"),
        pytest.param(bytes([6, 1, 2, 1]), b"\x00", id="v1.2.1.6"),
    ],
)
def test_log_chunk_0_switches_layout_at_v1_2_1_6(
    mcu_version: bytes, closing: bytes
) -> None:
    """The layout follows a set ``mcu_version``; v1.2.1.6 is the first new build."""
    device = A1785()
    device.set_values(mcu_version=mcu_version)

    frames = device.mcu.respond(
        0x066,
        request=bytes.fromhex("a10121a203020000"),
        values=device.negotiated_module.values,
        rtc=0x6AA730DA,
    )
    data = decode_field(Parameters.parse(frames[0].payload), "a4", GreedyBytes)

    assert data[9:10] == closing


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
    assert decode_field(Parameters.parse(chunk.payload), "a4", GreedyBytes)[10:27] == (
        b"APCDKKE0000000009"
    )
    with pytest.raises(TypeError, match="device_sn is a bytes value"):
        layout.reply(0x066, request, state)


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
    assert data[:10] == b"[6AA73134\x00"


def test_a_clock_set_before_the_sync_floor_is_acked_but_never_reaches_the_rtc() -> None:
    """a1783-control.md "Clock": ``set_utc_time_to_mcu`` drops UTC < 0x6561aca0.

    The ``a3`` of 0 the floor set carries leaves the handshake's offset in place.
    """
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
    assert module.rtc == 0x6561ACA0 - 14400


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

    Battery-Only Power blocks the period and AC input (0 W); switched off at
    15:44:27 (the app's cloud ``0101``, set here as a value), Peak goes active
    and holds AC input at 0 W; at 16:00 the period ends, and by 16:00:13 the
    charge the switch-off started has ramped in: the expansion, below its
    cap, at 1208 W.
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
        await link.send(0x101, [(0xA1, b"\x21"), (0xAB, b"\x02\x01\x00")], SESSION)
    device.set_cloud(True)

    blocked = decode(device.mqtt_frame(0x421))
    clock.advance(27)
    device.set_values(ac_input_disable_switch=0)
    held = decode(device.mqtt_frame(0x421))
    clock.advance(16 * 60 - 27 + 13)
    device.negotiated_module.check_pushes()
    ended = decode(device.mqtt_frame(0x421))

    states = []
    for frame in (blocked, held, ended):
        fields = Parameters.parse(frame.payload)
        states.append(
            (
                decode_field(fields, "d9", solix_c_gen2.Tou).tou_active_period,
                decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power,
                decode_field(fields, "a1", GreedyBytes).hex(),
            )
        )
    assert states == [(0, 0, "34"), (1, 0, "34"), (0, 1208, "34")]


#: ``0022`` at 15:44:00 EDT, and ``4090`` Time-of-Use with a 50 % reserve over
#: Peak 15-16, or Peak 15-16 then Mid-Peak 16-17.
CLOCK_15_44_00 = [(0xA1, UTC_15_44_00), (0xA3, EDT)]
TOU_HEAD = [(0xA2, b"\x01\x01"), (0xA5, b"\x01\x32")]
PEAK_15_16 = [*TOU_HEAD, (0xA6, b"\x01\x01"), (0xA7, bytes.fromhex("04010f10"))]
PEAK_THEN_MID_PEAK = [
    *TOU_HEAD,
    (0xA6, b"\x01\x02"),
    (0xA7, bytes.fromhex("07010f10021011")),
]
#: Seconds from 15:44:00 to the 16:00:00 boundary.
TO_16_00 = 960


def test_the_peak_end_ramps_ac_input_back_replays_2026_10_21() -> None:
    """Cloud ``0421`` 2026-10-21 15:59:59-16:00:13 (a1783-control.md "TOU" effects).

    ``d9[0]`` 1 -> 0 at 16:00:00; AC input (``a7[4:6]``) stays 0 W to 16:00:05
    and reads 86 / 57 / 178 / 770 W at :07 / :08 / :09 / :11, 1290 W at :13.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, PEAK_15_16)
    readings = []

    for at in (-1, 1, 5, 7, 8, 9, 11, 13):
        clock.advance(TO_16_00 + at - clock.now())
        fields = Parameters.parse(decode(device.mqtt_frame(0x421)).payload)
        readings.append(
            (
                decode_field(fields, "d9", solix_c_gen2.Tou).tou_active_period,
                decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power,
                decode_field(fields, "a6", solix_c_gen2.MainData).ac_input_power,
            )
        )

    assert readings == [
        (1, 0, 0),
        (0, 0, 0),
        (0, 0, 0),
        (0, 86, 86),
        (0, 57, 57),
        (0, 178, 178),
        (0, 770, 770),
        (0, 1290, 1290),
    ]


def test_a_period_that_held_nothing_ends_with_no_ramp() -> None:
    """Below the 50 % threshold Peak doesn't hold, so its end changes nothing."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**TOU_UNIT, "pack_soc": 400})
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, PEAK_15_16)

    clock.advance(TO_16_00 + 1)
    fields = Parameters.parse(decode(device.mqtt_frame(0x421)).payload)

    assert decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power == 1290


def test_a_held_period_running_into_another_keeps_holding() -> None:
    """Peak 15-16 then Mid-Peak 16-17: still held at 16:00:09, no ramp."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, PEAK_THEN_MID_PEAK)

    clock.advance(TO_16_00 + 9)
    fields = Parameters.parse(decode(device.mqtt_frame(0x421)).payload)

    assert decode_field(fields, "d9", solix_c_gen2.Tou).tou_active_period == 2
    assert decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power == 0


def test_a_clock_set_retimes_the_ramp() -> None:
    """The ramp follows the RTC: set back to 15:44:00 at 16:00:09, Peak holds again."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, PEAK_15_16)
    clock.advance(TO_16_00 + 9)
    device.set_cloud(False)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, PEAK_15_16)
    readings = []

    for at in (-1, 11):
        clock.advance(TO_16_00 + 9 + TO_16_00 + at - clock.now())
        fields = Parameters.parse(decode(device.mqtt_frame(0x421)).payload)
        readings.append(
            decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power
        )

    assert readings == [0, 770]


async def test_over_ble_no_period_activates() -> None:
    """a1783-control.md "TOU (4090)" condition 2: off the cloud no period is active.

    A unit on BLE is never bound on WiFi too, so Peak at 15:44:27 holds nothing.
    """
    device = A1783(clock=ManualClock())
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
        await link.send(0x022, [(0xA1, UTC_15_44_27), (0xA3, EDT)])
        await link.send(0x090, plan, SESSION)
        status = await link.send(0x100, [(0xA1, b"\x21")], SESSION)

    fields = Parameters.parse(status[-1].plaintext)
    assert (
        decode_field(fields, "d9", solix_c_gen2.Tou).tou_active_period,
        decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power,
        decode_field(fields, "a1", GreedyBytes).hex(),
    ) == (0, 1290, "31")


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
    device.set_cloud(True)

    fields = Parameters.parse(decode(device.mqtt_frame(0x421)).payload)
    assert (
        decode_field(fields, "d9", solix_c_gen2.Tou).tou_active_period,
        decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power,
        decode_field(fields, "a1", GreedyBytes).hex(),
    ) == expected


def test_the_c1000_gen_2_shares_the_peak_hold() -> None:
    """The A1763 (no ``ce``, MainMcu v1.2.1.1) runs the line's own TOU check."""
    clock = ManualClock()
    device = A1763(clock=clock)
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
    clock.advance(10 * 3600)
    device.set_cloud(True)
    fields = Parameters.parse(decode(device.mqtt_frame(0x421)).payload)

    assert decode_field(fields, "d9", solix_c_gen2.Tou).tou_active_period == 1
    assert decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power == 0


@pytest.mark.parametrize(("values", "reserve", "ac_input_power"), PACK_SOC_HOLD_CASES)
def test_the_peak_hold_compares_the_main_pack_soc(
    values: dict[str, Value | None], reserve: int, ac_input_power: int
) -> None:
    """a1783-control.md "TOU" effects: held while c490 ``pack_soc`` >= T, at 0.1 %.

    ``0421 a5`` ``battery_level`` plays no part; unset, ``pack_soc`` is the
    recorded ``0490``'s (1000).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    layout = device.layout
    assert layout is not None
    module = device.negotiated_module
    unit = {**TOU_UNIT, **values}
    device.set_values(**{name: v for name, v in unit.items() if v is not None})
    plan = f"a10121a2020101a50201{reserve:02x}a6020101a70404{TOU_ALL_DAY}"
    state = module.mcu.state({})
    device.set_values(**layout.state_changes(0x090, bytes.fromhex(plan), state))
    clock.advance(10 * 3600)
    device.set_cloud(True)

    fields = Parameters.parse(decode(device.mqtt_frame(0x421)).payload)
    assert decode_field(fields, "d9", solix_c_gen2.Tou).tou_active_period == 1
    assert (
        decode_field(fields, "a7", solix_c_gen2.AcOutput).ac_input_power
        == ac_input_power
    )


def test_the_cloud_and_the_power_stage_input_are_no_values() -> None:
    """The cloud is the device's state (``set_cloud``), never a telemetry value.

    TOU condition 3 reads ``a7[3]`` ``ac_input_status``; no hidden value backs it.
    """
    device = A1783()
    for name in ("wifi_online", "cloud", "power_stage_ac_input"):
        with pytest.raises(LayoutError):
            device.set_values(**{name: 1})


@pytest.mark.parametrize("model", MODELS)
def test_no_ble_frame_carries_a_cloud_route_or_message(model: Model) -> None:
    """a1783-telemetry.md "BLE / cloud exclusivity": no cloud route or ``0503``.

    Every push and reply the MCU sends the module routes ``a1 31``, even with a
    Peak period due; ``a1 34`` / ``12`` and ``0503`` are MQTT's alone.
    """
    clock = ManualClock()
    device = model(clock=clock)
    layout = device.layout
    assert layout is not None
    module = device.negotiated_module
    device.set_values(**TOU_UNIT)
    plan = bytes.fromhex("a10121a2020101a5020132a6020101a70404010018")
    device.set_values(**layout.state_changes(0x090, plan, module.mcu.state({})))
    clock.advance(10 * 3600)
    script = module.mcu
    frames = [
        *(
            script.push(push, values=module.values, rtc=module.rtc)
            for push in script.pushes
        ),
        *(
            frame
            for request in script.replies
            for frame in script.respond(request, values=module.values, rtc=module.rtc)
        ),
    ]

    assert solix_c_gen2.MSGTYPE_503 not in {frame.cmd.msgtype for frame in frames}
    routes = {
        PUSH_490_LAYOUT.parse(frame.payload).route.route
        if frame.cmd.msgtype == MSGTYPE_490
        else Int8ul.parse(Parameters.parse(frame.payload)["a1"]["value"])
        for frame in frames
    }
    assert routes == {0x31}


@pytest.mark.parametrize(("msgtype", "route"), MQTT_MESSAGES)
def test_the_cloud_frame_is_the_ble_one_under_its_mqtt_route(
    msgtype: int, route: str
) -> None:
    """a1783-telemetry.md "MQTT channel routing": the BLE frame under its MQTT ``a1``.

    A plain ``03 01 0f`` frame with no link flags, field for field the one the
    MCU sends to BLE.
    """
    device = A1783()
    script = device.mcu
    ble = script.pushes.get(msgtype) or next(
        frame
        for frames in script.replies.values()
        for frame in frames
        if frame.cmd.msgtype == msgtype
    )
    device.set_cloud(True)

    frame = decode(device.mqtt_frame(msgtype))

    ble_fields = Parameters.parse(ble.payload)
    fields = Parameters.parse(frame.payload)
    assert (frame.pattern.composer, frame.pattern.channel) == (0x01, 0x0F)
    assert (frame.cmd.encrypted, frame.cmd.fragmented) == (False, False)
    assert decode_field(fields, "a1", GreedyBytes).hex() == route
    assert fields.prefix == ble_fields.prefix
    assert {tag: Parameter.build(f) for tag, f in fields.items() if tag != "a1"} == {
        tag: Parameter.build(f) for tag, f in ble_fields.items() if tag != "a1"
    }


def test_the_cloud_frame_needs_the_cloud_and_a_cloud_message() -> None:
    """``mqtt_frame`` builds only on the cloud, and not ``0490`` (HTTPS) or unknowns."""
    device = A1783()
    with pytest.raises(RuntimeError, match="set_cloud"):
        device.mqtt_frame(0x421)
    device.set_cloud(True)
    for msgtype in (0x490, 0x999):
        with pytest.raises(LayoutError, match=f"{msgtype:04x}"):
            device.mqtt_frame(msgtype)
    with pytest.raises(LayoutError, match="0425"):
        A1763().mcu.mqtt(0x425)


@pytest.mark.parametrize("model", MODELS)
def test_the_0503_totals_are_the_doc_example_and_settable(model: Model) -> None:
    """a1783-telemetry.md "0503": cloud only, ``a1 12``; the values set show next.

    Built in an active Peak period, the only time the firmware sends it.
    """
    clock = ManualClock()
    device = model(clock=clock)
    layout = device.layout
    assert layout is not None
    module = device.negotiated_module
    device.set_values(**TOU_UNIT)
    plan = bytes.fromhex(f"a10121a2020101a5020132a6020101a70404{TOU_ALL_DAY}")
    device.set_values(**layout.state_changes(0x090, plan, module.mcu.state({})))
    clock.advance(10 * 3600)
    device.set_cloud(True)

    seeded = device.mqtt_frame(0x503)
    device.set_values(peak_time=1850, battery_to_load_energy=211, time_save=7)
    totals = Parameters.parse(decode(device.mqtt_frame(0x503)).payload)

    assert seeded == MQTT_0503
    assert solix_c_gen2.MSGTYPE_503 not in device.mcu.pushes
    assert decode_field(totals, "a2", solix_c_gen2.TouTotals) == {
        "solar_to_battery_energy": 0,
        "grid_to_battery_energy": 0,
        "battery_to_load_energy": 211,
        "peak_time": 1850,
        "mid_peak_time": 130,
        "off_peak_time": 5230,
    }
    assert decode_field(totals, "a3", ui(4)) == 7


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize(
    ("mode", "record"),
    [
        pytest.param("00", TOU_ALL_DAY, id="tou-off"),
        pytest.param("01", TOU_PEAK_15, id="outside-the-window"),
    ],
)
def test_no_0503_outside_an_active_tou_period(
    model: Model, mode: str, record: str
) -> None:
    """a1783-telemetry.md "0503" trigger: sent only in an active TOU period.

    At 10:00 RTC, TOU off or a 15-16 Peak window leaves ``d9[0]`` 0; an
    all-day Peak window then publishes the doc example.
    """
    clock = ManualClock()
    device = model(clock=clock)
    layout = device.layout
    assert layout is not None
    module = device.negotiated_module
    device.set_values(**TOU_UNIT)
    plan = bytes.fromhex(f"a10121a20201{mode}a5020132a6020101a70404{record}")
    device.set_values(**layout.state_changes(0x090, plan, module.mcu.state({})))
    clock.advance(10 * 3600)
    device.set_cloud(True)

    with pytest.raises(RuntimeError, match="0503"):
        device.mqtt_frame(0x503)
    active = bytes.fromhex(f"a10121a2020101a5020132a6020101a70404{TOU_ALL_DAY}")
    state = module.mcu.state(module.values)
    device.set_values(**layout.state_changes(0x090, active, state))
    assert device.mqtt_frame(0x503) == MQTT_0503


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
                (tou.backup_mode, tou.tou_periods.hex(), tou.tou_active_period)
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


def test_a1783_reads_a_grid_loss_report_as_its_soc() -> None:
    """A ``0x59`` ``0425`` reads as its SoC: a1783-control.md, BLE 10-23 06:02:38."""
    layout = A1783().layout
    assert layout is not None

    values = layout.read(0x425, bytes.fromhex("a10131a2020159a302045f"))

    assert values == {"event_code": 0x59, "event_soc": 95}


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


#: The 2026-10-26 Kasa run (a1783-control.md "``a4[31]`` witnesses"): Battery-Only
#: Power on, the outage alert on, mains in, the main pack at 18 % under a 95 % cap
#: and on the bus (``a5[1]`` 2 from 15:29:01), the expansion attached (its 5 %
#: left at the recording's 90 %, which keeps the OTA precheck still). Its
#: discharge latch counts from the main's own SoC.
KASA_UNIT: dict[str, Value] = {
    "ac_input_disable_switch": 1,
    "outage_alert_switch": True,
    "ac_input_status": 2,
    "ac_input_power": 0,
    "battery_level": 18,
    "charging_limit_value": 95,
    "screen_switch": 0,
    "pack_on_bus": 1,
    "discharge_latch_from": 180,
}
#: The ``4425`` the Kasa run's grid loss drew at 15:28:49: event 0x59, SoC 18 %.
GRID_LOSS_4425 = ("4425", "a10131a2020159a3020412")


def test_kasa_toggle_replays_bench() -> None:
    """a1783-control.md "``a4[31]`` witnesses", 2026-10-26 15:28:45-15:29:05 EDT.

    Mains off for 6 s: ``a7[3]`` 2 -> 0, the flag's 0.1 s debounce, then
    ``a4[31]`` ``06`` -> ``0a`` with the screen woken (one frame at 15:28:45);
    the 0x59 alert on the 5th tick (:49); mains back at :51, the accept at :54;
    the charge enable 8 s on at 118 W (15:29:01), ``a3[0]`` 2 (:02), the full
    charge 5 s after the enable (:05).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**KASA_UNIT)
    client = authorized_client(device, TOKEN)
    outputs: list[Output] = []
    device.listen(outputs.append)
    module = device.negotiated_module
    timeline = []

    def note() -> None:
        values, fields = next_0421(device)
        a4 = decode_field(fields, "a4", GreedyBytes)
        a6 = decode_field(fields, "a6", GreedyBytes)
        a7 = decode_field(fields, "a7", GreedyBytes)
        timeline.append(
            (
                f"{a4[31]:02x}",
                a4[21],
                a7[3],
                a6[2:4].hex(),
                a7[4:6].hex(),
                values["work_status"],
                values["charge_discharge_status"],
                pushed(client, outputs),
            )
        )
        outputs.clear()

    def step(seconds: float) -> None:
        clock.advance(seconds)
        outputs.append(module.check_pushes())
        note()

    device.set_values(ac_input_status=0)
    for seconds in (0.1, 3.9, 0.1):
        step(seconds)
    clock.advance(1.9)
    device.set_values(ac_input_status=1)
    note()
    for seconds in (2.9, 0.1, 7.9, 0.1, 2, 3):
        step(seconds)

    assert timeline == [
        ("0a", 1, 0, "0000", "0000", 0, 0, []),  # 15:28:45
        ("0a", 1, 0, "0000", "0000", 0, 0, []),
        ("0a", 1, 0, "0000", "0000", 0, 0, [GRID_LOSS_4425]),  # 15:28:49
        ("0a", 1, 1, "0000", "0000", 0, 0, []),  # 15:28:51
        ("0a", 1, 1, "0000", "0000", 0, 0, []),
        ("02", 1, 1, "0000", "0000", 0, 0, []),  # 15:28:54
        ("02", 1, 1, "0000", "0000", 0, 0, []),
        ("02", 1, 1, "7600", "7600", 0, 2, []),  # 15:29:01, 118 W, a5[1] 2
        ("02", 1, 1, "7600", "7600", 2, 2, []),  # 15:29:02, a3[0] 2
        ("02", 1, 1, "b804", "b804", 2, 2, []),  # 15:29:05
    ]


@pytest.mark.parametrize(
    ("state", "ac_input_status"),
    [pytest.param(1, 2, id="on"), pytest.param(0, 1, id="off")],
)
def test_battery_only_sets_ac_input_status_at_once_with_mains_in(
    state: int, ac_input_status: int
) -> None:
    """a1783-control.md 19:39:18 / 19:40:42: ``a4[31]`` state 1 is ``a7[3]`` 2, 0 is 1.

    It shows straight after ``set_values``: no clock step, no push check.
    """
    device = A1783(clock=ManualClock())
    device.set_values(ac_input_status=1, ac_input_disable_switch=1 - state)

    device.set_values(ac_input_disable_switch=state)

    assert next_0421(device)[0]["ac_input_status"] == ac_input_status


def test_battery_only_without_mains_leaves_ac_input_status() -> None:
    device = A1783(clock=ManualClock())
    device.set_values(ac_input_status=0, ac_input_disable_switch=0)

    device.set_values(ac_input_disable_switch=1)

    assert next_0421(device)[0]["ac_input_status"] == 0


def test_a_4101_ab_reply_carries_the_new_ac_input_status() -> None:
    """``4101 ab`` 1 with mains in: its follow-up ``4421`` already reads ``a7[3]`` 2."""
    device = A1783(clock=ManualClock())
    device.set_values(ac_input_status=1, ac_input_disable_switch=0)
    client = authorized_client(device, TOKEN)

    request = client.request(0x101, [(0xA1, b"\x21"), (0xAB, b"\x02\x01\x00")], SESSION)
    out = device.negotiated_module.write(request)
    replies = [reply for data in out.frames if (reply := client.open(data)) is not None]

    assert [cmd_hex(reply.frame) for reply in replies] == ["4901", "4421"]
    a7 = decode_field(Parameters.parse(replies[1].plaintext), "a7", GreedyBytes)
    assert a7[3] == 2


def test_a_value_the_call_sets_wins_over_what_it_implies() -> None:
    """A name the call changes stands: the AC input Battery-Only or no mains drops.

    Held while Battery-Only Power is on, the set power shows once it is off.
    """
    device = A1783(clock=ManualClock())
    device.set_values(ac_input_status=1, ac_input_disable_switch=0, ac_input_power=0)

    device.set_values(ac_input_disable_switch=1, ac_input_power=900)
    held = decode_field(next_0421(device)[1], "a7", GreedyBytes)[4:6]
    device.set_values(ac_input_disable_switch=0)
    released = decode_field(next_0421(device)[1], "a7", GreedyBytes)[4:6]
    device.set_values(ac_input_status=0, ac_input_power=500)
    no_mains = decode_field(next_0421(device)[1], "a7", GreedyBytes)[4:6]

    assert [held.hex(), released.hex(), no_mains.hex()] == ["0000", "8403", "f401"]


def test_a_mains_loss_without_battery_only_moves_only_the_input() -> None:
    """With Battery-Only off and the alert off: ``a7[3]`` 0 and AC in 0 W, no alert."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{
            **KASA_UNIT,
            "ac_input_disable_switch": 0,
            "ac_input_status": 1,
            "outage_alert_switch": False,
            "ac_input_power": 1208,
        }
    )
    client = authorized_client(device, TOKEN)

    device.set_values(ac_input_status=0)
    _, fields = next_0421(device)
    clock.advance(10)
    out = device.negotiated_module.check_pushes()

    a4 = decode_field(fields, "a4", GreedyBytes)
    assert (a4[31], a4[21]) == (0x00, 0)
    assert decode_field(fields, "a7", GreedyBytes)[3:6] == bytes(3)
    assert pushed(client, [out]) == []


def test_mains_lost_again_before_the_clear_keeps_the_auto_off() -> None:
    """Back for 2 s only: the next loss settles the flag at 0 before it returns.

    No accept, and so no charge enable.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**KASA_UNIT, "outage_alert_switch": False})

    device.set_values(ac_input_status=0)
    device.set_values(ac_input_status=1)
    clock.advance(2)
    device.negotiated_module.check_pushes()
    device.set_values(ac_input_status=0)
    clock.advance(12)
    device.negotiated_module.check_pushes()
    values = device.negotiated_module.values

    assert (values["ac_input_disable_switch"], values["ac_input_power"]) == (2, 0)


def test_battery_only_back_on_before_the_restart_stops_it() -> None:
    """Off then on again within 7.5 s: the restart finds AC input disabled."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(ac_input_status=1, ac_input_disable_switch=1, battery_level=40)

    device.set_values(ac_input_disable_switch=0)
    clock.advance(3)
    device.negotiated_module.check_pushes()
    device.set_values(ac_input_disable_switch=1)
    clock.advance(5)
    device.negotiated_module.check_pushes()

    assert device.negotiated_module.values["ac_input_power"] == 0


def test_mains_back_with_its_own_ac_input_power_starts_no_charge() -> None:
    """The call's ``ac_input_power`` stands past the charge enable and its ramp.

    Its 300 W charges the main on the bus all the same: ``a3[0]`` 2 once its
    20 samples pass (a1783-telemetry.md "Flow state").
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    unit = {**KASA_UNIT, "ac_input_disable_switch": 0, "ac_input_status": 0}
    device.set_values(**unit)

    device.set_values(ac_input_status=1, ac_input_power=300)
    for _ in range(17):
        clock.advance(1)
        device.negotiated_module.check_pushes()
    values, fields = next_0421(device)

    assert (values["ac_input_power"], values["work_status"]) == (300, 2)
    assert decode_field(fields, "a6", GreedyBytes)[2:4] == (300).to_bytes(2, "little")


def test_a_jump_settles_the_sequence_but_sends_no_alert() -> None:
    """A ``ManualClock.jump`` past each step: its values set, its push unsent."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**KASA_UNIT)
    client = authorized_client(device, TOKEN)

    device.set_values(ac_input_status=0)
    clock.jump(10)
    lost = device.negotiated_module.check_pushes()
    device.set_values(ac_input_status=1)
    clock.jump(20)
    back = device.negotiated_module.check_pushes()
    values, fields = next_0421(device)

    assert pushed(client, [lost, back]) == []
    assert decode_field(fields, "a4", GreedyBytes)[31] == 0x02
    assert (values["ac_input_power"], values["work_status"]) == (1208, 2)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        pytest.param({"battery_level": 18}, (1208, 2, 2, 0), id="main-below-cap"),
        pytest.param(
            {"battery_level": 95, "sub_package_battery_level": 37},
            (1208, 0, 0, 2),
            id="main-at-cap-expansion-below",
        ),
        pytest.param(
            {"battery_level": 95, "sub_package_battery_level": 95},
            (0, 0, 0, 0),
            id="both-at-cap",
        ),
        pytest.param(
            {"battery_level": 95, "sub_package_connection_status": 0},
            (0, 0, 0, 0),
            id="main-at-cap-no-expansion",
        ),
    ],
)
def test_ac_charging_after_mains_returns_follows_the_packs_caps(
    values: dict[str, Value], expected: tuple[int, int, int, int]
) -> None:
    """a1783-telemetry.md "Flow state"; a1783-control.md 06:03:00 (the expansion).

    The main below its cap charges (``a5[1]`` 2, ``a3[0]`` 2 s later); at its
    cap an attached expansion below its own takes the charge (``c0[24]`` 2) and
    the main's states stay 0; with every pack at its cap no AC input comes. Read
    at the ramp's end: the accept 3 s on, the enable 8 s after, the ramp 5 s.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{
            **KASA_UNIT,
            "ac_input_disable_switch": 0,
            "ac_input_status": 0,
            "sub_package_connection_status": 1,
            **values,
        }
    )

    device.set_values(ac_input_status=1)
    clock.advance(16)
    device.negotiated_module.check_pushes()
    state = next_0421(device)[0]

    assert (
        state["ac_input_power"],
        state["work_status"],
        state["charge_discharge_status"],
        state["sub_package_status"],
    ) == expected


def test_the_c1000_gen_2_takes_the_sequence_but_has_no_alert_to_send() -> None:
    """The A1763 records no ``0425``: the loss settles, nothing is pushed.

    Its v1.1.2.6 ``a4`` has no ``[31]`` to show the state, so it is read off
    the device's values.
    """
    clock = ManualClock()
    device = A1763(clock=clock)
    device.set_values(**KASA_UNIT)
    client = authorized_client(device, TOKEN)

    device.set_values(ac_input_status=0)
    clock.advance(5)
    out = device.negotiated_module.check_pushes()

    assert pushed(client, [out]) == []
    assert device.negotiated_module.values["ac_input_disable_switch"] == 2


def test_a6_carries_the_shared_power_names() -> None:
    """a1783-telemetry.md ``a6``: one name per quantity, so both tags carry it."""
    device = A1783()

    device.set_values(
        ac_input_disable_switch=0,
        total_output_power=88,
        ac_input_power=1211,
        pv1_input_power=79,
        remaining_time=3,
        battery_level=95,
    )
    _, fields = next_0421(device)

    assert decode_field(fields, "a6", GreedyBytes).hex() == "5800bb044f0003005f"
    assert decode_field(fields, "a7", GreedyBytes)[4:6].hex() == "bb04"
    assert decode_field(fields, "a8", GreedyBytes)[1:3].hex() == "4f00"
    assert decode_field(fields, "a5", GreedyBytes)[2] == 95


def test_battery_only_on_and_off_replays_the_2026_10_06_frames() -> None:
    """a1783-control.md "``a4[31]`` witnesses", MQTT 19:38:27 -> 19:39:18 -> 19:40:42.

    Charging at 1208 W with mains in; Battery-Only on reads ``a7[3]`` 2 with AC
    input 0 W; off, ``a7[3]`` 1 and still 0 W, until charging restarts 7.5 s
    on (the corpus: 6 and 9 s, its first sample a ramp's start): the power
    stage's 118 W first, the full charge 5 s later.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{
            **KASA_UNIT,
            "ac_input_disable_switch": 0,
            "ac_input_status": 1,
            "ac_input_power": 1208,
            "battery_level": 63,
        }
    )
    timeline = []

    def note() -> None:
        _, fields = next_0421(device)
        a4 = decode_field(fields, "a4", GreedyBytes)
        a6 = decode_field(fields, "a6", GreedyBytes)
        a7 = decode_field(fields, "a7", GreedyBytes)
        timeline.append((f"{a4[31]:02x}", a7[3], a6[2:4].hex(), a7[4:6].hex()))

    note()
    device.set_values(ac_input_disable_switch=1)
    note()
    device.set_values(ac_input_disable_switch=0)
    note()
    clock.advance(7.4)
    device.negotiated_module.check_pushes()
    note()
    clock.advance(0.1)
    device.negotiated_module.check_pushes()
    note()
    clock.advance(5)
    device.negotiated_module.check_pushes()
    note()

    assert timeline == [
        ("02", 1, "b804", "b804"),  # 19:38:27
        ("06", 2, "0000", "0000"),  # 19:39:18
        ("02", 1, "0000", "0000"),  # 19:40:42
        ("02", 1, "0000", "0000"),
        ("02", 1, "7600", "7600"),
        ("02", 1, "b804", "b804"),
    ]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        pytest.param({"total_output_power": 26}, (0, 0, 0, 1), id="ac-only-output-on"),
        pytest.param({"total_output_power": 0}, (0, 0, 0, 0), id="ac-only-no-output"),
        pytest.param(
            {
                "pv1_input_power": 80,
                "pack_on_bus": 1,
                "pack_soc": 900,
                "discharge_latch_from": 900,
            },
            (2, 2, 0, 0),
            id="dc-input-charging",
        ),
    ],
)
async def test_a_held_peak_shows_no_charging_only_ac_could_cause(
    values: dict[str, Value], expected: tuple[int, int, int, int]
) -> None:
    """Cloud ``0421`` 2026-10-21 15:49:53 (a1783-telemetry.md "Flow state").

    In the Peak hold ``a3[0]`` reads 0 and the bus holder's port flow drops to
    the output's: ``c0[24]`` 1 with output on, the expansion holding the bus as
    recorded. A DC input still charging the main on the bus keeps it charging.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{
            **TOU_UNIT,
            "pv1_input_power": 0,
            "total_output_power": 0,
            **values,
        }
    )
    clock.advance(2)  # a3[0] past its 20 samples
    device.negotiated_module.check_pushes()
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
        await link.send(0x022, [(0xA1, UTC_15_44_27), (0xA3, EDT)])
        await link.send(0x090, plan, SESSION)
    device.set_cloud(True)

    fields = Parameters.parse(decode(device.mqtt_frame(0x421)).payload)
    status = decode_field(fields, "a3", solix_c_gen2.Status)
    battery = decode_field(fields, "a5", solix_c_gen2.Battery)
    ac = decode_field(fields, "a6", solix_c_gen2.MainData).ac_input_power
    expansion = decode_field(fields, "c0", solix_c_gen2.Expansion)
    assert decode_field(fields, "d9", solix_c_gen2.Tou).tou_active_period == 1
    assert (
        status.work_status,
        battery.charge_discharge_status,
        ac,
        expansion.sub_package_status,
    ) == expected


def test_socket_taps_replay_the_2026_09_01_reattach() -> None:
    """a1783-control.md "``4102 a2`` and the USB-C ports", app_log 2026-09-01 02:26.

    The on tap (02:26:41.696), PD1 ``[0]`` 1 at 43.096, the off tap at 43.547
    (PD1 holds), 86 W at 46.095 with ``a6[0:2]`` 86 and ``a5[1]`` 1. PD2, already
    attached, and the empty PD3 don't move; nor do ``a3[0]``, ``ae``, ``a7``. The
    main is on the bus, the expansion attached at 56 % (``c0`` 02:26:40.159).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        pack_on_bus=1, discharge_latch_from=1000, sub_package_battery_level=56
    )
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    timeline = [socket_and_ports(next_0421(device)[1])]  # 02:26:40.159
    still = next_0421(device)[0]

    def tap(on: int) -> None:
        request = client.request(
            0x102, [(0xA1, b"\x21"), (0xA2, bytes([0x01, on]))], SESSION
        )
        out = module.write(request)
        replies = [r for data in out.frames if (r := client.open(data)) is not None]
        assert [cmd_hex(reply.frame) for reply in replies] == ["4902", "4421"]
        timeline.append(socket_and_ports(Parameters.parse(replies[1].plaintext)))

    def frame_after(seconds: float) -> None:
        clock.advance(seconds)
        module.check_pushes()
        timeline.append(socket_and_ports(next_0421(device)[1]))

    tap(1)  # 02:26:41.696, its c421 at 42.056
    frame_after(1.4)  # 43.096
    clock.advance(0.45)
    tap(0)  # 43.547
    frame_after(2.55)  # 46.095
    after = next_0421(device)[0]

    assert timeline == [
        (0, "000000", "010000", "000000", "0000", 0),
        (1, "000000", "010000", "000000", "0000", 0),
        (1, "010000", "010000", "000000", "0000", 0),
        (0, "010000", "010000", "000000", "0000", 0),
        (0, "015600", "010000", "000000", "5600", 1),
    ]
    for name in ("work_status", "usb1_output_status", "ac_output_status"):
        assert after[name] == still[name]


def test_an_off_edge_alone_reattaches_a_port_dropped_with_the_socket_on() -> None:
    """2026-10-08: PD1 dropped 01:22:39; socket off 11:40:57, ``aa`` 01 at :58, 86 W.

    The expansion holds the bus: the 86 W shows in ``c0[24]`` 1, ``a5[1]`` 0
    (c421 11:41:00.658).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(car_charge1_output_status=1)
    clock.advance(5)
    device.negotiated_module.check_pushes()
    device.set_values(pd1_output_status=0, pd1_output_power=0, total_output_power=0)

    device.set_values(car_charge1_output_status=0)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    attached = socket_and_ports(next_0421(device)[1])
    clock.advance(3)
    device.negotiated_module.check_pushes()
    values, fields = next_0421(device)

    assert attached == (0, "010000", "010000", "000000", "0000", 0)
    assert socket_and_ports(fields) == (0, "015600", "010000", "000000", "5600", 0)
    assert values["sub_package_status"] == 1


def test_an_attached_port_holds_through_the_edge() -> None:
    """2026-10-07 21:05:16: PD1 at 77 W, the socket on, ``aa`` 014d00 throughout."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(pd1_output_status=1, pd1_output_power=77, total_output_power=77)

    device.set_values(car_charge1_output_status=1)
    clock.advance(10)
    device.negotiated_module.check_pushes()

    assert socket_and_ports(next_0421(device)[1]) == (
        1,
        "014d00",
        "010000",
        "000000",
        "4d00",
        0,
    )


def test_only_a_port_with_a_device_plugged_reattaches() -> None:
    """PD2's re-attach at 0 W (2026-08-29 18:13:37); the empty PD3 stays 0."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        pd1_device_plugged=0,
        pd2_output_status=0,
        pd2_device_plugged=1,
        pd2_device_power=0,
    )

    device.set_values(car_charge1_output_status=1)
    clock.advance(10)
    device.negotiated_module.check_pushes()

    assert socket_and_ports(next_0421(device)[1]) == (
        1,
        "000000",
        "010000",
        "000000",
        "0000",
        0,
    )


def test_the_edge_calls_own_port_power_wins() -> None:
    """Set beside the edge, a port's power stands; the re-attach still sets ``[0]``."""
    clock = ManualClock()
    device = A1783(clock=clock)

    device.set_values(car_charge1_output_status=1, pd1_output_power=40)
    clock.advance(10)
    device.negotiated_module.check_pushes()
    values = device.negotiated_module.values

    assert (values["pd1_output_status"], values["pd1_output_power"]) == (1, 40)
    assert "total_output_power" not in values


def test_a_port_detached_again_before_its_power_draws_nothing() -> None:
    """2026-09-24 05:17: ``aa`` 01 at +1 s, 0 W, back to 00 before any load."""
    clock = ManualClock()
    device = A1783(clock=clock)

    device.set_values(car_charge1_output_status=1)
    clock.advance(2)
    device.negotiated_module.check_pushes()
    device.set_values(pd1_output_status=0)
    clock.advance(5)
    device.negotiated_module.check_pushes()

    assert socket_and_ports(next_0421(device)[1])[1] == "000000"


def test_a_jump_past_the_reattach_settles_it() -> None:
    """A ``ManualClock.jump`` runs both steps' values: ``[0]`` 1 and 86 W."""
    clock = ManualClock()
    device = A1783(clock=clock)

    device.set_values(car_charge1_output_status=1)
    clock.jump(10)
    device.negotiated_module.check_pushes()
    values, fields = next_0421(device)

    assert socket_and_ports(fields)[1:] == ("015600", "010000", "000000", "5600", 0)
    assert values["sub_package_status"] == 1


@pytest.mark.parametrize("model", MODELS)
def test_the_usb_c_device_inputs_are_hidden_port_values(model: Model) -> None:
    """``<port>_device_plugged`` (0/1) and ``_device_power`` (u16 W); no field."""
    device = model()
    layout = device.layout
    assert layout is not None

    for port in ("pd1", "pd2", "pd3"):
        for name in (f"{port}_device_plugged", f"{port}_device_power"):
            assert layout.locate(name) == (0x421, 0x900)
    with pytest.raises(TypeError, match="pd2_device_plugged"):
        device.set_values(pd2_device_plugged=2)
    with pytest.raises(ConstructError):
        device.set_values(pd3_device_power=70000)


#: ``4101`` / ``4102`` ``a3 05 03 2c010000``: a 300 s output timer.
TIMER_300 = (0xA3, bytes.fromhex("032c010000"))


def test_the_output_timers_replay_the_2026_07_19_timeouts() -> None:
    """a1783-control.md "``0403`` push" ``0x55`` / ``0x56``, app_log 2026-07-19.

    Both outputs on; 300 s set on AC (12:48:01.601) and, 26 s on, on the car
    socket (12:48:27.502). ``a4[0..3]`` / ``[8..11]`` count down 2 s at a time
    (274 / 300 at 12:48:28.029); at 0 the output goes off and a ``4403`` follows.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(ac_output_status=1, car_charge1_output_status=1)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    timeline = []

    def at(seconds: float) -> None:
        clock.advance(seconds - clock.now())
        out = pushed(client, [module.check_pushes()])
        _, fields = next_0421(device)
        a4 = decode_field(fields, "a4", GreedyBytes)
        timeline.append(
            (
                int.from_bytes(a4[0:4], "little"),
                int.from_bytes(a4[8:12], "little"),
                decode_field(fields, "a7", GreedyBytes)[0],
                decode_field(fields, "b2", GreedyBytes)[0],
                out,
            )
        )

    command(device, client, 0x101, [TIMER_300])
    at(1)
    at(3)
    clock.advance(23)
    command(device, client, 0x102, [TIMER_300])
    at(27)
    at(299)
    at(301)
    at(325)
    at(327)

    assert timeline == [
        (300, 0, 1, 1, []),
        (298, 0, 1, 1, []),
        (274, 300, 1, 1, []),
        (2, 28, 1, 1, []),
        (0, 26, 0, 1, [("4403", "a10131a2020155")]),
        (0, 2, 0, 1, []),
        (0, 0, 0, 0, [("4403", "a10131a2020156")]),
    ]


def test_a_timer_set_with_its_output_off_clears() -> None:
    """a1783-control.md "Output timers": every 10 ms pass with AC off clears it."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(ac_output_status=0, ac_timer_value=10)

    clock.advance(0.01)
    module.check_pushes()
    cleared = module.values["ac_timer_value"]
    device.set_values(ac_output_status=1)
    clock.advance(20)

    assert cleared == 0
    assert pushed(client, [module.check_pushes()]) == []
    assert module.values["ac_output_status"] == 1


def test_switching_an_output_off_discards_its_timer() -> None:
    """The car socket switched off with 6 s left: the remainder is gone, no ``0403``."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(car_charge1_output_status=1, car_charger_timer_value=10)
    clock.advance(4.5)
    module.check_pushes()

    device.set_values(car_charge1_output_status=0)
    clock.advance(20)

    assert pushed(client, [module.check_pushes()]) == []
    assert module.values["car_charger_timer_value"] == 0


def test_a_zero_timer_disables_the_countdown() -> None:
    """``acTimerValue`` 0 disables (a1783-control.md ``4101 a3``): AC stays on."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(ac_output_status=1, ac_timer_value=10)
    clock.advance(3)
    module.check_pushes()

    device.set_values(ac_timer_value=0)
    clock.advance(20)

    assert pushed(client, [module.check_pushes()]) == []
    assert (module.values["ac_timer_value"], module.values["ac_output_status"]) == (
        0,
        1,
    )


def test_a_car_timeout_is_a_socket_edge() -> None:
    """The socket going off at its timeout re-attaches a detached USB-C port too."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(car_charge1_output_status=1)
    clock.advance(5)
    device.negotiated_module.check_pushes()
    device.set_values(
        pd1_output_status=0,
        pd1_output_power=0,
        total_output_power=0,
        car_charger_timer_value=2,
    )

    clock.advance(3)
    device.negotiated_module.check_pushes()

    assert socket_and_ports(next_0421(device)[1])[:2] == (0, "010000")


@pytest.mark.parametrize(
    ("model", "attached", "delay"),
    [
        pytest.param(A1783, 1, 0.49, id="A1783-expansion"),
        pytest.param(A1785, 0, 1.0, id="A1785-no-expansion"),
        pytest.param(A1763, None, 1.0, id="A1763"),
        pytest.param(A1765, None, 1.0, id="A1765"),
    ],
)
def test_the_low_soc_alert_replays_2026_10_15(
    model: Model, attached: int | None, delay: float
) -> None:
    """a1783-control.md "``0403`` push": SoC 11 -> 10 (05:00:23), ``0x52`` 0.49 s on.

    3 evaluations at or below 10 %, one per block-0 reply (500 ms), and one per
    block-1 reply too with an expansion attached: ~0.5 s with one, ~1 s without.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    soc = {"battery_level": 11}
    device.set_values(
        **soc
        if attached is None
        else {**soc, "sub_package_connection_status": attached}
    )

    device.set_values(battery_level=10)
    clock.advance(delay - 0.01)
    early = pushed(client, [module.check_pushes()])
    clock.advance(0.01)

    assert early == []
    assert pushed(client, [module.check_pushes()]) == [("4403", "a10131a2020152")]


def test_the_low_soc_alert_waits_for_the_soc_to_stay_down() -> None:
    """Assumed from "SoC <= 10 % for 3 replies": back above 10 first, no alert."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(battery_level=11)

    device.set_values(battery_level=10)
    clock.advance(0.2)
    device.set_values(battery_level=11)
    clock.advance(1)

    assert pushed(client, [module.check_pushes()]) == []


def test_the_low_soc_alert_rearms_only_above_10() -> None:
    """One ``0x52`` per crossing: 10 -> 9 sends none, 9 -> 11 -> 10 sends again."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(battery_level=11)
    sent = []

    for level in (10, 9, 11, 10):
        device.set_values(battery_level=level)
        clock.advance(1)
        sent.append(pushed(client, [module.check_pushes()]))

    alert = [("4403", "a10131a2020152")]
    assert sent == [alert, [], [], alert]


def test_the_empty_soc_alert_follows_the_firmware() -> None:
    """``0x50`` at SoC <= 1 %, ``soc_floor_event_eval(1, ...)``: firmware only."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(battery_level=2)
    clock.advance(1)
    module.check_pushes()  # the 10 % floor, crossed on the way down

    device.set_values(battery_level=1)
    clock.advance(1)

    assert pushed(client, [module.check_pushes()]) == [("4403", "a10131a2020150")]


def test_display_on_lasts_its_timeout_replays_2026_08_27() -> None:
    """app_log 2026-08-27: ``4103 a2`` 1 at 18:24:04.961, ``a4[21]`` 0 at 18:29:06.

    a1783-control.md "``4103``": ``a4[21]`` drops exactly ``display_timeout``
    (``a4[15..16]``, 300 s there) after a wake.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(screen_off_time=300)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module

    replies = command(device, client, 0x103, [(0xA2, b"\x01\x01")])
    shown = decode_field(Parameters.parse(replies[-1].plaintext), "a4", GreedyBytes)
    clock.advance(299.5)
    module.check_pushes()
    still = module.values["screen_switch"]
    clock.advance(1)
    module.check_pushes()

    assert (shown[21], still, module.values["screen_switch"]) == (1, 1, 0)


@pytest.mark.parametrize(
    ("msgtype", "field"),
    [
        pytest.param(0x103, (0xA3, b"\x01\x02"), id="4103-a3-brightness"),
        pytest.param(0x103, (0xA9, b"\x01\x01"), id="4103-a9-language"),
        pytest.param(0x102, (0xA2, b"\x01\x01"), id="4102-a2-car-socket"),
    ],
)
def test_a_waking_setter_turns_the_screen_on(
    msgtype: int, field: tuple[int, bytes]
) -> None:
    """The app_log setters sent with the screen off whose next frame read it on.

    ``4103 a3`` (4 of 4, e.g. 2026-07-02 15:11:59), ``a9`` (2026-09-18 06:36:25),
    ``4102 a2`` (2026-09-01 02:26:41, 4 of 4); the reply's ``4421`` shows it.
    """
    device = A1783(clock=ManualClock())
    client = authorized_client(device, TOKEN)

    replies = command(device, client, msgtype, [field])

    a4 = decode_field(Parameters.parse(replies[-1].plaintext), "a4", GreedyBytes)
    assert a4[21] == 1


@pytest.mark.parametrize(
    ("msgtype", "field"),
    [
        pytest.param(0x101, (0xA4, b"\x02\xb0\x04"), id="4101-a4-ac-input-limit"),
        pytest.param(0x101, (0xA6, b"\x01\x01"), id="4101-a6-ac-energy-saving"),
        pytest.param(0x102, (0xA4, b"\x01\x01"), id="4102-a4-dc-energy-saving"),
        pytest.param(0x103, (0xA4, b"\x02\x3c\x00"), id="4103-a4-display-timeout"),
        pytest.param(0x103, (0xA8, b"\x01\x01"), id="4103-a8-port-memory"),
        pytest.param(0x103, (0xB0, b"\x01\x00"), id="4103-b0-outage-alert"),
    ],
)
def test_other_setters_leave_the_screen_off(
    msgtype: int, field: tuple[int, bytes]
) -> None:
    """The same survey: each sent with the screen off left it off (1-3 times each)."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)

    command(device, client, msgtype, [field])
    clock.advance(5)
    device.negotiated_module.check_pushes()

    assert next_0421(device)[0]["screen_switch"] == 0


def test_a_display_timeout_of_0_keeps_the_screen_on() -> None:
    """``a4[15..16]`` 0 = never (a1783-control.md ``4103 a4``)."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(screen_off_time=0)

    device.set_values(screen_switch=1)
    clock.advance(86400)
    device.negotiated_module.check_pushes()

    assert device.negotiated_module.values["screen_switch"] == 1


def test_a_wake_while_on_rearms_the_timeout() -> None:
    """``screen_wake_apply`` re-arms the timeout (a1783-operation.md 05:46:21)."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(screen_off_time=300)
    module = device.negotiated_module

    device.set_values(screen_switch=1)
    clock.advance(200)
    module.check_pushes()
    device.set_values(device_language=1)
    clock.advance(250)
    module.check_pushes()
    rearmed = module.values["screen_switch"]
    clock.advance(100)
    module.check_pushes()

    assert (rearmed, module.values["screen_switch"]) == (1, 0)


def test_the_mains_auto_off_wakes_the_screen_for_its_timeout() -> None:
    """Q13's auto-off (06:02:35) wakes the screen through the same wake."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{**KASA_UNIT, "outage_alert_switch": False, "screen_off_time": 10}
    )

    device.set_values(ac_input_status=0)
    clock.advance(0.1)
    device.negotiated_module.check_pushes()
    woken = device.negotiated_module.values["screen_switch"]
    clock.advance(10)
    device.negotiated_module.check_pushes()

    assert (woken, device.negotiated_module.values["screen_switch"]) == (1, 0)


@pytest.mark.parametrize("model", MODELS)
def test_the_0490_posts_every_540_s_from_the_start(model: Model) -> None:
    """a1783-telemetry.md "When the display board posts c490": every 540 s.

    The posts sit on one 540 s grid from the display board's start (2026-10-07
    20:07:07 · 20:16:07 · 20:25:07 · 20:34:08).
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    posts = []

    for step in (539, 1, 539, 1):
        clock.advance(step)
        out = pushed(client, [module.check_pushes()])
        posts.append([int(cmd, 16) & 0xFFF for cmd, _ in out])

    assert posts == [[], [0x490], [], [0x490]]


def test_a_0490_stop_misses_posts_and_keeps_the_grid() -> None:
    """2026-10-07: the last post 20:34:08, the next 22:40:07, 14 x 540 s on.

    The switch off misses the posts; on again, the next one is on the grid,
    not 540 s after the switch.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(device_point_switch=0)

    clock.advance(1200)
    missed = pushed(client, [module.check_pushes()])
    device.set_values(device_point_switch=1)
    clock.advance(419)
    early = pushed(client, [module.check_pushes()])
    clock.advance(1)

    assert (missed, early) == ([], [])
    assert len(pushed(client, [module.check_pushes()])) == 1


def test_a_0490_due_in_a_link_gap_goes_out_on_the_next_accumulation() -> None:
    """firmware/mainmcu.md: ``c490_queue`` fails with no link, then retries every 9 s.

    The 540 s post falls due before any link; a link authorized at 1000 s gets it
    on the 1008 s accumulation, and the next post is back on the grid at 1080 s.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    clock.advance(1000)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    posts = []

    for step in (7, 1, 71, 1):
        clock.advance(step)
        posts.append(len(pushed(client, [module.check_pushes()])))

    assert posts == [0, 1, 0, 1]


def test_a_link_gap_with_no_0490_due_adds_no_post() -> None:
    """Down from 100 s to 500 s, no boundary between: the next post is the 540 s one."""
    clock = ManualClock()
    device = A1783(clock=clock)
    authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock.advance(100)
    module.check_pushes()
    module.disconnect()
    clock.advance(400)
    client = authorized_client(device, TOKEN)

    clock.advance(39)
    early = pushed(client, [module.check_pushes()])
    clock.advance(1)

    assert early == []
    assert len(pushed(client, [module.check_pushes()])) == 1


def test_a_0490_due_on_the_cloud_is_queued_and_not_retried() -> None:
    """Bound and on WiFi the queue succeeds (the cloud upload, not modeled).

    Back on BLE at 1000 s, the next post is the 1080 s one.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_cloud(True)
    clock.advance(1000)
    device.negotiated_module.check_pushes()
    device.set_cloud(False)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module

    clock.advance(79)
    early = pushed(client, [module.check_pushes()])
    clock.advance(1)

    assert early == []
    assert len(pushed(client, [module.check_pushes()])) == 1


def test_a_0490_retry_waits_out_the_switch() -> None:
    """The switch off keeps a missed post pending; on again, the next 9 s sends it."""
    clock = ManualClock()
    device = A1783(clock=clock)
    clock.advance(600)
    device.set_values(device_point_switch=0)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock.advance(30)
    held = pushed(client, [module.check_pushes()])
    device.set_values(device_point_switch=1)

    clock.advance(9)

    assert held == []
    assert len(pushed(client, [module.check_pushes()])) == 1


#: ``4090``: Time-of-Use with one Off-Peak record over the whole day.
OFF_PEAK_ALL_DAY = [*TOU_HEAD, (0xA6, b"\x01\x01"), (0xA7, bytes.fromhex("04030018"))]


def test_each_10_s_pass_counts_the_active_period() -> None:
    """a1783-telemetry.md "0503" Trigger: +10 s a pass while ``d9[0]`` != 0."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, OFF_PEAK_ALL_DAY)

    clock.advance(30)
    device.negotiated_module.check_pushes()
    values = device.negotiated_module.values

    assert (values["off_peak_time_delta"], values["tou_usage_passes"]) == (30, 3)


def test_no_pass_counts_off_the_cloud() -> None:
    """Off the cloud (BLE only, condition 2) no period is active, so none counts."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, OFF_PEAK_ALL_DAY)
    device.set_cloud(False)

    clock.advance(30)
    device.negotiated_module.check_pushes()

    assert "tou_usage_passes" not in device.negotiated_module.values


def test_every_120_passes_fold_into_the_0503_totals() -> None:
    """The fold adds the deltas to the lifetime totals ``mqtt_frame(0x503)`` shows."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, OFF_PEAK_ALL_DAY)
    recorded = int(device.negotiated_module.mcu.state({})["off_peak_time"])

    layout = device.layout
    assert layout is not None
    published = []
    for step in (1199, 1):
        clock.advance(step)
        device.negotiated_module.check_pushes()
        frame = decode(device.mqtt_frame(0x503))
        published.append(layout.read(0x503, frame.payload)["off_peak_time"])

    assert published == [recorded, recorded + 1200]
    assert device.negotiated_module.values["off_peak_time_delta"] == 0


def test_the_pass_count_restarts_after_pass_720() -> None:
    """2 h of TOU-active time: pass 720 flushes, and the count starts again."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, OFF_PEAK_ALL_DAY)
    recorded = int(device.negotiated_module.mcu.state({})["off_peak_time"])

    clock.advance(7210)
    device.negotiated_module.check_pushes()
    values = device.negotiated_module.values

    assert (values["tou_usage_passes"], values["off_peak_time"]) == (1, recorded + 7200)


def test_the_2_h_flush_publishes_the_0503() -> None:
    """a1783-telemetry.md "0503" Trigger: pass 720 publishes it at ``a1 12``.

    The totals folded and Time Save (the RTC to the minute) are the payload
    ``mqtt_frame(0x503)`` builds; nothing goes out before the 720th pass.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, OFF_PEAK_ALL_DAY)

    clock.advance(7199)
    device.negotiated_module.check_pushes()
    early = list(device.mqtt_published)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    published = list(device.mqtt_published)
    built = device.mqtt_frame(0x503)
    rtc = device.negotiated_module.rtc

    assert early == []
    assert published == [(0x503, built)]
    totals = Parameters.parse(decode(built).payload)
    assert decode_field(totals, "a1", GreedyBytes).hex() == "12"
    assert decode_field(totals, "a3", ui(4)) == rtc - rtc % 60


def test_the_0503_flush_off_the_cloud_publishes_nothing() -> None:
    """Off the cloud no TOU period is active, so no pass counts and none flushes."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT)
    tou_on_the_cloud(device, TOKEN, CLOCK_15_44_00, OFF_PEAK_ALL_DAY)
    device.set_cloud(False)

    clock.advance(7300)
    device.negotiated_module.check_pushes()

    assert device.mqtt_published == []


#: UTC epochs of the 2026-10-06 (EDT) backup-plan witnesses (a1783-control.md
#: "``d9`` tail", "``0425`` events").
EPOCH_19_53_36 = 1791330816
EPOCH_20_59_54 = 1791334794
EPOCH_22_59_58 = 1791341998
NO_END = 0xFFFFFFFF
#: The app's "from now, no end" plan (start 2026-09-19 22:46:00 UTC), its 21-22
#: EDT plan, and the Storm Guard window 23:00-09:00 EDT.
PLAN_FROM_0919 = (1789857960, NO_END)
PLAN_21_22 = (1791334800, 1791338400)
STORM_23_09 = (1791342000, 1791378000)
#: The firmware's never record, which a cancel writes over a plan.
NEVER = (NO_END, NO_END)
WINDOW_END_0B = ("4425", "a10131a202010ba3050400000000a4050400000000")


def test_the_fast_charging_plan_on_and_off_replays_2026_10_06() -> None:
    """19:53:37: ``405e`` on, ``0x0a`` on the next 100 ms pass; 19:54:14 off, ``0x0b``.

    The ``0425`` comes from the firmware's pass, never the ``405e`` reply.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_19_53_36.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)

    replies = command(device, client, 0x05E, backup_plan(0, 1, (PLAN_FROM_0919,)))
    clock.advance(0.1)
    started = pushed(client, [module.check_pushes()])
    on = backup_tail(next_0421(device)[0])
    command(device, client, 0x05E, backup_plan(0, 0))
    clock.advance(0.1)
    ended = pushed(client, [module.check_pushes()])

    assert [cmd_hex(reply.frame) for reply in replies] == ["485e", "4421"]
    assert started == [("4425", "a10131a202010aa30504a810af6aa40504ffffffff")]
    assert on == (1, 1, 0, *PLAN_FROM_0919, *PLAN_FROM_0919)
    assert ended == [WINDOW_END_0B]
    assert backup_tail(next_0421(device)[0]) == (0, 0, 0, *PLAN_FROM_0919, 0, 0)


def test_a_plan_window_starts_and_ends_on_its_own_replays_2026_10_06() -> None:
    """21:00:00 ``0x0a`` with the 21-22 epochs, the bus to main; 22:00 ``0x0b``.

    a1783-control.md 21:00:04: ``c0[24]`` 2 -> 0 and ``a3[0]`` 0 -> 2, the main
    pack (96 %) taking the charge from the expansion.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOU_UNIT, battery_level=96, sub_package_battery_level=22)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_20_59_54.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    command(device, client, 0x05E, backup_plan(0, 1, (PLAN_21_22,)))

    clock.advance(5.9)
    early = pushed(client, [module.check_pushes()])
    clock.advance(0.2)
    started = pushed(client, [module.check_pushes()])
    clock.advance(4)  # the 21:00:04 frame, a3[0] past its lag
    module.check_pushes()
    values = next_0421(device)[0]
    clock.advance(3596)
    ended = [p for p in pushed(client, [module.check_pushes()]) if p[0] == "4425"]

    assert early == []
    assert started == [("4425", "a10131a202010aa305049099c56aa40504a0a7c56a")]
    assert backup_tail(values) == (1, 1, 0, *PLAN_21_22, *PLAN_21_22)
    assert (
        values["sub_package_status"],
        values["work_status"],
        values["charge_discharge_status"],
    ) == (0, 2, 2)
    assert ended == [WINDOW_END_0B]


def test_a_storm_guard_window_start_replays_2026_10_06_23_00() -> None:
    """BLE 23:00:01: ``4425 0x07`` with the 03:00-13:00 UTC epochs, type 2."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_22_59_58.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    command(device, client, 0x05E, backup_plan(0, 1, (PLAN_21_22,)))
    command(device, client, 0x05E, backup_plan(1, 1, (STORM_23_09,)))

    clock.advance(2.1)
    started = pushed(client, [module.check_pushes()])

    assert started == [("4425", "a10131a2020107a30504b0b5c56aa405045042c66a")]
    assert backup_tail(next_0421(device)[0]) == (2, 1, 1, *PLAN_21_22, *STORM_23_09)


def test_with_none_running_storm_guard_is_chosen_first() -> None:
    """a1783-control.md ``d9`` type row: auto (table 2) before manual (table 1)."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_22_59_58.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    command(device, client, 0x05E, backup_plan(1, 1, ((STORM_23_09[0], NO_END),)))
    command(device, client, 0x05E, backup_plan(0, 1, ((STORM_23_09[0], NO_END),)))

    clock.advance(2.1)
    started = pushed(client, [module.check_pushes()])

    assert [event[1][12:14] for event in started] == ["07"]


def test_a_running_window_ends_into_the_other_with_no_0425() -> None:
    """The running plan keeps going; at its end the type flips to Storm Guard."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_22_59_58.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    manual = (EPOCH_22_59_58 - 60, STORM_23_09[0] + 60)
    command(device, client, 0x05E, backup_plan(0, 1, (manual,)))
    clock.advance(0.1)
    started = pushed(client, [module.check_pushes()])
    command(device, client, 0x05E, backup_plan(1, 1, (STORM_23_09,)))

    clock.advance(2.1)
    kept = (pushed(client, [module.check_pushes()]), next_0421(device)[0])
    clock.advance(60)
    flipped = (pushed(client, [module.check_pushes()]), next_0421(device)[0])

    assert [event[1][12:14] for event in started] == ["0a"]
    assert kept[0] == []
    assert backup_tail(kept[1])[0] == 1
    assert flipped[0] == []
    assert backup_tail(flipped[1]) == (2, 1, 1, *manual, *STORM_23_09)


def test_clear_all_zeroes_both_tables_and_switches() -> None:
    """``a4 02``: tables and switches zeroed (``disaster_plan_clear_all``); ``0x0b``."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_19_53_36.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    command(device, client, 0x05E, backup_plan(0, 1, (PLAN_FROM_0919,)))
    command(device, client, 0x05E, backup_plan(1, 1, (STORM_23_09,)))
    clock.advance(0.1)
    module.check_pushes()

    command(device, client, 0x05E, backup_plan(2))
    clock.advance(0.1)
    ended = pushed(client, [module.check_pushes()])

    assert ended == [WINDOW_END_0B]
    assert backup_tail(next_0421(device)[0]) == (0, 0, 0, 0, 0, 0, 0)
    assert module.values["auto_backup_plans"] == bytes(24)


def test_a_cancel_ends_the_running_plan_and_leaves_its_switch() -> None:
    """``aa 01``: the plan becomes the never record, ``0x0b`` on the next pass."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_19_53_36.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    command(device, client, 0x05E, backup_plan(0, 1, (PLAN_FROM_0919,)))
    clock.advance(0.1)
    module.check_pushes()

    command(device, client, 0x05E, backup_plan(0, cancel=True))
    clock.advance(0.1)
    ended = pushed(client, [module.check_pushes()])

    assert ended == [WINDOW_END_0B]
    assert backup_tail(next_0421(device)[0]) == (0, 1, 0, *NEVER, 0, 0)


def test_cancelling_storm_guard_turns_a_covering_plan_off() -> None:
    """A Storm Guard cancel turns the manual switch off when table 1 covers now."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_22_59_58.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    command(device, client, 0x05E, backup_plan(1, 1, (STORM_23_09,)))
    clock.advance(2.1)
    module.check_pushes()
    command(device, client, 0x05E, backup_plan(0, 1, (PLAN_FROM_0919,)))
    clock.advance(0.1)
    module.check_pushes()

    command(device, client, 0x05E, backup_plan(1, cancel=True))
    clock.advance(0.1)
    ended = pushed(client, [module.check_pushes()])

    assert [event[1][12:14] for event in ended] == ["08"]
    assert backup_tail(next_0421(device)[0])[:3] == (0, 0, 1)
    plans = module.values["auto_backup_plans"]
    assert isinstance(plans, bytes)
    assert plans[:8] == bytes.fromhex("ffffffffffffffff")


def test_the_plan_switched_off_also_ends_a_running_storm_guard() -> None:
    """``a4 00 · a5 00`` wipes every Storm Guard entry covering now: ``0x08``."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_22_59_58.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    command(device, client, 0x05E, backup_plan(1, 1, (STORM_23_09,)))
    clock.advance(2.1)
    module.check_pushes()

    command(device, client, 0x05E, backup_plan(0, 0))
    clock.advance(0.1)
    ended = pushed(client, [module.check_pushes()])

    assert [event[1][12:14] for event in ended] == ["08"]
    assert backup_tail(next_0421(device)[0])[:3] == (0, 0, 1)


def test_a_cancel_with_no_window_running_changes_nothing() -> None:
    """``aa 01`` acts only while a window is active (0x0800dc30-0x0800dc7a)."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_19_53_36.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    before = backup_tail(next_0421(device)[0])

    command(device, client, 0x05E, backup_plan(0, cancel=True))
    clock.advance(0.1)

    assert pushed(client, [module.check_pushes()]) == []
    assert backup_tail(next_0421(device)[0]) == before


def test_no_window_runs_before_the_rtc_reads_2025() -> None:
    """``rtc_year_valid?``: a plan covering 2024-06-01 never starts."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, (1717200000).to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)

    command(device, client, 0x05E, backup_plan(0, 1, ((1704067200, NO_END),)))
    clock.advance(1)

    assert pushed(client, [module.check_pushes()]) == []
    assert backup_tail(next_0421(device)[0])[0] == 0


def test_a_clock_set_into_a_plan_starts_it() -> None:
    """The windows follow the clock: set to 21:00:00 EDT, the 21-22 plan starts."""
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_19_53_36.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)
    command(device, client, 0x05E, backup_plan(0, 1, (PLAN_21_22,)))
    clock.advance(0.1)
    idle = pushed(client, [module.check_pushes()])

    reset = [(0xA1, PLAN_21_22[0].to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, reset)
    clock.advance(0.1)

    assert idle == []
    assert [e[1][12:14] for e in pushed(client, [module.check_pushes()])] == ["0a"]


def test_the_c1000_gen_2_runs_the_window_with_no_0425() -> None:
    """The A1763 records no ``0425``: its ``d9`` tail moves, nothing is pushed."""
    clock = ManualClock()
    device = A1763(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_19_53_36.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)

    command(device, client, 0x05E, backup_plan(0, 1, (PLAN_FROM_0919,)))
    clock.advance(0.1)

    assert pushed(client, [module.check_pushes()]) == []
    assert backup_tail(next_0421(device)[0])[0] == 1


def test_a_window_start_leaves_a_full_main_pack_charging_the_expansion() -> None:
    """The bus moves to the main only below 100 %; a full main leaves ``c0[24]`` 2."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        ac_input_disable_switch=0,
        ac_input_status=1,
        ac_input_power=1210,
        battery_level=100,
    )
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock_set = [(0xA1, EPOCH_19_53_36.to_bytes(4, "little")), (0xA3, EDT)]
    exchange(module, client, 0x022, clock_set)

    command(device, client, 0x05E, backup_plan(0, 1, (PLAN_FROM_0919,)))
    clock.advance(0.1)
    module.check_pushes()
    window = backup_tail(next_0421(device)[0])[0]

    assert (window, module.values["sub_package_status"]) == (1, 2)


@pytest.mark.parametrize("plans", [bytes(23), b"\x00" * 25, 5])
def test_the_storm_guard_table_is_three_epoch_pairs(plans: Value) -> None:
    """Hidden ``auto_backup_plans``: exactly 3 x (u32 start, u32 end), as bytes."""
    device = A1783()

    with pytest.raises(TypeError, match="auto_backup_plans"):
        device.set_values(auto_backup_plans=plans)


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param([(0xA3, b"\x01\x02"), (0xA4, b"\x01\x00")], id="a3-not-3"),
        pytest.param([(0xA4, b"\x01\x00"), (0xA5, b"\x01\x01")], id="no-a3"),
    ],
)
def test_a_405e_without_its_group_selector_changes_nothing(
    fields: list[tuple[int, bytes]],
) -> None:
    """``a3`` must be 3 (a1783-control.md ``405e``); acked, nothing applied."""
    device = A1783(clock=ManualClock())
    client = authorized_client(device, TOKEN)
    before = backup_tail(next_0421(device)[0])

    replies = command(device, client, 0x05E, fields)

    assert replies[0].plaintext[:1] == b"\x00"
    assert backup_tail(next_0421(device)[0]) == before


#: The port powers that make each port flow, from idle or output on.
FLOW_POWERS: dict[int, dict[str, Value]] = {
    2: {"pv1_input_power": 100},
    1: {"total_output_power": 60},
    0: {"total_output_power": 0},
}


@pytest.mark.parametrize(
    ("flow", "work"),
    [
        pytest.param(2, 2, id="entering-charge"),
        pytest.param(1, 1, id="discharging"),
        pytest.param(0, 0, id="idle"),
    ],
)
def test_a3_follows_a5_after_its_20_samples(flow: int, work: int) -> None:
    """a1783-telemetry.md "``a5[1]`` vs ``a3[0]``": ``a3[0]`` lags 2 s (20 x 100 ms).

    Wire: 2.0 s charge (2026-08-29), 3.0 / 2.4 s discharge, upper bounds. The
    main on the bus below its cap; PV in over the output charges it, output
    alone discharges it, and ``a3[0]`` follows its current.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    start = 1 if flow == 0 else 0
    device.set_values(
        ac_input_disable_switch=0,
        pack_on_bus=1,
        pack_soc=800,
        discharge_latch_from=800,
        total_output_power=60 * start,
    )
    clock.advance(5)
    device.negotiated_module.check_pushes()

    device.set_values(**FLOW_POWERS[flow])
    moved = next_0421(device)[0]
    clock.advance(1.9)
    device.negotiated_module.check_pushes()
    lagging = next_0421(device)[0]["work_status"]
    clock.advance(0.1)
    device.negotiated_module.check_pushes()

    assert (moved["charge_discharge_status"], moved["work_status"]) == (flow, start)
    assert lagging == start
    assert next_0421(device)[0]["work_status"] == work


def test_a_call_setting_a3_itself_keeps_it() -> None:
    """``a3[0]`` set beside ``a5[1]`` stands; no follow overwrites it."""
    clock = ManualClock()
    device = A1783(clock=clock)

    device.set_values(charge_discharge_status=2, work_status=1)
    clock.advance(3)
    device.negotiated_module.check_pushes()

    assert next_0421(device)[0]["work_status"] == 1


def test_the_ac_charge_start_moves_a5_first_replays_2026_10_26() -> None:
    """BLE 15:29:01 ``a5[1]`` 2 with the first AC sample, ``a3[0]`` 2 at 15:29:02."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{**KASA_UNIT, "ac_input_disable_switch": 0, "ac_input_status": 0}
    )
    device.set_values(ac_input_status=1)
    clock.advance(11)
    device.negotiated_module.check_pushes()
    started = next_0421(device)[0]
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert (started["charge_discharge_status"], started["work_status"]) == (2, 0)
    assert next_0421(device)[0]["work_status"] == 2


def test_battery_only_on_leaves_a3_charging_for_its_lag() -> None:
    """MQTT 2026-10-06 19:39:18: ``a5[1]`` 2 -> 0, AC 0 W; ``a3[0]`` 2 to 19:39:22."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{
            **KASA_UNIT,
            "ac_input_disable_switch": 0,
            "ac_input_status": 1,
            "ac_input_power": 1210,
            "battery_level": 64,
            "total_output_power": 0,
        }
    )
    clock.advance(2)  # the main's charge past its 20 samples: a3[0] 2
    device.negotiated_module.check_pushes()

    device.set_values(ac_input_disable_switch=1)
    switched = next_0421(device)[0]
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert (
        switched["ac_input_status"],
        switched["ac_input_power"],
        switched["charge_discharge_status"],
        switched["work_status"],
    ) == (2, 0, 0, 2)
    assert next_0421(device)[0]["work_status"] == 0


def test_battery_only_on_leaves_a_dc_fed_charge_charging() -> None:
    """With the DC input charging, Battery-Only stops only AC: ``a5[1]`` stays 2."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{
            **KASA_UNIT,
            "ac_input_disable_switch": 0,
            "ac_input_status": 1,
            "pv1_input_power": 80,
        }
    )

    device.set_values(ac_input_disable_switch=1)
    clock.advance(2)
    device.negotiated_module.check_pushes()
    values = next_0421(device)[0]

    assert (values["charge_discharge_status"], values["work_status"]) == (2, 2)


#: ``0490`` ``.15.5`` / ``.15.6``: the pack holding the bus reads (8, 9), the
#: other (3, 3) (a1783-operation.md "Observed handovers").
MAIN_ON_BUS = {
    "stage_a_state": 8,
    "stage_b_state": 9,
    "sub_package_stage_a_state": 3,
    "sub_package_stage_b_state": 3,
}
SLOTS = tuple(MAIN_ON_BUS)
#: The 2026-10-06 bench unit charging its main at 1210 W, the expansion at 5 %.
CHARGING_MAIN = {
    **KASA_UNIT,
    "ac_input_disable_switch": 0,
    "ac_input_status": 1,
    "ac_input_power": 1210,
    "battery_level": 94,
    "charging_limit_value": 95,
    "pack_soc": 940,
    "sub_package_connection_status": 1,
    "sub_package_battery_level": 5,
    "sub_package_pack_soc": 50,
    "pack_on_bus": 1,
    "discharge_latch_from": 940,
    **MAIN_ON_BUS,
}


def test_the_main_at_its_cap_hands_the_charge_over_replays_2026_10_06() -> None:
    """MQTT 20:20:00 main 95 %, 20:20:03 ``a3[0]`` / ``a5[1]`` 0, ``c0[24]`` 2.

    The charge-cap latch sets after 100 steps (1 s) charging at the cap
    (mainbms-control-fsms.md "Pack selection" :165, :193); ``a3[0]`` reads 0
    at once, the expansion holding the bus; the slots swap (2026-08-27).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**CHARGING_MAIN)

    device.set_values(battery_level=95, pack_soc=950)
    latching = next_0421(device)[0]["charge_discharge_status"]
    clock.advance(1)
    device.negotiated_module.check_pushes()
    values = next_0421(device)[0]
    posted = next_0490(device)

    assert latching == 2
    assert (
        values["work_status"],
        values["charge_discharge_status"],
        values["sub_package_status"],
    ) == (0, 0, 2)
    assert tuple(posted[name] for name in SLOTS) == (3, 3, 8, 9)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"sub_package_connection_status": 0}, id="no-expansion"),
        pytest.param(
            {"sub_package_battery_level": 95, "sub_package_pack_soc": 950},
            id="expansion-at-its-cap",
        ),
        pytest.param(
            {"ac_input_status": 0, "ac_input_power": 0, "total_output_power": 60},
            id="main-not-charging",
        ),
    ],
)
def test_no_cap_handover_without_an_expansion_to_charge(
    values: dict[str, Value],
) -> None:
    """The charge moves only to an attached expansion below its cap."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, **values})

    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


#: 2026-09-03 03:29: the expansion on the bus (the recording's) feeding the
#: ES720 at 92 W, ``min_soc`` 5, ``max_soc`` 98, the main at 96 %, the
#: expansion's 0.1 % SoC at 6.0 % (a1783-operation.md "Expansion to its floor").
EXPANSION_DISCHARGING = {
    **KASA_UNIT,
    "charging_limit_value": 98,
    "battery_level": 96,
    "pack_soc": 960,
    "discharge_limit_value": 5,
    "total_output_power": 92,
    "sub_package_connection_status": 1,
    "sub_package_battery_level": 6,
    "sub_package_pack_soc": 60,
    "pack_on_bus": 2,
    "discharge_latch_from": 960,
}


def test_the_expansion_under_its_line_hands_back_replays_2026_09_03() -> None:
    """03:29:25 ``a5[1]`` 1 and ``c0[24]`` 0; ``a3[0]`` follows (03:29:28).

    Once its 0.1 % SoC is under ``min_soc`` + 0.9 % (pack selection :170), at
    once, the 3 s dwell long over; the next ``0490`` has the main on the bus.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**EXPANSION_DISCHARGING)

    device.set_values(sub_package_pack_soc=58)
    handed = next_0421(device)[0]
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert (
        handed["charge_discharge_status"],
        handed["sub_package_status"],
        handed["work_status"],
    ) == (1, 0, 0)
    assert next_0421(device)[0]["work_status"] == 1
    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_the_expansion_keeps_the_bus_off_its_line() -> None:
    """``c0[25]`` isn't the compared value: its whole percent at 5 moves nothing."""
    device = A1783(clock=ManualClock())
    device.set_values(**{**EXPANSION_DISCHARGING, "sub_package_pack_soc": 59})

    device.set_values(sub_package_battery_level=5, sub_package_pack_soc=59)

    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_a_charging_expansion_under_its_line_hands_back_the_cap_latch_clear() -> None:
    """:170 tests the charge-cap latch, not the flow: under its cap, it's clear.

    The DC input over the output charges the expansion on the bus.
    """
    device = A1783(clock=ManualClock())
    device.set_values(**{**EXPANSION_DISCHARGING, "sub_package_pack_soc": 59})

    device.set_values(sub_package_pack_soc=58, pv1_input_power=100)

    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


#: 2026-08-31 19:23: main 100 % (``max_soc`` 100), expansion 56 % on the bus, idle.
FLOOR_LEVER = {
    **KASA_UNIT,
    "charging_limit_value": 100,
    "discharge_limit_value": 5,
    "battery_level": 100,
    "pack_soc": 1000,
    "total_output_power": 0,
    "sub_package_connection_status": 1,
    "sub_package_battery_level": 56,
    "sub_package_pack_soc": 560,
    "pack_on_bus": 2,
    "discharge_latch_from": 1000,
}


def test_raising_min_soc_moves_the_bus_to_the_main_replays_2026_08_31() -> None:
    """a1783-operation.md "``min_soc`` as a lever": floor 60 over the expansion's 56 %.

    The expansion under its new line hands the bus to the main (:170; :172's
    zeroed allowance would too). Post n+1 (19:32:07): main 3/9 (on the bus,
    quiet), expansion 3/3.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**FLOOR_LEVER)

    device.set_values(discharge_limit_value=60)
    clock.advance(3.1)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 9, 3, 3)


def test_lowering_min_soc_waits_for_a_1_percent_drop() -> None:
    """Reverting keeps the main until its SoC drops >= 1.0 % (the discharge latch).

    Then the expansion, above its line, takes discharge back: ``c0[24]`` the
    port flow, ``a5[1]`` 0 at once (pack selection :164, :194).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**FLOOR_LEVER)
    device.set_values(discharge_limit_value=60)
    clock.advance(10)
    device.negotiated_module.check_pushes()

    device.set_values(discharge_limit_value=5)
    kept = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(total_output_power=60, pack_soc=991)
    still = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(pack_soc=990)
    values = next_0421(device)[0]

    assert kept == still == (3, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)
    assert (values["sub_package_status"], values["charge_discharge_status"]) == (1, 0)


def test_a_new_holder_keeps_the_bus_for_its_dwell() -> None:
    """Just taken back by the expansion, a raised floor moves the bus 3.01 s later."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**FLOOR_LEVER)
    device.set_values(discharge_limit_value=60)
    clock.advance(10)
    device.negotiated_module.check_pushes()
    device.set_values(discharge_limit_value=5, total_output_power=60)
    device.set_values(pack_soc=990)

    device.set_values(discharge_limit_value=60)
    clock.advance(3)
    device.negotiated_module.check_pushes()
    dwelling = tuple(next_0490(device)[name] for name in SLOTS)
    clock.advance(0.1)
    device.negotiated_module.check_pushes()

    assert dwelling == (3, 3, 8, 9)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_a_main_leaving_charge_within_the_latch_keeps_the_charge() -> None:
    """The latch needs 1 s charging at the cap: charging ended first, no handover."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**CHARGING_MAIN)

    device.set_values(battery_level=95, pack_soc=950)
    device.set_values(ac_input_power=0)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_the_switch_gap_finds_the_expansion_at_its_floor() -> None:
    """Latched within 2 s of the last switch, the expansion fell to its floor."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**FLOOR_LEVER)
    device.set_values(discharge_limit_value=60)
    device.set_values(discharge_limit_value=5, total_output_power=60)

    device.set_values(pack_soc=990)
    device.set_values(sub_package_pack_soc=50)
    clock.advance(3)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 9, 3, 3)


def test_an_expansion_back_above_its_line_within_its_dwell_keeps_the_bus() -> None:
    """Just handed the charge, the expansion dips under its line and recovers."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()

    device.set_values(
        ac_input_power=0,
        total_output_power=60,
        sub_package_pack_soc=58,
        pack_soc=940,
    )
    clock.advance(1)
    device.negotiated_module.check_pushes()
    device.set_values(sub_package_pack_soc=60)
    clock.advance(3)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_a_main_below_its_cap_takes_the_bus_at_8() -> None:
    """``.15.5`` reads 3 only quiet at or above the cap; inside the band, 8."""
    device = A1783(clock=ManualClock())
    device.set_values(**{**FLOOR_LEVER, "pack_soc": 800, "discharge_latch_from": 800})

    device.set_values(discharge_limit_value=60)

    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_a_main_at_its_cap_takes_the_bus_too() -> None:
    """:170's charge-allowance test on the main is taken as passing at its cap."""
    device = A1783(clock=ManualClock())
    device.set_values(**{**OFF_GRID_EXPANSION, "pack_soc": 980})

    device.set_values(sub_package_pack_soc=58)

    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_the_main_keeps_the_bus_over_an_expansion_in_its_band_replays_2026_09_03() -> (
    None
):
    """a1783-operation.md "Expansion to its floor": ``c0[24]`` 0 03:29:25-08:00.

    The main falls 96 -> 85 % while the expansion sits in its 5.0-5.9 % band:
    :164 never hands it discharge back, however far the main drops.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**EXPANSION_DISCHARGING)
    device.set_values(sub_package_pack_soc=58)
    clock.advance(4)
    device.negotiated_module.check_pushes()
    handed = tuple(next_0490(device)[name] for name in SLOTS)

    for soc in range(959, 849, -1):
        device.set_values(pack_soc=soc)
        clock.advance(6)
        device.negotiated_module.check_pushes()

    assert handed == (8, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)
    assert next_0421(device)[0]["sub_package_status"] == 0


def test_an_expansion_put_on_the_bus_under_its_line_hands_it_back() -> None:
    """:170 is checked every step, so it arms when the bus is set by name too."""
    device = A1783(clock=ManualClock())
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})

    device.set_values(pack_on_bus=2, ac_input_power=0, total_output_power=60)

    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_a_charge_retakes_the_discharge_latch_baseline() -> None:
    """The >= 1.0 % drop counts from the main's SoC when its charging stopped."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**FLOOR_LEVER, "pack_soc": 800, "discharge_latch_from": 800})
    device.set_values(discharge_limit_value=60)
    clock.advance(5)
    device.negotiated_module.check_pushes()
    device.set_values(discharge_limit_value=5, total_output_power=60)

    device.set_values(pack_soc=805, pv1_input_power=100)
    device.set_values(pv1_input_power=0)
    device.set_values(pack_soc=796)
    kept = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(pack_soc=795)

    assert kept == (8, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_a_rise_without_charging_keeps_the_discharge_latch_baseline() -> None:
    """A SoC rise with no charge doesn't re-take the baseline."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**FLOOR_LEVER, "pack_soc": 800, "discharge_latch_from": 800})
    device.set_values(discharge_limit_value=60)
    clock.advance(5)
    device.negotiated_module.check_pushes()
    device.set_values(discharge_limit_value=5, total_output_power=60)

    device.set_values(pack_soc=805)
    device.set_values(pack_soc=791)
    kept = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(pack_soc=790)

    assert kept == (8, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_a_latch_set_over_the_band_hands_over_once_the_expansion_leaves_it() -> None:
    """The latch arms on the main's drop and stays set; :164 waits for the allowance."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**FLOOR_LEVER)
    device.set_values(discharge_limit_value=60)
    clock.advance(10)
    device.negotiated_module.check_pushes()
    device.set_values(discharge_limit_value=5, total_output_power=60)
    device.set_values(sub_package_pack_soc=55)

    device.set_values(pack_soc=990)
    held = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(sub_package_pack_soc=60)

    assert held == (3, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_the_charge_cap_latch_clears_the_discharge_latch() -> None:
    """Armed over the band, then the cap handover: the latch is clear when it's back."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**FLOOR_LEVER)
    device.set_values(discharge_limit_value=60)
    clock.advance(10)
    device.negotiated_module.check_pushes()
    device.set_values(discharge_limit_value=5, total_output_power=60)
    device.set_values(sub_package_pack_soc=55)
    device.set_values(pack_soc=990)

    device.set_values(pack_soc=1000, pv1_input_power=100)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    charging = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(pv1_input_power=0)
    clock.advance(4)
    device.negotiated_module.check_pushes()
    device.set_values(sub_package_pack_soc=60)

    assert charging == (3, 3, 8, 9)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_the_expansion_path_hold_blocks_the_discharge_handover() -> None:
    """The expansion's ``raw+0x95`` set holds :164 off; cleared, the latch moves it."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**FLOOR_LEVER, "pack_soc": 800, "discharge_latch_from": 800})
    device.set_values(discharge_limit_value=60)
    clock.advance(5)
    device.negotiated_module.check_pushes()
    device.set_values(discharge_limit_value=5, total_output_power=60)
    device.set_values(sub_package_path_hold=1)

    device.set_values(pack_soc=790)
    held = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(sub_package_path_hold=0)

    assert held == (8, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_the_expansion_path_hold_blocks_the_charge_handover() -> None:
    """``raw+0x95`` set holds :165 off, not the cap latch: cleared, the charge moves.

    The latch set under the hold (:193), the move follows the clear at once.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**CHARGING_MAIN)
    device.set_values(sub_package_path_hold=1)

    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(2)
    device.negotiated_module.check_pushes()
    held = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(sub_package_path_hold=0)

    assert held == (8, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"ac_input_power": 0, "total_output_power": 60}, id="discharge"),
        pytest.param(
            {"ac_input_status": 0, "ac_input_power": 0}, id="charge-source-gone"
        ),
        pytest.param(
            {"ac_input_power": 0, "pack_soc": 940}, id="discharge-latch-armed"
        ),
    ],
)
def test_the_cap_latch_clears(values: dict[str, Value]) -> None:
    """:193 clears in discharge and with the source gone; :194 setting clears it too.

    Set under the path hold, cleared: the hold's clear then moves nothing.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})
    device.set_values(sub_package_path_hold=1)
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    device.set_values(**values)
    device.set_values(sub_package_path_hold=0)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_alternator_byte_9_sets_the_cap_latch_with_no_charge_source() -> None:
    """:193's source is ``flag_00c7`` or ALT byte +9 == 2: +9 alone moves the charge.

    The main charges (+5 A on ``.14.29``) with no port input to raise ``flag_00c7``.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{
            **CHARGING_MAIN,
            "ac_input_status": 0,
            "ac_input_power": 0,
            "pack_current": 500,
        }
    )
    device.set_values(alternator_byte_9=2)

    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_the_source_going_holds_the_cap_latch_15_s_with_alternator_byte_9() -> None:
    """``flag_00c7`` gone with +9 == 2: the latch clears after 1 500 steps, then :170.

    The expansion, quiet under its line, keeps the bus 15 s; the main then takes
    it, quiet at its 95 % cap.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})
    device.set_values(alternator_byte_9=2)
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()

    device.set_values(ac_input_status=0, ac_input_power=0)
    clock.advance(14)
    device.negotiated_module.check_pushes()
    held = tuple(next_0490(device)[name] for name in SLOTS)
    clock.advance(4.1)
    device.negotiated_module.check_pushes()

    assert held == (3, 3, 8, 9)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 9, 3, 3)


def test_alternator_byte_9_alone_holds_the_cap_latch_15_s() -> None:
    """Set with ``flag_00c7`` gone, the latch clears 1 500 steps on.

    The main charges (+5 A on ``.14.29``) with no port input; the path hold kept
    it on the bus, and cleared after the latch it moves nothing.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **{
            **CHARGING_MAIN,
            "ac_input_status": 0,
            "ac_input_power": 0,
            "pack_current": 500,
        }
    )
    device.set_values(alternator_byte_9=2, sub_package_path_hold=1)
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    latched = device.negotiated_module.values["charge_cap_latch"]
    clock.advance(16)
    device.negotiated_module.check_pushes()

    device.set_values(sub_package_path_hold=0)

    assert latched == 1
    assert device.negotiated_module.values["charge_cap_latch"] == 0
    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_the_source_back_within_15_s_keeps_the_cap_latch() -> None:
    """``flag_00c7`` back resets the clear count: the expansion keeps the bus."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})
    device.set_values(alternator_byte_9=2)
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    device.set_values(ac_input_status=0, ac_input_power=0)
    clock.advance(10)
    device.negotiated_module.check_pushes()

    device.set_values(ac_input_status=1)
    clock.advance(10)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_a_spent_cap_latch_waits_for_the_charge_source() -> None:
    """The clear count outlives the clear: +9 alone re-sets nothing.

    The main charging again (+5 A on ``.14.29``) with no source sets nothing; with
    ``flag_00c7`` back it sets at once, its 1 s count run out before.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})
    device.set_values(alternator_byte_9=2)
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    device.set_values(ac_input_status=0, ac_input_power=0)
    clock.advance(19)
    device.negotiated_module.check_pushes()

    device.set_values(pack_current=500)
    clock.advance(2)
    device.negotiated_module.check_pushes()
    spent = tuple(next_0490(device)[name] for name in SLOTS)
    device.set_values(ac_input_status=1, ac_input_power=1210)

    assert spent == (3, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_alternator_byte_9_is_a_byte() -> None:
    """Hidden ``alternator_byte_9``: the ALT record's +9, one byte."""
    device = A1783()

    with pytest.raises(ConstructError):
        device.set_values(alternator_byte_9=256)


def test_the_1_s_charging_count_runs_once() -> None:
    """``0x20000b78`` is never reset: after its first second the latch sets at once.

    fsms "Charge-cap latch counters". Back on the main at its cap, a second
    charge hands over without the 1 s.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    device.set_values(ac_input_status=0, ac_input_power=0)
    clock.advance(4)
    device.negotiated_module.check_pushes()
    back = tuple(next_0490(device)[name] for name in SLOTS)

    device.set_values(ac_input_status=1, ac_input_power=1210)

    assert back == (3, 9, 3, 3)
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_the_cap_latch_keeps_an_idle_expansion_under_its_line_on_the_bus() -> None:
    """Handed the charge at 5.0 %, the expansion goes quiet with the source still in.

    The charge-cap latch stays set (not discharging, a source), so :170 waits.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()

    device.set_values(ac_input_power=0)
    clock.advance(4)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_the_charge_source_going_hands_an_expansion_under_its_line_back() -> None:
    """The source gone clears the cap latch at once; :170 then moves the bus.

    The main, quiet at its 95 % cap, takes it at (3, 9).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "discharge_limit_value": 5})
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    device.set_values(ac_input_power=0)

    device.set_values(ac_input_status=0)
    clock.advance(4)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 9, 3, 3)


def test_the_expansion_path_hold_is_0_or_1() -> None:
    """Hidden ``sub_package_path_hold``: the expansion's ``raw+0x95``, set or clear."""
    device = A1783()

    with pytest.raises(TypeError, match="sub_package_path_hold"):
        device.set_values(sub_package_path_hold=2)


#: ``4104`` ``a4``: the expansion's switch, off (0) or on (1).
EXPANSION_OFF = [(0xA4, b"\x01\x00")]
EXPANSION_ON = [(0xA4, b"\x01\x01")]


def test_the_expansion_switch_shows_the_app_value_for_2_s_replays_2026_10_06() -> None:
    """a1783-control.md "``4104``": MQTT 19:43:30 ``c0[28]`` 0, 19:43:32 back to 1.

    Mains charging (``flag_00c7``), the off arms nothing: the override shows the
    app's 0 until the block-0xf reread 2 s later pushes a ``0421`` with the
    expansion's own switch-on state.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**CHARGING_MAIN)
    client = authorized_client(device, TOKEN)

    command(device, client, 0x104, EXPANSION_OFF)
    shown = next_0421(device)[0]["sub_package_switch_status"]
    clock.advance(1.9)
    still = pushed(client, [device.negotiated_module.check_pushes()])
    clock.advance(0.1)
    reread = pushed(client, [device.negotiated_module.check_pushes()])

    assert shown == 0
    assert "4421" not in {cmd for cmd, _ in still}
    assert "4421" in {cmd for cmd, _ in reread}
    assert next_0421(device)[0]["sub_package_switch_status"] == 1
    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


#: The expansion discharging to the ES720 with no charge source in.
OFF_GRID_EXPANSION = {
    **EXPANSION_DISCHARGING,
    "ac_input_disable_switch": 0,
    "ac_input_status": 0,
}


def test_switching_the_expansion_off_returns_the_bus_to_the_main() -> None:
    """No charge source: the off reaches the expansion and the main takes the bus.

    ``pack_select_eval`` returns the selection to the main (``0x21``) at once;
    the reread 2 s later still reads 0, the expansion now off (``flag_0b56``).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**OFF_GRID_EXPANSION)
    client = authorized_client(device, TOKEN)

    command(device, client, 0x104, EXPANSION_OFF)
    values = next_0421(device)[0]
    slots = tuple(next_0490(device)[name] for name in SLOTS)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert (values["sub_package_status"], values["charge_discharge_status"]) == (0, 1)
    assert slots == (8, 9, 3, 3)
    assert next_0421(device)[0]["sub_package_switch_status"] == 0


def test_switching_the_expansion_back_on_reads_1() -> None:
    """An on reaches the expansion whatever the source; the main keeps the bus."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**OFF_GRID_EXPANSION)
    client = authorized_client(device, TOKEN)
    command(device, client, 0x104, EXPANSION_OFF)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    command(device, client, 0x104, EXPANSION_ON)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert next_0421(device)[0]["sub_package_switch_status"] == 1
    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_a_switched_off_expansion_takes_no_charge_at_the_cap() -> None:
    """Off, the expansion may not charge: the main keeps charging past its cap."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**CHARGING_MAIN, "ac_input_status": 0, "ac_input_power": 0})
    client = authorized_client(device, TOKEN)
    command(device, client, 0x104, EXPANSION_OFF)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    device.set_values(ac_input_status=1, ac_input_power=1210)
    device.set_values(battery_level=95, pack_soc=950)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert tuple(next_0490(device)[name] for name in SLOTS) == (8, 9, 3, 3)


def test_battery_only_mains_is_a_charge_source_for_the_switch() -> None:
    """``.14.30`` bit 0x02 is set with ``a7[3]`` 2 too: the off arms nothing.

    a1783-telemetry.md ``.14.30`` bits (2026-10-06 23:34:07, 0x12 with ``a7[3]`` 2).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**EXPANSION_DISCHARGING)
    client = authorized_client(device, TOKEN)

    command(device, client, 0x104, EXPANSION_OFF)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert next_0421(device)[0]["sub_package_switch_status"] == 1
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_pv_input_is_a_charge_source_for_the_switch() -> None:
    """``.14.30`` bit 0 (PV / DC in) sets ``flag_00c7``: the off arms nothing."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**OFF_GRID_EXPANSION, "pv1_input_power": 120})
    client = authorized_client(device, TOKEN)

    command(device, client, 0x104, EXPANSION_OFF)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert next_0421(device)[0]["sub_package_switch_status"] == 1
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_the_alternator_charging_is_a_charge_source_for_the_switch() -> None:
    """``flag_00c7`` also reads the ALT record's mode 2: the off arms nothing.

    mainbms-control-fsms.md "``flag_00c7``" (block A +3), here with no DC watts.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        **OFF_GRID_EXPANSION,
        external_device_mode=solix_c_gen2.ExternalDeviceMode.CHARGING,
    )
    client = authorized_client(device, TOKEN)

    command(device, client, 0x104, EXPANSION_OFF)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert next_0421(device)[0]["sub_package_switch_status"] == 1
    assert tuple(next_0490(device)[name] for name in SLOTS) == (3, 3, 8, 9)


def test_the_bus_holder_is_main_or_expansion() -> None:
    """Hidden ``pack_on_bus``: 1 main, 2 expansion."""
    device = A1783()

    with pytest.raises(TypeError, match="pack_on_bus"):
        device.set_values(pack_on_bus=3)


#: The 2026-10-20 04:58 bench unit: mains in, the main on the bus at its 95 % cap,
#: a 6 W pad on PD2 draining it, the expansion attached at 95 % (a1783-telemetry.md
#: "AC top-off burst").
TOPPED_OFF = {
    **KASA_UNIT,
    "ac_input_disable_switch": 0,
    "ac_input_status": 1,
    "ac_input_power": 0,
    "battery_level": 95,
    "charging_limit_value": 95,
    "pack_soc": 950,
    "total_output_power": 6,
    "sub_package_battery_level": 95,
    "discharge_latch_from": 950,
}


def test_an_ac_top_off_burst_replays_2026_10_20() -> None:
    """04:57:40: SoC under the 95.0 % clear threshold reopens charge, AC in ~1.2 kW.

    ``a5[1]`` 2 at once and ``a3[0]`` after its lag; ~1 min later (off by 04:59)
    the SoC is past ``max_soc`` + 0.7 % and the burst stops.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOPPED_OFF)

    device.set_values(pack_soc=949)
    started = next_0421(device)[0]
    clock.advance(2)
    device.negotiated_module.check_pushes()
    charging = next_0421(device)[0]["work_status"]
    clock.advance(58)
    device.negotiated_module.check_pushes()
    ended = next_0421(device)[0]

    assert (started["ac_input_power"], started["charge_discharge_status"]) == (1208, 2)
    assert charging == 2
    assert (ended["ac_input_power"], ended["charge_discharge_status"]) == (0, 1)
    assert device.negotiated_module.values["pack_soc"] == 957


@pytest.mark.parametrize(
    ("cap", "clear"),
    [
        pytest.param(95, 950, id="cap-95"),
        pytest.param(99, 985, id="cap-99"),
        pytest.param(100, 999, id="cap-100"),
    ],
)
def test_the_top_off_reopens_below_the_clear_threshold(cap: int, clear: int) -> None:
    """``soc_status_thresholds_load``: ``max_soc``, 98.5 % at 99, 99.9 % at 100."""
    device = A1783(clock=ManualClock())
    device.set_values(
        **{**TOPPED_OFF, "charging_limit_value": cap, "pack_soc": clear + 5}
    )

    device.set_values(pack_soc=clear)
    at_threshold = next_0421(device)[0]["ac_input_power"]
    device.set_values(pack_soc=clear - 1)

    assert at_threshold == 0
    assert next_0421(device)[0]["ac_input_power"] == 1208


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"ac_input_status": 0}, id="no-mains"),
        pytest.param({"ac_input_disable_switch": 1}, id="battery-only"),
        pytest.param({"pack_on_bus": 2}, id="expansion-on-the-bus"),
    ],
)
def test_no_top_off_without_mains_on_a_main_on_the_bus(
    values: dict[str, Value],
) -> None:
    """The burst is AC charge into the on-bus main; without that, nothing starts.

    No mains, Battery-Only Power on, or the expansion holding the bus.
    """
    device = A1783(clock=ManualClock())
    device.set_values(**{**TOPPED_OFF, **values})

    device.set_values(pack_soc=949)

    assert device.negotiated_module.values["ac_input_power"] == 0


#: ``4489`` with the OTA precheck allowed (``a2`` 1, ``a3`` 0), and blocked by
#: low SoC (``a2`` 0, ``a3`` 2) (a1783-control.md ``4089``).
OTA_ALLOWED_4489 = ("4489", "a10131a2020101a3020100a402012da502010fa602010f")
OTA_LOW_SOC_4489 = ("4489", "a10131a2020100a3020102a402012da502010fa602010f")


def test_the_ota_precheck_pushes_as_the_expansion_reaches_6_replays() -> None:
    """MQTT 2026-10-06 20:21:28 / BLE 2026-10-15 07:45:55: ``c0[25]`` 5 -> 6 %.

    The ``4489`` with the pair 1 0 follows 0.15 s after the frame.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        battery_level=95, sub_package_connection_status=1, sub_package_battery_level=5
    )
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module

    device.set_values(sub_package_battery_level=6)
    clock.advance(0.1)
    early = pushed(client, [module.check_pushes()])
    clock.advance(0.05)

    assert early == []
    assert pushed(client, [module.check_pushes()]) == [OTA_ALLOWED_4489]


@pytest.mark.parametrize(
    ("levels", "event"),
    [
        pytest.param((50, 5), OTA_LOW_SOC_4489, id="main-below-6"),
        pytest.param((5, 6), OTA_ALLOWED_4489, id="main-back-to-6"),
    ],
)
def test_the_ota_precheck_follows_the_main_pack(
    levels: tuple[int, int], event: tuple[str, str]
) -> None:
    """``ota_precheck_update``: 0 2 below 6 % (integer), else 1 0; a change pushes."""
    clock = ManualClock()
    device = A1763(clock=clock)
    device.set_values(battery_level=levels[0])
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    clock.advance(1)
    module.check_pushes()

    device.set_values(battery_level=levels[1])
    clock.advance(0.2)

    assert pushed(client, [module.check_pushes()]) == [event]


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"battery_level": 40}, id="no-crossing"),
        pytest.param(
            {"sub_package_connection_status": 0, "sub_package_battery_level": 3},
            id="detached-expansion",
        ),
        pytest.param(
            {"battery_level": 4, "ota_enable": 1, "ota_block_reason": 0},
            id="the-call-sets-the-pair",
        ),
    ],
)
def test_no_ota_precheck_push_without_a_changed_pair(values: dict[str, Value]) -> None:
    """Only a pair the SoCs change is pushed.

    No crossing, a detached expansion's SoC, or a pair the call sets itself.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(battery_level=50, sub_package_connection_status=0)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module

    device.set_values(**values)
    clock.advance(1)
    out = pushed(client, [module.check_pushes()])

    assert [push for push in out if push[0] == "4489"] == []


#: 2026-10-15 05:44:42: Battery-Only Power on with mains in, the main at its 5 %
#: floor (6.0 % in 0.1 %) on the bus, discharging into the loads, the expansion
#: attached at 5 %.
AT_THE_FLOOR = {
    **KASA_UNIT,
    "ac_input_disable_switch": 1,
    "ac_input_status": 2,
    "ac_input_power": 0,
    "battery_level": 5,
    "discharge_limit_value": 5,
    "charging_limit_value": 95,
    "pack_soc": 60,
    "total_output_power": 60,
    "discharge_latch_from": 60,
}


def test_battery_only_drops_at_the_main_floor_replays_2026_10_15() -> None:
    """a1783-operation.md "Battery-Only Power at the floor", BLE 05:45:36-05:46:21.

    Under ``min_soc`` + 0.9 % the BMS's low-battery bit drops the mode: state
    1 -> 2 and ``a7[3]`` 2 -> 1 at once, AC charging 4 s later from the power
    stage's 118 W first step (the frames: 96 -> 609 -> 1 227 W); back above it,
    state 2 -> 0 and the screen on 1 s later.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**AT_THE_FLOOR)
    module = device.negotiated_module

    device.set_values(pack_soc=58)
    dropped = next_0421(device)[1]
    clock.advance(4)
    module.check_pushes()
    charging = next_0421(device)[0]
    device.set_values(pack_soc=59, battery_level=6)
    clock.advance(1)
    module.check_pushes()
    cleared = next_0421(device)[1]

    a4 = decode_field(dropped, "a4", GreedyBytes)
    assert (f"{a4[31]:02x}", decode_field(dropped, "a7", GreedyBytes)[3]) == ("0a", 1)
    assert (charging["ac_input_power"], charging["charge_discharge_status"]) == (
        118,
        2,
    )
    a4 = decode_field(cleared, "a4", GreedyBytes)
    assert (f"{a4[31]:02x}", a4[21]) == ("02", 1)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"ac_input_disable_switch": 0, "ac_input_status": 1}, id="off"),
        pytest.param({"ac_input_status": 0}, id="no-mains"),
    ],
)
def test_no_floor_drop_without_battery_only_on_mains(values: dict[str, Value]) -> None:
    """The floor drop clears Battery-Only Power held on mains, and nothing else."""
    device = A1783(clock=ManualClock())
    device.set_values(**{**AT_THE_FLOOR, **values})
    before = device.negotiated_module.values["ac_input_disable_switch"]

    device.set_values(pack_soc=58)

    assert device.negotiated_module.values["ac_input_disable_switch"] == before


def test_battery_only_set_again_before_the_clear_stays_on() -> None:
    """The clear acts only on state 2: Battery-Only Power switched on again stays."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**AT_THE_FLOOR)
    device.set_values(pack_soc=58)
    clock.advance(5)
    device.negotiated_module.check_pushes()

    device.set_values(pack_soc=59)
    device.set_values(ac_input_disable_switch=1)
    clock.advance(1)
    device.negotiated_module.check_pushes()

    assert device.negotiated_module.values["ac_input_disable_switch"] == 1


def test_each_pack_soc_shows_as_its_battery_level() -> None:
    """``a5[2]`` = ``a6[8]`` = ``.23.1`` = floor(``.14.18`` / 10), per pack.

    a1783-telemetry.md ``a6`` [8] and c490 ``.23``: the expansion's
    ``.23.1#1`` and ``c0[25]`` from ``.14#1.18`` the same way.
    """
    device = A1783()

    device.set_values(pack_soc=957, sub_package_pack_soc=449)
    values, fields = next_0421(device)
    post = next_0490(device)

    assert (values["battery_level"], values["sub_package_battery_level"]) == (95, 44)
    assert decode_field(fields, "a5", GreedyBytes)[2] == 95
    assert decode_field(fields, "a6", GreedyBytes)[8] == 95
    assert (post["battery_level"], post["sub_package_battery_level"]) == (95, 44)


def test_a_battery_level_set_by_name_stays_as_set() -> None:
    """Set together, the battery level by name wins over its ``pack_soc``."""
    device = A1783()

    device.set_values(battery_level=96, pack_soc=957)

    assert next_0421(device)[0]["battery_level"] == 96


def test_a_battery_level_set_alone_sets_its_pack_soc() -> None:
    """Each pack's level set alone puts its 0.1 % SoC at the level's whole percent."""
    device = A1783()

    device.set_values(battery_level=94, sub_package_battery_level=37)
    post = next_0490(device)

    assert (post["pack_soc"], post["sub_package_pack_soc"]) == (940, 370)


@pytest.mark.parametrize(
    ("name", "in_0490"),
    [
        pytest.param("charge_discharge_status", False, id="a5[1]"),
        pytest.param("sub_package_status", False, id="c0[24]"),
        pytest.param("work_status", False, id="a3[0]"),
        pytest.param("on_bus_pack_status", True, id=".23.7"),
        pytest.param("total_input_power", True, id=".23.4"),
    ],
)
def test_a_flow_state_set_by_name_stands(name: str, *, in_0490: bool) -> None:
    """A flow-state name set alone reads as set; none of its inputs moved.

    Battery-Only Power off and no mains: no AC hold reshapes the frame.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(ac_input_disable_switch=0, ac_input_status=0)

    device.set_values(**{name: 1})
    clock.advance(3)
    device.negotiated_module.check_pushes()

    assert (next_0490(device) if in_0490 else next_0421(device)[0])[name] == 1


def test_an_input_total_set_beside_its_inputs_stands_until_they_next_change() -> None:
    """``.23.4`` set with the AC and DC powers keeps; the next DC change refills it."""
    device = A1783(clock=ManualClock())
    device.set_values(
        ac_input_disable_switch=0,
        ac_input_status=1,
        ac_input_power=600,
        pv1_input_power=90,
        total_input_power=5,
    )
    held = next_0490(device)["total_input_power"]

    device.set_values(pv1_input_power=100)

    assert held == 5
    assert next_0490(device)["total_input_power"] == 700


#: The main on the bus, both packs below their caps, no mains: an output step
#: moves both statuses.
MAIN_BELOW_CAPS = {
    "pack_on_bus": 1,
    "pack_soc": 800,
    "sub_package_pack_soc": 800,
    "discharge_latch_from": 800,
    "ac_input_disable_switch": 0,
    "ac_input_status": 0,
}


def test_a3_set_beside_a_pack_current_change_skips_its_follow() -> None:
    """Output on with ``a3[0]`` 0 given: ``.23.7`` follows to 1, ``a3[0]`` stays 0."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**MAIN_BELOW_CAPS)
    clock.advance(5)
    device.negotiated_module.check_pushes()

    device.set_values(total_output_power=60, work_status=0)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert next_0421(device)[0]["work_status"] == 0
    assert next_0490(device)["on_bus_pack_status"] == 1


@pytest.mark.parametrize(
    ("names", "statuses"),
    [
        pytest.param({"work_status": 0}, (0, 1), id="a3[0]"),
        pytest.param({"work_status": 0, "on_bus_pack_status": 0}, (0, 0), id="both"),
    ],
)
def test_a_status_set_while_its_follow_waits_stays(
    names: dict[str, Value], statuses: tuple[int, int]
) -> None:
    """Output on queues the follow; a status set 1 s in stands at 2 s, the rest follow.

    The follow was queued before the set, by no later input change.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**MAIN_BELOW_CAPS)
    clock.advance(5)
    device.negotiated_module.check_pushes()

    device.set_values(total_output_power=60)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    device.set_values(**names)
    clock.advance(1)
    device.negotiated_module.check_pushes()

    assert (next_0421(device)[0]["work_status"], next_0490(device)[ON_BUS]) == statuses


def test_the_next_status_move_ends_a_status_set_by_name() -> None:
    """``a3[0]`` set to 2 holds through a queued follow; output off moves it to 0."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**MAIN_BELOW_CAPS)
    clock.advance(5)
    device.negotiated_module.check_pushes()
    device.set_values(total_output_power=60)
    clock.advance(1)
    device.negotiated_module.check_pushes()
    device.set_values(work_status=2)
    clock.advance(2)
    device.negotiated_module.check_pushes()
    held = next_0421(device)[0]["work_status"]

    device.set_values(total_output_power=0)
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert held == 2
    assert next_0421(device)[0]["work_status"] == 0


def test_the_0490_input_total_sums_the_ac_and_dc_inputs() -> None:
    """``.23.4`` = ``a6[2:4]`` + ``a6[4:6]``: AC in plus DC in, one state."""
    device = A1783(clock=ManualClock())

    device.set_values(
        ac_input_disable_switch=0,
        ac_input_status=1,
        ac_input_power=600,
        pv1_input_power=90,
    )

    assert next_0490(device)["total_input_power"] == 690


#: ``.23.7``, the on-bus pack's status.
ON_BUS = "on_bus_pack_status"
#: (bus holder, values) -> (``a3[0]``, ``.23.7``) once 20 samples have passed.
PACK_STATUS_CASES = [
    pytest.param(1, {"pv1_input_power": 100}, (2, 2), id="main-charging"),
    pytest.param(1, {"total_output_power": 60}, (1, 1), id="main-discharging"),
    pytest.param(1, {"pack_current": 500}, (0, 0), id="main-charging-no-source"),
    pytest.param(1, {"pack_current": 29}, (0, 0), id="main-under-0.3-a"),
    pytest.param(2, {"total_output_power": 60}, (0, 1), id="expansion-discharging"),
    pytest.param(2, {"pv1_input_power": 100}, (0, 0), id="expansion-charging"),
]


@pytest.mark.parametrize(("bus", "values", "statuses"), PACK_STATUS_CASES)
def test_a3_and_the_on_bus_status_follow_the_pack_current(
    bus: int, values: dict[str, Value], statuses: tuple[int, int]
) -> None:
    """a1783-telemetry.md "Flow state" ``a3[0]`` and ``.23.7``: +-0.3 A, 20 samples.

    ``a3[0]`` is the main's status, 0 while the expansion holds the bus;
    ``.23.7`` the bus holder's. A charge needs ``flag_00c7`` on the main, and
    the expansion's 2 reads 0. Both packs sit below their caps.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(
        pack_on_bus=bus,
        pack_soc=800,
        sub_package_pack_soc=800,
        discharge_latch_from=800,
        ac_input_disable_switch=0,
        ac_input_status=0,
    )
    clock.advance(5)
    device.negotiated_module.check_pushes()

    device.set_values(**values)
    early = (next_0421(device)[0]["work_status"], next_0490(device)[ON_BUS])
    clock.advance(2)
    device.negotiated_module.check_pushes()

    assert early == (0, 0)
    assert (next_0421(device)[0]["work_status"], next_0490(device)[ON_BUS]) == statuses


def test_the_top_off_end_shows_its_pack_soc_as_the_battery_level() -> None:
    """The burst ends with ``pack_soc`` at the set threshold, 95.7 %: 95 %."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**TOPPED_OFF)

    device.set_values(pack_soc=949)
    shown = next_0421(device)[0]["battery_level"]
    clock.advance(60)
    device.negotiated_module.check_pushes()

    assert shown == 94
    assert next_0421(device)[0]["battery_level"] == 95


#: (bus holder, PV in, AC in, output) -> (``a5[1]``, ``c0[24]``).
PORT_FLOW_CASES = [
    pytest.param(1, 0, 0, 0, (0, 0), id="main-idle"),
    pytest.param(1, 0, 0, 60, (1, 0), id="main-out"),
    pytest.param(1, 100, 0, 60, (2, 0), id="main-in-over-out"),
    pytest.param(1, 50, 0, 60, (1, 0), id="main-in-under-out"),
    pytest.param(1, 60, 0, 60, (1, 0), id="main-in-equal-out"),
    pytest.param(1, 0, 1210, 0, (2, 0), id="main-ac-in"),
    pytest.param(2, 0, 0, 0, (0, 0), id="expansion-idle"),
    pytest.param(2, 0, 0, 60, (0, 1), id="expansion-out"),
    pytest.param(2, 100, 0, 60, (0, 2), id="expansion-in-over-out"),
    pytest.param(2, 0, 1210, 0, (0, 2), id="expansion-ac-in"),
]


@pytest.mark.parametrize(("bus", "pv", "ac", "out", "flows"), PORT_FLOW_CASES)
def test_the_port_flow_shows_on_the_bus_holder(
    bus: int, pv: int, ac: int, out: int, flows: tuple[int, int]
) -> None:
    """a1783-telemetry.md "Flow state": ``a5[1]`` / ``c0[24]`` from the port powers.

    The port-flow code is 2 with Σ input over Σ output, 0 with no output, else
    1, no debounce; ``a5[1]`` carries it while the main holds the bus and
    ``c0[24]`` while the expansion does, the other reading 0.
    """
    device = A1783(clock=ManualClock())
    device.set_values(pack_on_bus=bus, ac_input_disable_switch=0, ac_input_status=1)

    device.set_values(pv1_input_power=pv, ac_input_power=ac, total_output_power=out)
    values = next_0421(device)[0]

    assert (values["charge_discharge_status"], values["sub_package_status"]) == flows


def test_the_port_flow_moves_with_the_bus() -> None:
    """The holder changing moves the code from ``c0[24]`` to ``a5[1]``."""
    device = A1783(clock=ManualClock())
    device.set_values(pack_on_bus=2, total_output_power=60)

    device.set_values(pack_on_bus=1)
    values = next_0421(device)[0]

    assert (values["charge_discharge_status"], values["sub_package_status"]) == (1, 0)


def test_with_no_expansion_the_main_shows_the_port_flow() -> None:
    """The C1000 Gen 2 has no ``c0``: ``a5[1]`` always carries the code."""
    device = A1763(clock=ManualClock())

    device.set_values(pv1_input_power=100, total_output_power=60)

    assert next_0421(device)[0]["charge_discharge_status"] == 2


def test_a_reattached_port_over_the_input_shows_output() -> None:
    """Charging at 50 W from PV, PD1's 86 W back after a socket tap outweighs it: 1.

    The port power step shows the port flow, not only idle to output.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    device.set_values(pack_on_bus=1, pv1_input_power=50)
    charging = next_0421(device)[0]["charge_discharge_status"]

    for on in (1, 0):
        command(device, client, 0x102, [(0xA2, bytes([0x01, on]))])
        clock.advance(2)
        device.negotiated_module.check_pushes()
    clock.advance(4)
    device.negotiated_module.check_pushes()
    values = next_0421(device)[0]

    assert charging == 2
    assert (values["total_output_power"], values["charge_discharge_status"]) == (86, 1)


def test_a_port_flow_change_re_derives_the_pack_current() -> None:
    """``.14.29`` / ``.14#1.29``: the net port flow over the bus holder's voltage.

    10 mA = W x 1000 / V in 0.1 V (a1783-telemetry.md "Flow state", current
    source); the main at 53.5 V, the expansion at 53.2 V; the other pack 0.
    """
    device = A1783(clock=ManualClock())
    device.set_values(pack_on_bus=1, discharge_latch_from=1000)
    currents = []
    for values in (
        {"pv1_input_power": 100},
        {"total_output_power": 200},
        {"pack_on_bus": 2},
    ):
        device.set_values(**values)
        post = next_0490(device)
        currents.append((post["pack_current"], post["sub_package_pack_current"]))

    assert currents == [(187, 0), (-187, 0), (0, -188)]


def test_the_0490_counter_page_turns_every_360_s() -> None:
    """``.14.25`` / ``.14.26``: each pack's page advances every 3 601 calls at 10 Hz.

    a1783-telemetry.md ``.14.25`` / ``.14.26`` and "pages"; c490-schema.md
    ``page_index_get``. 540 s posts land 1.5 pages apart; the recorded main
    reads page 3 at 707 h, the expansion page 0 at 14 h.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    pages = []
    for _ in range(4):
        clock.advance(540)
        device.negotiated_module.check_pushes()
        post = next_0490(device)
        pages.append((post["counter_page"], post["sub_package_counter_page"]))
    hours = next_0490(device)["counter_page_hours"]
    clock.advance(1442)  # 3 602 s: the 10th turn, 3 601 s counted
    device.negotiated_module.check_pushes()

    assert pages == [(0, 1), (1, 2), (3, 0), (0, 1)]
    assert hours == 707
    assert next_0490(device)["counter_page_hours"] == 708


def test_a_detached_expansion_s_counter_page_holds() -> None:
    """With no expansion attached its page doesn't turn; the main's does."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(sub_package_connection_status=0)

    clock.advance(400)
    device.negotiated_module.check_pushes()
    post = next_0490(device)

    assert (post["counter_page"], post["sub_package_counter_page"]) == (0, 0)


def test_a_pack_current_set_with_the_change_stays() -> None:
    """Set beside a port-flow change, ``pack_current`` wins in that change."""
    device = A1783(clock=ManualClock())
    device.set_values(pack_on_bus=1, discharge_latch_from=1000)

    device.set_values(pv1_input_power=100, pack_current=12)

    assert next_0490(device)["pack_current"] == 12


def test_the_auto_off_waits_for_the_power_stage_flag() -> None:
    """a1783-control.md "``a4[31]`` witnesses": the auto-off keys on the AC-input flag.

    The power stage's flag (``0x2000054a`` bit 0) drops with ``a7[3]`` and settles
    after the input monitor's 10-tick, 10 ms debounce: ``a4[31]`` ``06`` -> ``0a``
    0.1 s on.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**KASA_UNIT)

    device.set_values(ac_input_status=0)
    clock.advance(0.05)
    device.negotiated_module.check_pushes()
    early = decode_field(next_0421(device)[1], "a4", GreedyBytes)[31]
    clock.advance(0.05)
    device.negotiated_module.check_pushes()

    assert early == 0x06
    assert decode_field(next_0421(device)[1], "a4", GreedyBytes)[31] == 0x0A


def test_the_accept_waits_for_the_power_stage_report() -> None:
    """Mains back: the flag returns after the power stage's report and the debounce.

    The report, 2.9 s (the bench's accept 2-3 s after ``a7[3]``), isn't in MainMcu;
    the accept (state 2 -> 0; ``08`` -> ``00`` with the outage alert off) lands
    3.0 s after ``a7[3]`` returns.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**KASA_UNIT, "outage_alert_switch": False})
    device.set_values(ac_input_status=0)
    clock.advance(1)
    device.negotiated_module.check_pushes()

    device.set_values(ac_input_status=1)
    clock.advance(2.9)
    device.negotiated_module.check_pushes()
    early = decode_field(next_0421(device)[1], "a4", GreedyBytes)[31]
    clock.advance(0.1)
    device.negotiated_module.check_pushes()

    assert early == 0x08
    assert decode_field(next_0421(device)[1], "a4", GreedyBytes)[31] == 0x00


def test_the_alert_commits_on_its_fifth_tick() -> None:
    """The off-grid alert's 1 s tick commits the flag's drop on the 5th differing tick.

    The first counted tick is taken at the flag's drop, so the ``0x59`` goes out
    4 s after it: 4.1 s after ``a7[3]`` (15:28:45 -> :49 on the bench).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**KASA_UNIT)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module

    device.set_values(ac_input_status=0)
    clock.advance(4.0)
    early = pushed(client, [module.check_pushes()])
    clock.advance(0.1)

    assert [push for push in early if push[0] == "4425"] == []
    assert pushed(client, [module.check_pushes()]) == [GRID_LOSS_4425]


@pytest.mark.parametrize(
    ("back_after", "alerts"),
    [
        pytest.param(1.0, [], id="flag-back-before-the-commit"),
        pytest.param(1.2, [GRID_LOSS_4425], id="flag-back-after-it"),
    ],
)
def test_a_flag_back_before_the_alert_commits_cancels_it(
    back_after: float, alerts: list[tuple[str, str]]
) -> None:
    """Mains back resets the count once the flag returns (3 s on), not at ``a7[3]``."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**KASA_UNIT)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module

    device.set_values(ac_input_status=0)
    clock.advance(back_after)
    device.set_values(ac_input_status=1)
    clock.advance(10)
    out = pushed(client, [module.check_pushes()])

    assert [push for push in out if push[0] == "4425"] == alerts


def test_the_charge_enables_after_the_one_shot_and_ramps() -> None:
    """a1783-control.md:870: the enable on the 1 s tick after the 7 s one-shot.

    8 s after the accept (the bench: power 4-8 s after it), 118 W first, then the
    full charge 5 s on (both bench runs).
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**KASA_UNIT, "outage_alert_switch": False})
    module = device.negotiated_module
    device.set_values(ac_input_status=0)
    clock.advance(1)
    module.check_pushes()
    device.set_values(ac_input_status=1)
    clock.advance(3)
    module.check_pushes()
    powers = []

    for seconds in (7.9, 0.1, 4.9, 0.1):
        clock.advance(seconds)
        module.check_pushes()
        powers.append(next_0421(device)[0]["ac_input_power"])

    assert powers == [0, 118, 118, 1208]


@pytest.mark.parametrize(
    "on_at",
    [pytest.param(4, id="before-the-enable"), pytest.param(10, id="in-the-ramp")],
)
def test_battery_only_on_holds_the_charge_enable_off(on_at: float) -> None:
    """a1783-control.md:870: the charge enable is held 0 while Battery-Only is on."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**KASA_UNIT, "outage_alert_switch": False})
    module = device.negotiated_module
    device.set_values(ac_input_status=0)
    clock.advance(1)
    module.check_pushes()
    device.set_values(ac_input_status=1)
    clock.advance(3 + on_at)
    module.check_pushes()

    device.set_values(ac_input_disable_switch=1)
    clock.advance(14 - on_at)
    module.check_pushes()

    assert next_0421(device)[0]["ac_input_power"] == 0


def test_a_committed_return_posts_nothing() -> None:
    """The flag's return commits on its 5th tick with no ``0425``."""
    clock = ManualClock()
    device = A1783(clock=clock)
    device.set_values(**{**KASA_UNIT, "ac_input_disable_switch": 0})
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(ac_input_status=0)
    clock.advance(5)
    module.check_pushes()

    device.set_values(ac_input_status=1)
    clock.advance(8)

    assert [p for p in pushed(client, [module.check_pushes()]) if p[0] == "4425"] == []
