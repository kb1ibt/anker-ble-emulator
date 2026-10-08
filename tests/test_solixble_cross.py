# Copyright (c) 2026 Shawn Stricker
"""The SolixBLE cross-check: decode positions and command links against a map."""

import pytest

from anker_ble_emulator.layouts import TYPE_STR, TYPE_UI, Field, Layout, Part
from anker_ble_emulator.products import Product
from tests.fixtures.solixble import stream_read
from tools.solixble_cross import compare, cross_check, variant


#: A field with two named parts, after the type byte: a byte at 0, a word at 1.
PARTED = Field(
    0xA2,
    None,
    TYPE_UI,
    8,
    (Part("mode", 0, TYPE_UI, 1), Part("limit", 1, TYPE_UI, 2)),
)
WHOLE = Field(0xA3, "level", TYPE_UI, 2)
SERIAL = Field(0xA4, None, TYPE_STR, 20, (Part("sn", 2, TYPE_STR, 16),))


@pytest.mark.parametrize(
    ("read", "spec", "verdict"),
    [
        (stream_read("int", None, None), WHOLE, "agree"),
        (stream_read("int", 1, 2), WHOLE, "agree"),
        (stream_read("int", 1, 3), WHOLE, "conflict"),
        (stream_read("string", 1, None), WHOLE, "conflict"),
        (stream_read("int", 2, 3), WHOLE, "gap"),
        (stream_read("int", 2, 4), PARTED, "agree"),
        (stream_read("int", 2, 3), PARTED, "conflict"),
        (stream_read("int", 3, 4), PARTED, "conflict"),
        (stream_read("int", 5, 6), PARTED, "gap"),
        (stream_read("string", 4, 20), SERIAL, "agree"),
    ],
)
def test_a_read_is_compared_with_the_map_field_or_part_at_its_offset(
    read: dict[str, object], spec: Field, verdict: str
) -> None:
    assert compare(read, spec)[0] == verdict


def test_the_parameters_select_the_command_variant_by_its_selector() -> None:
    layout = Layout.load(Product.A2345)
    assert layout is not None

    chosen = variant(layout, 0x207, {"a1": "21", "a2": 1, "a3": 1})

    assert chosen is not None
    assert "usbc_2_switch" in {spec.get("state") for spec in chosen.values()}
    assert variant(layout, 0x207, {"a2": 7}) is None


def test_commands_link_by_a_position_a_property_reads_or_stay_unlinked() -> None:
    layout = Layout.load(Product.A1783)
    assert layout is not None
    facts = {
        "telemetry": ["421"],
        "snapshot": [],
        "properties": {
            "ac_output": [stream_read("int", 1, 2, tag="a7")],
            "ac_charging_power": [stream_read("int", None, None, tag="ff")],
            "timer": [{"source": "snapshot", "parse": "record", "tag": "aa"}],
        },
        "commands": [
            {"method": "turn_ac_on", "cmd": "101", "fields": {"a2": 1}},
            {"method": "set_nothing", "cmd": None, "fields": {}},
        ],
    }

    check = cross_check(Product.A1783, "C2000G2", facts, layout)

    links = {link.method: link for link in check.links}
    assert links["turn_ac_on"].verdict == "position"
    assert links["turn_ac_on"].properties == ("ac_output",)
    assert links["set_nothing"].verdict == "none"
    assert check.absent == ["ac_charging_power ff[:] (stream)"]
