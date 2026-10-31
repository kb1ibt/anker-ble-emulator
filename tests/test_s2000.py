# Copyright (c) 2026 Shawn Stricker
"""The SOLIX S2000 (AS220) through a real ``bleak.BleakClient``."""

import pytest
from bleak import BleakClient

from anker_ble_emulator import AS220, EmulatedBleakBackend, Outer
from anker_ble_emulator.devices.as220 import AcOutput, Charger, Power
from anker_ble_emulator.devices.base import data_resource
from anker_ble_emulator.devices.solix_c_gen2 import (
    Battery,
    Capabilities,
    DcInput,
    DeviceVersions,
    MemorySwitch,
    OutputPort,
    Settings,
    Tou,
)
from anker_ble_emulator.layouts import LayoutError, ui
from anker_ble_emulator.mcu import REPLY
from anker_ble_emulator.parameters import Parameters, decode_field
from tests.fixtures.client import SESSION, AppClient, BleakLink, settle


TOKEN = b"a5220000-5011-4000-b000-000000000001"
#: The subscribe SolixBLE's AS220 sends to start the stream.
SUBSCRIBE = [(0xA1, b"\x21"), (0xA2, b"\x04\x01")]
#: Each rating at its edge; AC in is the charge plus the AC out. The charge
#: holds to the unit's limit (1200 W recorded) + 10 W.
AT_RATING = [
    pytest.param({"ac_input_power": 1833, "ac_output_power": 1833}, id="bypass"),
    pytest.param({"ac_input_power": 1210, "ac_output_power": 0}, id="charge"),
    pytest.param(
        {"ac_input_power": 1833, "ac_output_power": 623}, id="charge passing AC out"
    ),
    pytest.param(
        {"ac_recharge_power": 800, "ac_input_power": 810, "ac_output_power": 0},
        id="charge at a lower limit",
    ),
    pytest.param(
        {"super_fast_charge_status": 1, "ac_input_power": 1600, "ac_output_power": 0},
        id="UltraFast charge",
    ),
    pytest.param({"pv1_input_power": 400}, id="solar"),
    pytest.param({"ac_output_power": 3000}, id="AC out peak"),
    pytest.param({"pd1_output_power": 118}, id="USB"),
    pytest.param({"total_input_power": 2233}, id="input total"),
    pytest.param({"total_output_power": 3118}, id="output total"),
]
#: One watt over each rating, with what the refusal names.
OVER_RATING = [
    pytest.param(
        {"ac_input_power": 1834, "ac_output_power": 1834}, "AC in", id="bypass"
    ),
    pytest.param(
        {"ac_input_power": 1211, "ac_output_power": 0}, "the AC charge", id="charge"
    ),
    pytest.param(
        {"ac_input_power": 1833, "ac_output_power": 622},
        "the AC charge",
        id="charge passing AC out",
    ),
    pytest.param(
        {"ac_recharge_power": 800, "ac_input_power": 811, "ac_output_power": 0},
        "the AC charge",
        id="charge at a lower limit",
    ),
    pytest.param(
        {"super_fast_charge_status": 1, "ac_input_power": 1601, "ac_output_power": 0},
        "the AC charge",
        id="UltraFast charge",
    ),
    pytest.param({"pv1_input_power": 401}, "solar", id="solar"),
    pytest.param({"ac_output_power": 3001}, "AC out", id="AC out peak"),
    pytest.param({"pd1_output_power": 119}, "USB", id="USB"),
    pytest.param({"total_input_power": 2234}, "the input total", id="input total"),
    pytest.param({"total_output_power": 3119}, "the output total", id="output total"),
]


async def test_as220_grants_by_button_then_streams_on_subscribe() -> None:
    device = AS220()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        replies = await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        stream = await link.send(0x100, SUBSCRIBE, SESSION)
        setter = await link.send(0x057, [(0xA1, b"\x21")], SESSION)
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    assert device.outer == Outer.ENCRYPTED
    assert replies[0x827].status == 0x09
    assert replies[0x829].fields[0xA2] == b"ESP32"
    assert [reply.frame.cmd.msgtype for reply in stream] == [0x900, 0x421]
    status = REPLY.parse(stream[0].plaintext)
    assert status.status == 0x00
    assert status.fields == stream[1].plaintext
    assert b"AS220" in stream[1].plaintext
    assert [reply.plaintext for reply in setter] == [bytes.fromhex("00a10131")]
    assert version == []
    assert device.advertisement_data.manufacturer_data == {}


def test_as220_codecs_read_the_values_the_doc_verified() -> None:
    fields = Parameters.parse(bytes.fromhex(data_resource("as220.json")["421"]))

    settings = decode_field(fields, "a4", Settings)
    battery = decode_field(fields, "a5", Battery)
    power = decode_field(fields, "a6", Power)
    ac = decode_field(fields, "a7", AcOutput)

    assert (settings.ac_recharge_power, settings.ac_frequency) == (1200, 60)
    assert (battery.temperature, battery.battery_level, battery.health) == (
        26,
        100,
        100,
    )
    assert (power.ac_input_plug_status, power.total_input_power) == (1, 55)
    assert ac.ac_input_power == power.total_input_power
    # Full and passing mains through: no flow in or out of the battery.
    assert battery.charge_discharge_status == 0


def test_as220_shares_the_a1783_codecs_and_reads_f0_as_the_charger() -> None:
    """PR #65's ``c421``, read with the A1783's ``d9``/``dc``/``f9``/``fa`` codecs."""
    fields = Parameters.parse(bytes.fromhex(data_resource("as220.json")["421"]))

    settings = decode_field(fields, "a4", Settings)
    tou = decode_field(fields, "d9", Tou)
    versions = decode_field(fields, "f9", DeviceVersions)
    flags = decode_field(fields, "fa", Capabilities)

    assert decode_field(fields, "f0", Charger).ac_input_status == 1
    assert (
        tou.tou_reserve_power,
        tou.charging_limit_value,
        tou.discharge_limit_value,
    ) == (6, 100, 1)
    assert (tou.charging_limit_value, tou.discharge_limit_value) == (
        settings.charging_limit_value,
        settings.discharge_limit_value,
    )
    assert tou.tou_periods == b"\x00"
    assert decode_field(fields, "dc", MemorySwitch).memory_switch_error_code == 0
    assert versions.mcu_version == bytes.fromhex("09020001")
    assert versions.module_version == bytes.fromhex("00030300")
    assert flags.support_set_country_code
    assert flags.support_ac_input_disable
    assert not flags.support_upgrade_record


def test_as220_answers_4090_from_its_recording_with_no_stored_plan() -> None:
    """Its ``0890`` is recorded, so a ``tou_periods`` set implies no other value."""
    layout = AS220().layout
    assert layout is not None
    assert layout.implied({"tou_periods": b"\x00"}) == {}
    assert "tou_period_table" not in layout.names(0x421)


def test_as220_named_values_reach_both_telemetry_frames() -> None:
    device = AS220()
    recorded = Parameters.parse(device.mcu.push(0x421).payload)

    device.set_values(
        total_input_power=1129,
        ac_output_power=7,
        pd1_output_power=12,
        timestamp=0x01020304,
        charge_discharge_status=2,
        ac_input_status=0,
        charging_limit_value=90,
    )

    frames = device.mcu.respond(0x100, values=device.negotiated_module.values)
    for frame in frames:
        fields = Parameters.parse(frame.payload)
        power = decode_field(fields, "a6", Power)
        ac = decode_field(fields, "a7", AcOutput)
        usb = decode_field(fields, "aa", OutputPort)
        charger = decode_field(fields, "f0", Charger)
        assert power.total_input_power == 1129
        assert ac.ac_output_power == 7
        assert decode_field(fields, "a5", Battery).charge_discharge_status == 2
        # One app name, every tag that carries it.
        assert (charger.ac_input_status, ac.ac_input_status) == (0, 0)
        assert (
            charger["_reserved"] == decode_field(recorded, "f0", Charger)["_reserved"]
        )
        assert decode_field(fields, "a4", Settings).charging_limit_value == 90
        assert decode_field(fields, "d9", Tou).charging_limit_value == 90
        assert (usb.output_status, usb.output_power) == (0, 12)
        assert fields["fe"]["type"] == 0x03
        assert decode_field(fields, "fe", ui(4)) == 0x01020304
        # The reserved bytes keep the recording's values.
        assert power["_reserved"] == decode_field(recorded, "a6", Power)["_reserved"]
        assert ac["_reserved"] == decode_field(recorded, "a7", AcOutput)["_reserved"]
    assert [frame.cmd.msgtype for frame in frames] == [0x900, 0x421]


@pytest.mark.parametrize("values", AT_RATING)
def test_as220_takes_each_rating_at_its_edge(values: dict[str, int]) -> None:
    device = AS220()

    device.set_values(**values)

    module = device.negotiated_module
    assert {name: module.values[name] for name in values} == values


@pytest.mark.parametrize(("values", "what"), OVER_RATING)
def test_as220_refuses_one_watt_over_a_rating_and_sets_nothing(
    values: dict[str, int], what: str
) -> None:
    device = AS220()
    module = device.negotiated_module
    before, pinned = dict(module.values), set(module.pinned)

    with pytest.raises(TypeError, match=f"^{what} reads at most"):
        device.set_values(**values)

    assert (module.values, module.pinned) == (before, pinned)


def test_as220_usb_a_and_the_15_w_usb_c_share_18_w_of_the_usb_total() -> None:
    """``aa`` is one total: 100 W USB-C, then 18 W, not the 15 + 12 W ports' 27 W."""
    device = AS220()

    device.set_values(pd1_output_power=100 + 18)
    with pytest.raises(TypeError, match="USB reads at most 118 W"):
        device.set_values(pd1_output_power=100 + 15 + 12)

    assert device.negotiated_module.values["pd1_output_power"] == 118


def test_as220_refuses_a_change_that_leaves_the_charge_over_its_rating() -> None:
    """UltraFast off, or less AC out passed through, would leave AC in charging."""
    device = AS220()
    device.set_values(
        super_fast_charge_status=1, ac_input_power=1800, ac_output_power=200
    )
    module = device.negotiated_module
    before = dict(module.values)

    with pytest.raises(TypeError, match="the AC charge reads at most 1210 W"):
        device.set_values(super_fast_charge_status=0)
    with pytest.raises(TypeError, match="the AC charge reads at most 1600 W"):
        device.set_values(ac_output_power=199)

    assert module.values == before


def test_as220_refuses_a_limit_lowered_under_a_running_charge() -> None:
    """The charge holds to the unit's own limit + 10 W, the setting it runs at."""
    device = AS220()
    device.set_values(ac_input_power=1210, ac_output_power=0)
    module = device.negotiated_module
    before = dict(module.values)

    with pytest.raises(TypeError, match="the AC charge reads at most 1110 W"):
        device.set_values(ac_recharge_power=1100)
    device.set_values(ac_recharge_power=1100, ac_input_power=1110)

    assert module.values == {
        **before,
        "ac_recharge_power": 1100,
        "ac_input_power": 1110,
    }


def test_as220_refuses_names_it_doesnt_carry() -> None:
    device = AS220()
    assert device.layout is not None

    with pytest.raises(LayoutError):
        device.set_values(pd2_output_power=1)
    assert device.layout.names(0x857) == frozenset()
    assert device.layout.build(0x857, {"timestamp": 1}) == b""
    assert device.layout.update(0x857, b"\x00\xa1\x01\x31", {}) == b"\x00\xa1\x01\x31"
    device.set_values(charging_limit_value=90, pv1_charge_status=1)
    pushed = Parameters.parse(
        device.mcu.push(0x421, values=device.negotiated_module.values).payload
    )
    recorded = Parameters.parse(device.mcu.push(0x421).payload)
    assert decode_field(pushed, "a8", DcInput).pv1_charge_status == 1
    assert decode_field(pushed, "fe", ui(4)) == decode_field(recorded, "fe", ui(4))
