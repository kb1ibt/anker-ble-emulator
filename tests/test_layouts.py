# Copyright (c) 2026 Shawn Stricker
"""Layouts: telemetry built and set by field name, commands checked by the map."""

import json

import pytest

from anker_ble_emulator.layouts import (
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    TYPE_UI,
    Field,
    Layout,
    LayoutError,
    encode_value,
)
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.maps import LAYOUT


def test_build_writes_every_typed_field_as_zero_after_the_ble_route() -> None:
    payload = Layout(LAYOUT).build(0x405)

    assert payload.hex() == (
        "a10131a2020100a303020000a5050000000000a6050300000000a7050500000000a80404000000"
    )


def test_named_values_are_encoded_by_their_type() -> None:
    payload = Layout(LAYOUT).build(
        0x405,
        {"soc": 87, "power": -5, "serial": b"AB", "energy": 70000, "ratio": 0.5},
    )

    fields = decode_fields(payload)
    assert fields[0xA2] == bytes.fromhex("0157")
    assert fields[0xA3] == bytes.fromhex("02fbff")
    assert fields[0xA5] == b"\x00AB\x00\x00"
    assert fields[0xA6] == bytes.fromhex("0370110100")
    assert fields[0xA7] == bytes.fromhex("050000003f")


def test_parts_are_set_at_their_offset() -> None:
    payload = Layout(LAYOUT).build(0x405, {"mode": 2, "limit": 0x0102})

    assert decode_fields(payload)[0xA8] == bytes.fromhex("04020201")


def test_update_changes_only_the_named_fields_of_a_recorded_payload() -> None:
    recorded = bytes.fromhex("a10131a2020164a3030210009901ff")

    updated = Layout(LAYOUT).update(0x405, recorded, {"soc": 50})

    assert updated == bytes.fromhex("a10131a2020132a3030210009901ff")


@pytest.mark.parametrize(
    ("msgtype", "values", "error"),
    [
        pytest.param(0x406, {}, LayoutError, id="unknown message"),
        pytest.param(0x405, {"untyped": 1}, LayoutError, id="untyped field"),
        pytest.param(0x405, {"soc": b"x"}, TypeError, id="bytes for a number"),
        pytest.param(0x405, {"serial": 1}, TypeError, id="number for bytes"),
    ],
)
def test_unknown_or_mistyped_values_are_refused(
    msgtype: int, values: dict[str, int | bytes], error: type[Exception]
) -> None:
    with pytest.raises(error):
        Layout(LAYOUT).build(msgtype, values)


@pytest.mark.parametrize(
    ("msgtype", "request_hex", "status"),
    [
        pytest.param(0x04A, "a10121a2020101", STATUS_ACCEPTED, id="option"),
        pytest.param(0x04A, "a10121a2020102", STATUS_REJECTED, id="not an option"),
        pytest.param(0x04A, "a10121fe050300000000", STATUS_ACCEPTED, id="no setting"),
        pytest.param(0x101, "a10121a40302f401", STATUS_ACCEPTED, id="in range"),
        pytest.param(0x101, "a10121a403025000", STATUS_REJECTED, id="below min"),
        pytest.param(0x101, "a10121a40302dc05", STATUS_REJECTED, id="above max"),
        pytest.param(0x101, "a10121a40302fa00", STATUS_REJECTED, id="off step"),
        pytest.param(0x101, "a10121a4020202", STATUS_REJECTED, id="too short"),
        pytest.param(0x101, "a10121a40102", STATUS_REJECTED, id="type only"),
        pytest.param(0x101, "a10121a400", STATUS_REJECTED, id="empty"),
        pytest.param(0x101, "a10121a503004142", STATUS_ACCEPTED, id="string"),
        pytest.param(0x101, "a10121a4", STATUS_REJECTED, id="truncated"),
    ],
)
def test_commands_are_checked_against_the_map(
    msgtype: int, request_hex: str, status: int
) -> None:
    assert Layout(LAYOUT).check(msgtype, bytes.fromhex(request_hex)) == status


def test_an_unmapped_command_is_refused() -> None:
    layout = Layout(LAYOUT)

    assert layout.has_command(0x04A)
    assert not layout.has_command(0x04B)
    with pytest.raises(LayoutError):
        layout.check(0x04B, b"")


@pytest.mark.parametrize(
    ("msgtype", "request_hex", "changes"),
    [
        pytest.param(0x04A, "a10121a2020101", {"soc": 1}, id="switch"),
        pytest.param(0x101, "a10121a40302f401", {"power": 500}, id="limit"),
        pytest.param(0x04C, "a10121a2020101", {"mode": 2}, id="converted"),
        pytest.param(0x04C, "a10121a3020101", {}, id="no telemetry field"),
        pytest.param(0x04C, "a10121a40400414243", {"serial": b"ABC"}, id="bytes"),
        pytest.param(0x04A, "a10121a2020102", {}, id="rejected"),
        pytest.param(0x04A, "a10121", {}, id="no setting"),
        pytest.param(0x04B, "a10121a2020101", {}, id="unmapped"),
        pytest.param(0x04A, "a10121a2", {}, id="truncated"),
    ],
)
def test_an_accepted_command_sets_the_telemetry_it_names(
    msgtype: int, request_hex: str, changes: dict[str, int | bytes]
) -> None:
    layout = Layout(LAYOUT)

    assert layout.state_changes(msgtype, bytes.fromhex(request_hex)) == changes


def test_a_converted_value_outside_its_table_sets_nothing() -> None:
    data = json.loads(json.dumps(LAYOUT))
    data["commands"]["004c"][0]["fields"]["a2"]["options"] = [0, 1, 7]

    assert Layout(data).state_changes(0x04C, bytes.fromhex("a10121a2020107")) == {}


@pytest.mark.parametrize(
    ("port", "state"),
    [(0, "usbc_1_switch"), (1, "usbc_2_switch"), (4, "usba_switch")],
)
def test_a_shared_opcode_picks_the_variant_its_selector_names(
    port: int, state: str
) -> None:
    layout = Layout.load("A2345")
    assert layout is not None
    request = bytes.fromhex(f"a10121a20201{port:02x}a3020101")

    assert layout.state_changes(0x207, request) == {state: 1}


def test_a_selector_no_variant_names_is_refused() -> None:
    layout = Layout.load("A2345")
    assert layout is not None

    assert layout.check(0x207, bytes.fromhex("a10121a2020107a3020101")) == (
        STATUS_REJECTED
    )


def test_extend_adds_fields_and_keeps_the_ones_already_typed() -> None:
    layout = Layout(LAYOUT)
    typed = layout.messages[0x405]
    existing = typed[0]

    layout.extend(
        {
            0x405: (
                Field(existing.tag, "other", TYPE_UI, 2),
                Field(0xEE, "added", TYPE_UI, 2),
            )
        }
    )

    assert layout.messages[0x405] == (*typed, Field(0xEE, "added", TYPE_UI, 2))
    assert decode_fields(layout.build(0x405, {"added": 7}))[0xEE] == b"\x01\x07"


def test_retype_replaces_a_message_s_typed_fields() -> None:
    layout = Layout(LAYOUT)

    layout.retype({0x405: (Field(0xEE, "only", TYPE_UI, 2),)})

    assert layout.messages[0x405] == (Field(0xEE, "only", TYPE_UI, 2),)
    assert layout.build(0x405).hex() == "a10131ee020100"


def test_locate_finds_fields_and_parts() -> None:
    layout = Layout(LAYOUT)

    assert layout.locate("soc") == (0x405,)
    assert layout.locate("limit") == (0x405,)
    assert layout.locate("untyped") == ()


def test_encode_value_pads_and_cuts_byte_values() -> None:
    assert encode_value(0x00, 3, b"ABCDE") == b"ABC"
    assert encode_value(0x06, 3, b"A") == b"A\x00\x00"
