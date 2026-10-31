# Copyright (c) 2026 Shawn Stricker
"""Live-value rigs: hand-written tables in the built shape, and a stream reader."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from anker_ble_emulator import A91B2, A2345, ManualClock
from anker_ble_emulator.devices.base import Profile, data_resource
from anker_ble_emulator.devices.prime import MSGTYPE_303
from anker_ble_emulator.live import LiveTable, LiveValues
from tests.fixtures.prime import advance, command


if TYPE_CHECKING:
    from anker_ble_emulator import EmulatedDevice
    from anker_ble_emulator.layouts import Value
    from tests.fixtures.client import AppClient

PORT_FIELDS = ("output_status", "output_voltage", "output_current", "output_power")


def _port(name: str, samples: list[list[Any]]) -> dict[str, Any]:
    fields = [f"{name}_{field}" for field in PORT_FIELDS]
    return {
        "name": name,
        "fields": fields,
        "live": fields[1:],
        "idle": [0, 20, 0],
        "samples": samples,
    }


#: A Prime table: pd1 and pd2 on ``0303``, the snapshot's pd1 after it.
PRIME_TABLE: dict[str, Any] = {
    "device": "A2345",
    "sections": [
        {
            "msgtype": 0x303,
            "groups": [
                _port(
                    "pd1",
                    [
                        [[1, 5000, 100, 49], 3],
                        [[1, 19760, 1000, 1970], 1],
                        [[1, 0, 20, 0], 4],
                        [[0, 0, 20, 0], 5],
                    ],
                ),
                _port("pd2", [[[1, 5008, 200, 100], 2], [[1, 9000, 300, 270], 1]]),
            ],
            "active_sets": [[[], 1], [["pd1"], 3], [["pd1", "pd2"], 2]],
        },
        {
            "msgtype": 0xA00,
            "groups": [_port("pd1", [[[1, 4992, 99, 48], 1]])],
            "active_sets": [[["pd1"], 1]],
        },
    ],
}

#: A flow table: an input and two outputs, their total, a pack current paired
#: with the total, a slow battery level and the remaining time.
FLOW_TABLE: dict[str, Any] = {
    "device": "test",
    "sections": [
        {
            "msgtype": 0x421,
            "groups": [
                {
                    "name": "ac_in",
                    "fields": ["ac_input_switch", "ac_input_power"],
                    "live": ["ac_input_power"],
                    "idle": [0],
                    "samples": [[[1, 300], 2], [[1, 0], 1]],
                },
                {
                    "name": "ac_out",
                    "fields": ["ac_output_power"],
                    "live": ["ac_output_power"],
                    "idle": [0],
                    "samples": [[[100], 1], [[250], 1]],
                },
                {
                    "name": "usbc",
                    "fields": ["usbc_power"],
                    "live": ["usbc_power"],
                    "idle": [0],
                    "samples": [[[30], 1], [[65], 1]],
                },
            ],
            "active_sets": [
                [[], 1],
                [["ac_in"], 1],
                [["ac_out"], 2],
                [["ac_out", "usbc"], 2],
                [["ac_in", "ac_out", "usbc"], 1],
            ],
            "totals": [
                {
                    "field": "total_output_power",
                    "sum_of": ["ac_output_power", "usbc_power"],
                }
            ],
            "paired": [
                {
                    "given": ["total_output_power"],
                    "fields": ["pack_current"],
                    "samples": [
                        [[[100], [-20]], 3],
                        [[[130], [-26]], 1],
                        [[[0], [0]], 2],
                    ],
                    "unpaired": 1,
                }
            ],
            "slow": [
                {
                    "field": "battery_level",
                    "interval": 1,
                    "steps": [[-1, 1], [0, 6], [1, 1]],
                    "values": [[40, 2], [41, 5], [44, 1]],
                }
            ],
            "remaining_time": {
                "output_idle": [[0, 4]],
                "output_active": [[120, 3], [600, 1]],
            },
        },
        {
            "msgtype": 0x900,
            "groups": [
                {
                    "name": "ac_out",
                    "fields": ["ac_output_power"],
                    "live": ["ac_output_power"],
                    "idle": [0],
                    "samples": [[[999], 1]],
                }
            ],
            "active_sets": [[["ac_out"], 1]],
        },
    ],
}

#: A device survey as the frame survey writes it: one section with a group
#: matched on a field outside it, a unit's identity group, two totals (one that
#: didn't always hold), a counter, a slow field, a paired draw and the remaining
#: time; a section with nothing live.
SURVEY: dict[str, Any] = {
    "device": "A1783",
    "sections": [
        {
            "msgtype": "0421",
            "frames": 1000,
            "groups": [
                {
                    "name": "ac_out",
                    "tag": "a5",
                    "fields": ["ac_output_switch", "ac_output_power"],
                    "match": ["ac_input_status"],
                    "live": ["ac_output_power"],
                    "idle": [0],
                    "samples": [[[1, 100, 1], 7], [[1, 0, 1], 2]],
                    "dropped": 3,
                },
                {
                    "name": "usbc",
                    "tag": "a6",
                    "fields": ["usbc_power"],
                    "live": ["usbc_power"],
                    "idle": [0],
                    "samples": [[[30], 1]],
                    "dropped": 0,
                },
                {
                    "name": "pd1_device",
                    "tag": "bd",
                    "fields": ["pd1_device_vid"],
                    "live": ["pd1_device_vid"],
                    "idle": [0],
                    "samples": [[[1452], 1]],
                    "dropped": 0,
                },
            ],
            "active_sets": [
                [[], 4],
                [["ac_out"], 3],
                [["ac_out", "pd1_device"], 1],
                [["usbc", "pd1_device"], 2],
                [["usbc"], 1],
            ],
            "totals": [
                {
                    "field": "total_output_power",
                    "sum_of": ["ac_output_power", "usbc_power"],
                    "equal": 1000,
                    "differ": 0,
                    "max": 130,
                },
                {
                    "field": "total_input_power",
                    "sum_of": ["ac_input_power"],
                    "equal": 990,
                    "differ": 10,
                    "max": 300,
                },
            ],
            "slow": [
                {
                    "field": "pack_soc",
                    "steps": [[-1, 2], [0, 90], [1, 3]],
                    "values": [[400, 10], [450, 1]],
                    "interval": {"median": 540, "gaps": [[540, 90], [1080, 4]]},
                }
            ],
            "counters": [{"field": "charge_cycles", "values": [[12, 1000]]}],
            "remaining_time": {
                "output_idle": [[0, 5]],
                "output_active": [[120, 2]],
            },
            "paired": [
                {
                    "given": ["total_output_power"],
                    "fields": ["pack_current"],
                    "samples": [[[[100], [-20]], 3]],
                    "unpaired": 1,
                }
            ],
        },
        {"msgtype": 0x900, "frames": 4, "groups": [], "active_sets": [], "totals": []},
    ],
}

#: The state a flow draw starts from: AC input switched on, a pack at 41 %.
FLOW_STATE: dict[str, Value] = {
    "ac_input_switch": 1,
    "ac_input_power": 0,
    "ac_output_power": 0,
    "usbc_power": 0,
    "total_output_power": 0,
    "pack_current": 0,
    "battery_level": 41,
    "remaining_time": 0,
}


def live_values(table: dict[str, Any], seed: int, period: float = 1.0) -> LiveValues:
    """Return a seeded draw over ``table``."""
    rng = random.Random(seed)  # noqa: S311  # a replay of recordings, not a secret
    return LiveValues(LiveTable.from_json(table), period, rng)


def recorded(table: dict[str, Any], name: str) -> set[Value]:
    """Return every value ``table`` records for ``name``: samples, idle and splits."""
    values: set[Value] = set()
    for section in table["sections"]:
        for group in section["groups"]:
            if name in group["live"]:
                values.add(group["idle"][group["live"].index(name)])
                index = group["fields"].index(name)
                values.update(sample[index] for sample, _ in group["samples"])
        for block in section.get("paired", []):
            if name in block["fields"]:
                index = block["fields"].index(name)
                values.update(pair[1][index] for pair, _ in block["samples"])
        remaining = section.get("remaining_time")
        if name == "remaining_time" and remaining is not None:
            for split in remaining.values():
                values.update(value for value, _ in split)
    return values


def flow_draws(seed: int, count: int, **state: Value) -> list[dict[str, Value]]:
    """Return the state after each of ``count`` draws of the flow table, 1 s apart."""
    live, current = live_values(FLOW_TABLE, seed), {**FLOW_STATE, **state}
    draws = []
    for second in range(count):
        current |= live.draw(current, (), float(second))
        draws.append(dict(current))
    return draws


def prime_live(
    seed: int, table: dict[str, Any] = PRIME_TABLE
) -> tuple[A2345, ManualClock]:
    """Return an A2345 with pd1 and pd2 attached, drawing ``table`` with ``seed``."""
    clock = ManualClock()
    device = A2345(clock=clock)
    device.set_values(pd1_output_status=1, pd2_output_status=1)
    device.negotiated_module.live = live_values(table, seed)
    return device, clock


def recorded_live(
    product: type[A2345 | A91B2], seed: int
) -> tuple[A2345 | A91B2, ManualClock]:
    """Return a Prime as recorded, drawing its own table with ``seed``."""
    clock = ManualClock()
    device = product(clock=clock)
    device.vary_live_values(seed)
    return device, clock


def live_table(device: EmulatedDevice) -> dict[str, Any]:
    """Return the device's own live table, as built."""
    profile = device.profile
    if not isinstance(profile, Profile) or profile.live is None:
        msg = f"{device.pn} has no live table"
        raise TypeError(msg)
    return data_resource(profile.live.table)


def drawn_fields(table: dict[str, Any], msgtype: int) -> list[str]:
    """Return the live fields of ``table``'s ``msgtype`` section, in table order."""
    return [
        name
        for section in table["sections"]
        if section["msgtype"] == msgtype
        for group in section["groups"]
        for name in group["live"]
    ]


def read_303(device: EmulatedDevice, payload: bytes) -> dict[str, Value]:
    """Return a ``0303`` push's named values."""
    layout = device.layout
    if layout is None:
        msg = f"{device.pn} has no layout"
        raise TypeError(msg)
    return layout.read(MSGTYPE_303, payload)


def current_303(device: EmulatedDevice) -> dict[str, Value]:
    """Return the named values of the ``0303`` the device would push now, undrawn."""
    module = device.negotiated_module
    return read_303(device, module.mcu.push(MSGTYPE_303, values=module.values).payload)


def stream(
    device: EmulatedDevice, client: AppClient, clock: ManualClock, bursts: int
) -> list[dict[str, Value]]:
    """Run ``bursts`` ``420b`` bursts; return each ``0303`` push's named values."""
    frames: list[tuple[str, str]] = []
    for _ in range(bursts):
        frames += command(device, client, 0x20B, [])
        for _ in range(9):
            frames += advance(device, client, clock, 1)
    return [
        read_303(device, bytes.fromhex(clear)) for cmd, clear in frames if cmd == "4303"
    ]
