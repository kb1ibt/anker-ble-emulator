# Copyright (c) 2026 Shawn Stricker
"""Layouts: telemetry built and set by field name, commands checked by the map."""

import pytest

from anker_ble_emulator.layouts import (
    STATUS_ACCEPTED,
    STATUS_REJECTED,
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


def test_encode_value_pads_and_cuts_byte_values() -> None:
    assert encode_value(0x00, 3, b"ABCDE") == b"ABC"
    assert encode_value(0x06, 3, b"A") == b"A\x00\x00"
