# Copyright (c) 2026 Shawn Stricker
"""A small anker-solix-api map, as a dict and as an importable package."""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from pathlib import Path

#: anker-solix-api's map keys, by the names the tool reads from ``mqttcmdmap``.
KEY_NAMES = {
    "NAME": "name",
    "TYPE": "type",
    "TOPIC": "topic",
    "BYTES": "bytes",
    "LENGTH": "length",
    "MASK": "mask",
    "OFFSET": "offset",
    "COMMAND_NAME": "command_name",
    "COMMAND_LIST": "command_list",
    "VALUE_MIN": "value_min",
    "VALUE_MAX": "value_max",
    "VALUE_STEP": "value_step",
    "VALUE_OPTIONS": "value_options",
    "VALUE_DEFAULT": "value_default",
}
#: ``mqttcmdmap`` as the tool sees it.
KEYS = ModuleType("mqttcmdmap")
KEYS.__dict__.update(KEY_NAMES)

#: One telemetry map shared by two products, as anker-solix-api reuses its maps.
TELEMETRY: dict[str, Any] = {
    "topic": "param_info",
    "a2": {"name": "battery_soc"},
    "a3": {"name": "ac_power"},
    "a4": {"name": "fixed_power", "type": b"\x02"},
    "a5": {"name": "never_recorded"},
    "a6": {"bytes": {"00": {"name": "mode", "type": b"\x01"}, "01": [{"name": "x"}]}},
    "a7": {
        "bytes": [
            {"name": "pack_sn", "type": b"\x00"},
            {"name": "pack_soc", "offset": 1},
        ]
    },
}

#: A sparse map of another message, named fields only.
SPARSE: dict[str, Any] = {
    "topic": "param_info",
    "b1": {"name": "t1"},
    "b2": {"name": "t2"},
    "b3": {"name": "t3"},
    "b4": {"name": "t4"},
    "b5": {"name": "t5"},
}

#: A message never recorded, sharing a field name with ``TELEMETRY``.
UNRECORDED: dict[str, Any] = {"topic": "param_info", "c1": {"name": "battery_soc"}}

SWITCH = {
    "command_name": "ac_output_switch",
    "a1": {"name": "pattern_22"},
    "a2": {
        "name": "set_ac_output_switch",
        "type": b"\x01",
        "value_options": {"off": 0, "on": 1},
    },
    "fe": {"name": "msg_timestamp", "type": b"\x03"},
}

GROUP = {
    "command_list": ["ac_charge_limit", "display_timeout"],
    "ac_charge_limit": {
        "a4": {
            "name": "set_limit",
            "type": b"\x02",
            "value_min": 100,
            "value_max": 1200,
            "value_step": 100,
        },
    },
    "display_timeout": {
        "a5": {
            "name": "set_timeout",
            "type": b"\x02",
            "value_options": [20, 30, 60],
            "value_default": 30,
        },
    },
}

#: ``SOLIXMQTTMAP``: two products share the telemetry map; one has a sparse map.
MAPS: dict[str, dict[str, Any]] = {
    "A0001": {"0405": TELEMETRY, "0407": UNRECORDED, "004a": SWITCH, "0101": GROUP},
    "A0002": {"0405": TELEMETRY, "0406": SPARSE},
}

_KEYS_SOURCE = "".join(f"{name} = {value!r}\n" for name, value in KEY_NAMES.items())
_MAP_SOURCE = (
    f"TELEMETRY = {TELEMETRY!r}\nSPARSE = {SPARSE!r}\nSWITCH = {SWITCH!r}\n"
    f"GROUP = {GROUP!r}\nUNRECORDED = {UNRECORDED!r}\n"
    "SOLIXMQTTMAP = {'A0001': {'0405': TELEMETRY, '0407': UNRECORDED,"
    " '004a': SWITCH, '0101': GROUP}, 'A0002': {'0405': TELEMETRY, '0406': SPARSE}}\n"
)


#: A layout as ``tools/import_maps.py`` writes it.
LAYOUT: dict[str, Any] = {
    "pn": "A0001",
    "source": "anker-solix-api test",
    "messages": {
        "0405": {
            "topic": "param_info",
            "fields": [
                {"tag": "a1", "name": None},
                {"tag": "a2", "name": "soc", "type": "01", "length": 2},
                {"tag": "a3", "name": "power", "type": "02", "length": 3},
                {"tag": "a4", "name": "untyped"},
                {"tag": "a5", "name": "serial", "type": "00", "length": 5},
                {"tag": "a6", "name": "energy", "type": "03", "length": 5},
                {"tag": "a7", "name": "ratio", "type": "05", "length": 5},
                {
                    "tag": "a8",
                    "name": None,
                    "type": "04",
                    "length": 4,
                    "bytes": [
                        {"name": "mode", "type": "01", "offset": 0},
                        {"name": "limit", "type": "02", "offset": 1},
                        {"name": "unnamed_part", "offset": 2},
                    ],
                },
            ],
        },
    },
    "commands": {
        "004a": [
            {
                "command": "ac_output_switch",
                "fields": {
                    "a1": {"name": "pattern_22"},
                    "a2": {"name": "set_switch", "type": "01", "options": [0, 1]},
                    "fe": {"name": "msg_timestamp", "type": "03"},
                },
            }
        ],
        "0101": [
            {
                "command": "limit",
                "fields": {
                    "a4": {
                        "name": "l",
                        "type": "02",
                        "min": 100,
                        "max": 1200,
                        "step": 100,
                    }
                },
            },
            {"command": "name", "fields": {"a5": {"name": "n", "type": "00"}}},
        ],
    },
}


def write_solix_api(root: Path) -> Path:
    """Write an importable ``anker_solix_api`` holding ``MAPS``; return its src dir."""
    package = root / "src" / "anker_solix_api"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "mqttcmdmap.py").write_text(_KEYS_SOURCE)
    (package / "mqttmap.py").write_text(_MAP_SOURCE)
    return root / "src"
