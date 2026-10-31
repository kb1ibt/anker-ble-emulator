# Copyright (c) 2026 Shawn Stricker
"""A1783 live-value rigs: a hand-written table in the built shape, and a poller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from anker_ble_emulator import A1783, ManualClock
from anker_ble_emulator.c_g2_490 import MSGTYPE_490
from anker_ble_emulator.devices.base import data_resource
from anker_ble_emulator.devices.solix_c_gen2 import MSGTYPE_421, MSGTYPE_900
from tests.fixtures.live import live_values

from .cgen2_fixtures import authorized_client, command


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator import EmulatedDevice
    from anker_ble_emulator.layouts import Value
    from tests.fixtures.client import AppClient


def _port(
    name: str, status: str, power: str, samples: list[list[Any]]
) -> dict[str, Any]:
    return {
        "name": name,
        "fields": [status, power],
        "live": [power],
        "idle": [0],
        "samples": samples,
    }


#: The output ports a ``0421``'s ``a6`` total sums.
OUTPUT_POWERS = ("ac_output_power", "pd1_output_power", "pd2_output_power")
#: The inputs the ``0490``'s ``.23.4`` total sums (the layout derives it).
INPUT_POWERS = ("ac_input_power", "pv1_input_power")

#: An A1783 table: AC in and out, PD1 and PD2 on ``0421``, the battery level and
#: the time left; the ``0490``'s pack flow, paired with the totals.
A1783_TABLE: dict[str, Any] = {
    "device": "A1783",
    "sections": [
        {
            "msgtype": 0x421,
            "groups": [
                {
                    "name": "ac_in",
                    "fields": [
                        "ac_input_disable_switch",
                        "ac_input_status",
                        "ac_input_power",
                    ],
                    "live": ["ac_input_power"],
                    "idle": [0],
                    "samples": [[[0, 2, 1210], 3], [[0, 2, 61], 1], [[0, 1, 1211], 1]],
                },
                _port(
                    "ac_out",
                    "ac_output_status",
                    "ac_output_power",
                    [[[1, 57], 2], [[1, 61], 1]],
                ),
                _port(
                    "pd1",
                    "pd1_output_status",
                    "pd1_output_power",
                    [[[1, 89], 3], [[1, 2], 1]],
                ),
                _port(
                    "pd2",
                    "pd2_output_status",
                    "pd2_output_power",
                    [[[1, 1], 4], [[1, 12], 1]],
                ),
            ],
            "active_sets": [
                [[], 4],
                [["pd2"], 3],
                [["pd1", "pd2"], 2],
                [["ac_out", "pd2"], 1],
                [["ac_in"], 1],
                [["ac_in", "pd2"], 1],
            ],
            "totals": [{"field": "total_output_power", "sum_of": list(OUTPUT_POWERS)}],
            "slow": [
                {
                    "field": "battery_level",
                    "interval": 1.0,
                    "steps": [[-1, 2], [0, 7], [1, 1]],
                    "values": [[90, 1], [95, 4], [100, 2]],
                }
            ],
            "remaining_time": {
                "output_idle": [[7110, 3], [3610, 1]],
                "output_active": [[2154, 2], [600, 1]],
            },
        },
        {
            "msgtype": MSGTYPE_490,
            "groups": [],
            "active_sets": [],
            "paired": [
                {
                    "given": [
                        "total_input_power",
                        "total_output_power",
                        "on_bus_pack_status",
                    ],
                    "fields": ["pack_current", "mcu_input_power", "mcu_output_power"],
                    "samples": [
                        [[[0, 0, 0], [0, 0, 0]], 5],
                        [[[0, 1, 0], [-2, 0, 1]], 3],
                        [[[0, 90, 0], [-185, 0, 90]], 2],
                        [[[1210, 0, 0], [2027, 1210, 0]], 2],
                    ],
                    "unpaired": 1,
                }
            ],
        },
    ],
}


def real_table() -> dict[str, Any]:
    """Return the A1783's packaged live table, as built from the frame survey."""
    return data_resource("a1783_live.json")


def a1783_real(seed: int, **values: Value) -> tuple[A1783, ManualClock, AppClient]:
    """Return an A1783 on an authorized link, drawing its packaged table with ``seed``.

    ``values`` are set (and so pinned) first.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, b"owner-token")
    if values:
        device.set_values(**values)
    device.vary_live_values(seed)
    return device, clock, client


def a1783_live(
    seed: int, table: dict[str, Any] = A1783_TABLE, **values: Value
) -> tuple[A1783, ManualClock, AppClient]:
    """Return an A1783 on an authorized link, drawing ``table`` with ``seed``.

    ``values`` are set (and so pinned) first. The first draw is made at once, so
    the periods fall on whole seconds from the start.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, b"owner-token")
    if values:
        device.set_values(**values)
    module = device.negotiated_module
    module.live = live_values(table, seed)
    module.refresh_live()
    module.check_pushes()
    return device, clock, client


def poll(device: EmulatedDevice, client: AppClient) -> dict[int, bytes]:
    """Send a ``4100`` poll; return its ``0900`` and ``0421`` cleartexts by msgtype."""
    replies = command(device, client, 0x100, [])
    return {
        reply.frame.cmd.msgtype: reply.plaintext
        for reply in replies
        if reply.frame.cmd.msgtype in {MSGTYPE_900, MSGTYPE_421}
    }


def run(
    device: EmulatedDevice,
    client: AppClient,
    clock: ManualClock,
    steps: tuple[float, ...],
) -> list[tuple[float, int, bytes]]:
    """Advance by each step, then poll; return every frame as (time, msgtype, clear).

    Each step's ``0490`` posts come before that step's poll.
    """
    module = device.negotiated_module
    frames: list[tuple[float, int, bytes]] = []
    for step in steps:
        clock.advance(step)
        for data in module.check_pushes().frames:
            reply = client.open(data)
            if reply is not None and reply.frame.cmd.msgtype == MSGTYPE_490:
                frames.append((clock.now(), MSGTYPE_490, reply.plaintext))
        frames += [
            (clock.now(), msgtype, clear)
            for msgtype, clear in poll(device, client).items()
        ]
    return frames


def named(device: EmulatedDevice, msgtype: int, clear: bytes) -> dict[str, Value]:
    """Return a ``0421``/``0900``/``0490`` cleartext's named values.

    Raises:
        TypeError: If the device has no layout or posts no ``0490``.

    """
    layout, push_490 = device.layout, device.mcu.push_490
    if layout is None or push_490 is None:
        msg = f"{device.pn} has no layout or no 0490"
        raise TypeError(msg)
    if msgtype == MSGTYPE_490:
        return push_490.read(clear)
    return layout.read(msgtype, clear)


#: Name parts that mark a ``0490`` counter, which a draw never touches; the
#: counter page turns on its own timer.
COUNTER_MARKS = ("cumulative_", "_posts", "_energy", "post_counter", "counter_page")


def counters(values: Mapping[str, Value]) -> dict[str, Value]:
    """Return the counters among a ``0490``'s named values."""
    return {
        name: value
        for name, value in values.items()
        if any(mark in name for mark in COUNTER_MARKS)
    }
