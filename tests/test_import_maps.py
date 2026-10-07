# Copyright (c) 2026 Shawn Stricker
"""The map import tool: learning field types from recordings, writing layouts."""

import json
from pathlib import Path

from tests.fixtures.logs import mqtt_frame, write_frame_log, write_mqtt_records
from tests.fixtures.maps import KEYS, MAPS, SPARSE, TELEMETRY, write_solix_api
from tools.import_maps import (
    Recorded,
    command_layout,
    field_layout,
    fields_of,
    learn,
    main,
    match_map,
    product_layout,
    recorded_frames,
)


#: A recorded ``0405``: ``a2`` 1-byte value, ``a3`` 2-byte, ``a6`` 3 bytes, a trailer.
TELEMETRY_FRAME = bytes.fromhex("a10131a2020164a303020a00a603040101fe050300000000")
#: The sparse message's five tags plus nine more the map doesn't name.
SPARSE_FRAME = bytes.fromhex(
    "b1020101b2020101b3020101b4020101b5020101"
    + "".join(f"{tag:02x}020100" for tag in range(0xC1, 0xCA))
)


def test_fields_walk_a_push_and_a_reply() -> None:
    assert fields_of(bytes.fromhex("a10131a2020164")) == [
        ("a1", b"\x31"),
        ("a2", b"\x01\x64"),
    ]
    assert fields_of(bytes.fromhex("00a10131")) == [("a1", b"\x31")]


def test_fields_that_dont_walk_to_the_end_are_none() -> None:
    assert fields_of(bytes.fromhex("a10131a205")) == []
    assert fields_of(bytes.fromhex("a1013130")) == []


def test_a_frame_is_matched_by_msgtype_then_by_its_tags() -> None:
    telemetry = {"0405": TELEMETRY}
    sparse = {"0406": SPARSE}
    tags = {f"{tag:02x}" for tag in range(0xB1, 0xB6)} | {"c1", "c2", "c3"}

    assert match_map(telemetry, Recorded("A0001", 0x405, b""), set()) is TELEMETRY
    assert match_map(sparse, Recorded("A0002", None, b""), tags) is SPARSE
    assert match_map(sparse, Recorded(None, None, b""), tags) is None
    assert match_map(sparse, Recorded("A0002", None, b""), {"b1", "b2"}) is None


def test_learned_types_follow_the_shared_map_and_the_field_name() -> None:
    frames = [
        Recorded("A0001", 0x405, TELEMETRY_FRAME),
        Recorded("A0001", 0x405, TELEMETRY_FRAME),
        Recorded("A0002", None, SPARSE_FRAME),
        Recorded(None, 0x999, TELEMETRY_FRAME),
    ]

    learned = learn(frames, MAPS, KEYS)
    a0002 = product_layout(MAPS["A0002"], learned, KEYS)
    a0001 = product_layout(MAPS["A0001"], learned, KEYS)

    assert learned.frames == 2
    fields = {item["tag"]: item for item in a0002["messages"]["0405"]["fields"]}
    assert fields["a2"] | {} == {
        "tag": "a2",
        "name": "battery_soc",
        "type": "01",
        "length": 2,
        "from": "recorded",
    }
    assert fields["a3"]["length"] == 3
    assert fields["a4"] == {
        "tag": "a4",
        "name": "fixed_power",
        "type": "02",
        "length": 3,
        "from": "map",
    }
    assert "type" not in fields["a5"]
    assert a0002["messages"]["0406"]["fields"][0]["from"] == "recorded"
    assert a0001["messages"]["0407"]["fields"][0] == {
        "tag": "c1",
        "name": "battery_soc",
        "type": "01",
        "length": 2,
        "from": "name",
    }


def test_sub_fields_keep_their_offsets_and_sequences() -> None:
    learned = learn([], MAPS, KEYS)

    mode = field_layout(TELEMETRY, "a6", learned, KEYS)
    pack = field_layout(TELEMETRY, "a7", learned, KEYS)

    assert mode["bytes"] == [
        {"name": "mode", "type": "01", "offset": 0},
        {"name": "x", "offset": 1},
    ]
    assert pack["sequence"] == [
        {"name": "pack_sn", "type": "00"},
        {"name": "pack_soc", "skip": 1},
    ]


def test_commands_keep_their_types_and_accepted_values() -> None:
    layout = product_layout(MAPS["A0001"], learn([], MAPS, KEYS), KEYS)

    assert layout["commands"]["004a"] == [
        {
            "command": "ac_output_switch",
            "fields": {
                "a1": {"name": "pattern_22"},
                "a2": {"name": "set_ac_output_switch", "type": "01", "options": [0, 1]},
                "fe": {"name": "msg_timestamp", "type": "03"},
            },
        }
    ]
    limit, timeout = layout["commands"]["0101"]
    assert limit["command"] == "ac_charge_limit"
    assert limit["fields"]["a4"] == {
        "name": "set_limit",
        "type": "02",
        "min": 100,
        "max": 1200,
        "step": 100,
    }
    assert timeout["fields"]["a5"]["options"] == [20, 30, 60]
    assert timeout["fields"]["a5"]["default"] == 30


def test_a_command_field_with_a_non_value_option_is_left_bare() -> None:
    spec = {"command_name": "x", "a2": {"name": "y", "value_options": "computed"}}

    assert command_layout(spec, KEYS)["fields"]["a2"] == {"name": "y"}


def test_frames_are_read_from_logs_records_and_lists(tmp_path: Path) -> None:
    log = write_frame_log(tmp_path / "c.log", [("c421", b"\xa1\x01\x31")])
    records = write_mqtt_records(
        tmp_path / "m.ndjson", [mqtt_frame(0x421, "a10134"), mqtt_frame(0x900, "00")]
    )
    listed = tmp_path / "frames.tsv"
    listed.write_text("A0002\tble\t\ta10131\nA0001\tdt\t405\ta10131\n")
    broken = tmp_path / "broken.txt"
    broken.write_text('{"data":"/wkOAAMBDwkDAKEBMm4="}\n')

    frames = list(recorded_frames([log, records, listed, broken]))

    assert [(frame.pn, frame.msgtype) for frame in frames] == [
        (None, 0x421),
        (None, 0x421),
        (None, 0x900),
        ("A0002", None),
        ("A0001", 0x405),
    ]
    assert frames[1].payload == bytes.fromhex("a10134")


def test_main_writes_one_layout_per_product(tmp_path: Path) -> None:
    src = write_solix_api(tmp_path / "solix")
    frames = tmp_path / "frames.tsv"
    frames.write_text(f"A0001\tdt\t405\t{TELEMETRY_FRAME.hex()}\n")
    output = tmp_path / "maps"

    status = main(["--solix-api", str(src), "--output", str(output), str(frames)])

    assert status == 0
    assert sorted(path.name for path in output.iterdir()) == [
        "a0001.json",
        "a0002.json",
    ]
    layout = json.loads((output / "a0002.json").read_text())
    assert layout["pn"] == "A0002"
    assert layout["source"] == "anker-solix-api"
    assert layout["messages"]["0405"]["fields"][0]["from"] == "recorded"
