# Copyright (c) 2026 Shawn Stricker
"""The SOLIX Alternator Charger (AS200) through a real ``bleak.BleakClient``."""

import pytest
from bleak import BleakClient

from anker_ble_emulator import (
    AS200,
    PRODUCTS,
    EmulatedBleakBackend,
    ModuleBuild,
    Outer,
    Product,
)
from anker_ble_emulator.devices.a1783 import PROFILE as A1783_PROFILE
from anker_ble_emulator.devices.as200 import (
    Output,
    Settings,
    Station,
    Status,
    WorkMode,
    WorkStatus,
)
from anker_ble_emulator.devices.base import data_resource
from anker_ble_emulator.devices.solix_c_gen2 import DeviceVersions
from anker_ble_emulator.layouts import LayoutError, ui
from anker_ble_emulator.mcu import REPLY
from anker_ble_emulator.parameters import Parameters, decode_field
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


TOKEN = b"owner-token"
#: The app's status request (``0100 a2 04 01``).
STATUS_REQUEST = [(0xA1, b"\x21"), (0xA2, b"\x04\x01")]
#: No time to full / empty.
NO_TIME = 0xFFFF


async def test_as200_grants_by_button_then_answers_the_status_request() -> None:
    device = AS200()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        status = await link.send(0x100, STATUS_REQUEST, SESSION)
        trigger = await link.send(
            0x057, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION
        )
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    assert device.outer == Outer.ENCRYPTED
    assert [cmd_hex(reply.frame) for reply in status] == ["4900", "4421"]
    reply = REPLY.parse(status[0].plaintext)
    assert reply.status == 0x00
    assert reply.fields == status[1].plaintext
    assert b"AS200" in status[1].plaintext
    assert [reply.plaintext for reply in trigger] == [bytes.fromhex("00a10131")]
    assert version == []
    assert device.advertisement_data.manufacturer_data == {}
    assert PRODUCTS[Product.AS200].solixble_class == ""


def test_as200_codecs_read_the_recorded_charge() -> None:
    """Charging a C2000 Gen 2 at 399 W through the expansion cable, 24 V LiFePO4."""
    fields = Parameters.parse(bytes.fromhex(data_resource("as200.json")["421"]))

    settings = decode_field(fields, "a4", Settings)
    status = decode_field(fields, "a6", Status)
    station = decode_field(fields, "a7", Station)
    versions = decode_field(fields, "f9", DeviceVersions)

    assert decode_field(fields, "a3", Output).total_output_power == 399
    assert status.total_output_power == 399
    assert (settings.power_switch, settings.current_work_mode) == (1, WorkMode.CHARGING)
    assert (settings.battery_type, settings.battery_voltage_type) == (0, 1)
    assert (
        settings.power_out_voltage,
        settings.charging_mode_power,
        settings.reverse_mode_power,
    ) == (257, 400, 200)
    assert (
        settings.shutdown_timeout,
        settings.shutdown_timeout_v2,
        settings.never_shutdown,
    ) == (0, 1440, 1)
    assert (status.work_status, status.temperature) == (WorkStatus.CHARGING, 40)
    assert (status.output_cable_status, status.xt60i_cable_status) == (1, 0)
    assert (
        status.charging_mode_power_min,
        status.charging_mode_power_max,
        status.reverse_mode_power_min,
        status.reverse_mode_power_max,
        status.power_out_voltage_min,
        status.power_out_voltage_max,
        status.battery_voltage,
    ) == (100, 800, 100, 800, 240, 276, 265)
    assert station.external_device_work_status == WorkStatus.CHARGING
    assert (
        station.external_device_remaining_charge_time,
        station.external_device_remaining_discharge_time,
        station.external_device_battery_level,
        station.external_device_temperature,
    ) == (8, NO_TIME, 91, 36)
    assert station.external_device_sn == A1783_PROFILE.serial.encode()
    assert station.external_device_product_code.rstrip(b"\x00") == b"A1783"
    assert versions.mcu_version == bytes.fromhex("00010101")
    module = "v" + ".".join(str(digit) for digit in reversed(versions.module_version))
    assert module == ModuleBuild.V0_3_2_7


async def test_as200_settings_show_in_the_telemetry_after_them() -> None:
    device = AS200()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        mode = await link.send(0x103, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        limits = await link.send(
            0x103,
            [
                (0xA1, b"\x21"),
                (0xA6, b"\x02" + ui(2).build(600)),
                (0xB4, b"\x02" + ui(2).build(250)),
            ],
            SESSION,
        )
        timeout = await link.send(
            0x103,
            [
                (0xA1, b"\x21"),
                (0xAE, b"\x02" + ui(2).build(720)),
                (0xBB, b"\x02" + ui(2).build(720)),
                (0xBC, b"\x01\x00"),
            ],
            SESSION,
        )
        shutdown = await link.send(
            0x103, [(0xA1, b"\x21"), (0xB8, b"\x01\x01")], SESSION
        )

    assert [cmd_hex(reply.frame) for reply in mode] == ["4903", "4421"]
    assert mode[0].plaintext == bytes.fromhex("00a10131")
    settings = decode_field(Parameters.parse(timeout[1].plaintext), "a4", Settings)
    assert settings.current_work_mode == WorkMode.REVERSE_CHARGING
    assert (settings.reverse_mode_power, settings.power_out_voltage) == (600, 250)
    assert (settings.shutdown_timeout, settings.shutdown_timeout_v2) == (720, 720)
    assert settings.never_shutdown == 0
    assert limits[1].plaintext != mode[1].plaintext
    # Powering off sets nothing the telemetry carries.
    assert shutdown[1].plaintext == timeout[1].plaintext


def test_as200_named_values_reach_every_tag_that_carries_them() -> None:
    device = AS200()
    recorded = Parameters.parse(device.mcu.push(0x421).payload)

    device.set_values(
        work_status=WorkStatus.REVERSE_CHARGING,
        total_output_power=721,
        external_device_remaining_discharge_time=23,
        external_device_sn=b"APCDKKE0000000009",
        timestamp=0x01020304,
    )

    fields = Parameters.parse(
        device.mcu.push(0x421, values=device.negotiated_module.values).payload
    )
    status = decode_field(fields, "a6", Status)
    station = decode_field(fields, "a7", Station)
    assert decode_field(fields, "a3", Output).total_output_power == 721
    assert (status.work_status, status.total_output_power) == (
        WorkStatus.REVERSE_CHARGING,
        721,
    )
    assert station.external_device_remaining_discharge_time == 23
    assert station.external_device_sn == b"APCDKKE0000000009"
    assert decode_field(fields, "fe", ui(4)) == 0x01020304
    assert fields["fe"]["type"] == 0x04
    # The bytes nothing names keep the recording's values.
    assert (
        status["_reserved_20"] == decode_field(recorded, "a6", Status)["_reserved_20"]
    )


def test_as200_pushes_its_ota_readiness() -> None:
    device = AS200()

    device.set_values(ota_enable=0, ota_block_reason=14)

    fields = Parameters.parse(
        device.mcu.push(0x489, values=device.negotiated_module.values).payload
    )
    assert decode_field(fields, "a2", ui(1)) == 0
    assert decode_field(fields, "a3", ui(1)) == 14
    assert fields["a4"]["value"] == b"\x2d"


@pytest.mark.parametrize("name", ["pd1_output_power", "battery_level"])
def test_as200_refuses_names_it_doesnt_carry(name: str) -> None:
    with pytest.raises(LayoutError):
        AS200().set_values(**{name: 1})
