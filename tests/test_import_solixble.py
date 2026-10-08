# Copyright (c) 2026 Shawn Stricker
"""The SolixBLE class import: outers, telemetry, decode positions and commands."""

from pathlib import Path

from tests.fixtures.solixble import EXTRA, run_import, write_solixble
from tools.import_solixble import msgtype


WHOLE = {"source": "snapshot", "parse": "record", "begin": None, "end": None}


def test_a_plain_device_reads_its_status_properties_and_commands(
    tmp_path: Path,
) -> None:
    data = run_import(tmp_path / "out.json", str(write_solixble(tmp_path / "base")))
    station = data["classes"]["Station"]

    assert station["outer"] == "plain"
    assert station["telemetry"] == ["402", "300", "405"]
    assert station["status"] == ["040", "840"]
    assert station["properties"] == {
        "battery": [
            {"source": "stream", "parse": "int", "tag": "c1", "begin": 1, "end": None}
        ],
        "serial": [
            {"source": "stream", "parse": "string", "tag": "a2", "begin": 3, "end": 20}
        ],
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


def test_an_encrypted_device_reads_its_helpers_and_snapshot(tmp_path: Path) -> None:
    data = run_import(tmp_path / "out.json", str(write_solixble(tmp_path / "base")))
    charger = data["classes"]["Charger"]

    assert charger["outer"] == "encrypted"
    assert charger["telemetry"] == ["303"]
    assert charger["snapshot"] == ["a00"]
    assert charger["subscribe"] == "200"
    assert charger["status"] is None
    assert charger["properties"] == {
        "port_power": [
            {"source": "stream", "parse": "int", "tag": "a2", "begin": 2, "end": 4}
        ],
        "schedule": [WHOLE | {"tag": "aa"}],
        "timer": [WHOLE | {"tag": "ab"}],
        "version": [
            {"source": "snapshot", "parse": "int", "tag": "a2", "begin": 1, "end": 3}
        ],
    }
    assert set(data["classes"]) == {"Charger", "Station", "StationPlus"}
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
        "level": [
            {
                "source": "stream",
                "parse": "int",
                "tag": "a1",
                "begin": None,
                "end": None,
            }
        ]
    }


def test_a_command_s_message_type_drops_its_link_flags() -> None:
    assert msgtype("c840") == "840"
    assert msgtype("4100") == "100"
