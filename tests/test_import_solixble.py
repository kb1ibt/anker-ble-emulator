# Copyright (c) 2026 Shawn Stricker
"""The SolixBLE class import: outers, telemetry, decode positions and commands."""

from pathlib import Path

from tests.fixtures.solixble import (
    EXTRA,
    run_import,
    snapshot_read,
    stream_read,
    write_solixble,
)
from tools.import_solixble import msgtype


def test_a_plain_device_reads_its_status_properties_and_commands(
    tmp_path: Path,
) -> None:
    data = run_import(tmp_path / "out.json", str(write_solixble(tmp_path / "base")))
    station = data["classes"]["Station"]

    assert station["outer"] == "plain"
    assert station["telemetry"] == ["402", "300", "405"]
    assert station["status"] == ["040", "840"]
    assert station["properties"] == {
        "battery": [stream_read("int", 1, None, tag="c1")],
        "serial": [stream_read("string", 3, 20)],
        "temperature": [stream_read("int", 1, 3, tag="c2", signed=True)],
    }
    assert station["commands"] == [
        {
            "method": "get_status_update",
            "cmd": "040",
            "fields": {"a1": "21"},
            "arguments": {},
        },
        {"method": "raw", "cmd": None, "fields": {}, "arguments": {}},
        {
            "method": "turn_ac_on",
            "cmd": "04a",
            "fields": {
                "a1": "21",
                "a2": 1,
                "a3": 1,
                "a4": None,
                "a5": False,
                "a6": -1,
            },
            "arguments": {"on": True, "port": 2, "flag": 1},
        },
    ]


def test_a_class_s_own_outer_overrides_its_base_s(tmp_path: Path) -> None:
    data = run_import(tmp_path / "out.json", str(write_solixble(tmp_path / "base")))

    assert data["classes"]["StationGen2"]["outer"] == "encrypted"


def test_an_encrypted_device_reads_its_helpers_and_snapshot(tmp_path: Path) -> None:
    data = run_import(tmp_path / "out.json", str(write_solixble(tmp_path / "base")))
    charger = data["classes"]["Charger"]

    assert charger["outer"] == "encrypted"
    assert charger["telemetry"] == ["303"]
    assert charger["snapshot"] == ["a00"]
    assert charger["subscribe"] == "200"
    assert charger["status"] is None
    assert charger["properties"] == {
        "port_power": [stream_read("int", 2, 4)],
        "schedule": [snapshot_read("aa", None, None)],
        "timer": [snapshot_read("ab", None, None)],
        "version": [snapshot_read("a2", 1, 3)],
    }
    assert set(data["classes"]) == {"Charger", "Station", "StationGen2", "StationPlus"}
    assert [c["method"] for c in data["classes"]["StationPlus"]["commands"]] == [
        "get_status_update",
        "raw",
    ]


def test_a_later_checkout_only_adds_the_classes_the_first_lacks(
    tmp_path: Path,
) -> None:
    base = write_solixble(tmp_path / "base")
    extra = write_solixble(tmp_path / "extra", EXTRA)

    data = run_import(tmp_path / "out.json", str(base), f"{extra}=extra branch")

    assert data["sources"] == ["SolixBLE", "extra branch"]
    assert data["classes"]["Station"]["outer"] == "plain"
    assert data["classes"]["Legacy"]["properties"] == {
        "level": [stream_read("int", None, None, tag="a1")]
    }


def test_a_command_s_message_type_drops_its_link_flags() -> None:
    assert msgtype("c840") == "840"
    assert msgtype("4100") == "100"
