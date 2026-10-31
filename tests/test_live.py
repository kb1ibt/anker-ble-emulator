# Copyright (c) 2026 Shawn Stricker
"""Live values: draws from the recorded frequencies, once per period."""

from __future__ import annotations

import itertools
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from anker_ble_emulator import A91B2, A1722, A1780, A1783, A2345, ManualClock
from anker_ble_emulator.devices.base import Profile
from anker_ble_emulator.devices.prime import MSGTYPE_303
from anker_ble_emulator.layouts import LayoutError
from anker_ble_emulator.live import LiveSource, LiveTable
from anker_ble_emulator.mcu import GIVEN, McuScript
from anker_ble_emulator.parameters import ScalarFieldLayout, TableLayout
from anker_ble_emulator.timed import Step, Timed
from tests.fixtures.live import (
    FLOW_STATE,
    FLOW_TABLE,
    PRIME_TABLE,
    current_303,
    drawn_fields,
    flow_draws,
    live_table,
    live_values,
    prime_live,
    read_303,
    recorded,
    recorded_live,
    stream,
)
from tests.fixtures.prime import command, prime_client


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator.layouts import Value
    from anker_ble_emulator.module import Output


TOKEN = b"owner-token"
PRIMES = [A2345, A91B2]
FLOW_LIVE = (
    "ac_input_power",
    "ac_output_power",
    "usbc_power",
    "pack_current",
    "remaining_time",
)
PRIME_LIVE = tuple(
    f"{port}_{field}"
    for port in ("pd1", "pd2")
    for field in ("output_voltage", "output_current", "output_power")
)


def test_every_drawn_value_is_one_the_table_records() -> None:
    draws = flow_draws(1, 400)

    for name in FLOW_LIVE:
        assert {draw[name] for draw in draws} <= recorded(FLOW_TABLE, name)
    assert {draw["ac_output_power"] for draw in draws} == {0, 100, 250}


def test_totals_hold_in_every_draw() -> None:
    draws = flow_draws(2, 400)

    assert all(
        d["total_output_power"] == int(d["ac_output_power"]) + int(d["usbc_power"])
        for d in draws
    )
    assert {d["total_output_power"] for d in draws} >= {0, 100, 130, 250, 315}


def test_a_group_matches_on_its_match_fields_too() -> None:
    group = {
        "name": "ac_in",
        "fields": ["ac_input_power"],
        "match": ["ac_input_switch", "ac_input_status"],
        "live": ["ac_input_power"],
        "idle": [0],
        "samples": [[[300, 1, 1], 1]],
    }
    table = {
        "sections": [{"msgtype": 0, "groups": [group], "active_sets": [[["ac_in"], 1]]}]
    }
    live = live_values(table, 15)
    held = {**FLOW_STATE, "ac_input_status": 2}

    drawn = live.draw({**FLOW_STATE, "ac_input_status": 1}, (), 1)

    assert drawn == {"ac_input_power": 300}
    assert live.draw(held, (), 1) == {"ac_input_power": 0}
    assert live.draw(FLOW_STATE, (), 1) == {"ac_input_power": 0}


def test_a_later_section_reads_what_the_layout_derives_from_an_earlier_draw() -> None:
    group = {
        "name": "a",
        "fields": ["a"],
        "live": ["a"],
        "idle": [0],
        "samples": [[[5], 1]],
    }
    paired = {
        "given": ["a_total"],
        "fields": ["p"],
        "samples": [[[[10], [7]], 1]],
        "unpaired": 0,
    }
    table = {
        "sections": [
            {"msgtype": 1, "groups": [group], "active_sets": [[["a"], 1]]},
            {"msgtype": 2, "groups": [], "active_sets": [], "paired": [paired]},
        ]
    }
    state: dict[str, Value] = {"a": 0, "a_total": 0, "p": 0}

    def doubled(current: Mapping[str, Value]) -> dict[str, Value]:
        return {"a_total": int(current["a"]) * 2}

    with_derive = live_values(table, 16).draw(state, (), 1, doubled)
    without = live_values(table, 16).draw(state, (), 1)

    assert with_derive == {"a": 5, "p": 7}
    assert without == {"a": 5}


@pytest.mark.parametrize("layout", [TableLayout(), ScalarFieldLayout()])
def test_the_base_layouts_derive_nothing(
    layout: TableLayout | ScalarFieldLayout,
) -> None:
    assert (layout.derive(FLOW_STATE), layout.derived) == ({}, frozenset())


def test_a_later_section_leaves_a_field_an_earlier_one_drew() -> None:
    draws = flow_draws(3, 200)

    assert 999 not in {draw["ac_output_power"] for draw in draws}


def test_a_switched_off_group_stays_idle() -> None:
    draws = flow_draws(4, 200, ac_input_switch=0)

    assert {draw["ac_input_power"] for draw in draws} == {0}


def test_a_pinned_value_never_changes() -> None:
    live = live_values(FLOW_TABLE, 5)
    state = {**FLOW_STATE, "ac_output_power": 77}

    draws = [live.draw(state, {"ac_output_power"}, 1) for _ in range(200)]

    assert all("ac_output_power" not in draw for draw in draws)
    assert {draw["total_output_power"] for draw in draws} == {77, 107, 142}
    assert "total_output_power" not in live.draw(state, {"total_output_power"}, 1)


def test_the_paired_field_follows_its_given_total() -> None:
    draws = flow_draws(6, 400)

    pairs = {(d["total_output_power"], d["pack_current"]) for d in draws}
    moved = {
        (now["total_output_power"], now["pack_current"])
        for then, now in itertools.pairwise(draws)
        if now["pack_current"] != then["pack_current"]
    }
    assert {(100, -20), (130, -26), (0, 0)} <= pairs
    assert moved <= {(100, -20), (130, -26), (0, 0)}
    assert any(total not in {0, 100, 130} for total, _ in pairs)


def test_a_slow_field_stays_within_its_recorded_range() -> None:
    live = live_values(FLOW_TABLE, 7)
    live.draw(FLOW_STATE, (), 0.0)

    levels = [
        int(live.draw(FLOW_STATE, (), 30.0 * k)["battery_level"]) for k in range(1, 51)
    ]

    assert set(levels) <= set(range(40, 45))
    assert len(set(levels)) > 1
    assert "battery_level" not in live.draw(FLOW_STATE, (), 1500.5)


def test_a_slow_field_steps_once_an_interval_whatever_the_draw_period() -> None:
    """Steps 540 s apart: 13 in 2 h of 1 s draws, none in 539 s, the rest carried."""
    slow = {
        "field": "pack_soc",
        "interval": 540,
        "steps": [[1, 1]],
        "values": [[0, 1], [10000, 1]],
    }
    table = {
        "sections": [{"msgtype": 0, "groups": [], "active_sets": [], "slow": [slow]}]
    }
    live = live_values(table, 17)
    state: dict[str, Value] = {"pack_soc": 0}

    for second in range(7201):
        state |= live.draw(state, (), float(second))
    short = live_values(table, 17)
    short_steps = [short.draw({"pack_soc": 0}, (), float(s)) for s in range(540)]

    assert state["pack_soc"] == 13  # 7200 // 540
    assert all(drawn == {} for drawn in short_steps)
    assert short.draw({"pack_soc": 0}, (), 540.0) == {"pack_soc": 1}
    assert short.draw({"pack_soc": 5}, {"pack_soc"}, 1080.0) == {}


def test_the_remaining_time_follows_the_output_split() -> None:
    draws = flow_draws(8, 300)

    idle = {d["remaining_time"] for d in draws if d["total_output_power"] == 0}
    busy = {d["remaining_time"] for d in draws if d["total_output_power"] != 0}
    assert (idle, busy) == ({0}, {120, 600})


def test_an_empty_split_or_no_active_sets_leaves_the_fields() -> None:
    table = {
        "sections": [
            {
                "msgtype": 0x421,
                "groups": FLOW_TABLE["sections"][0]["groups"][:1],
                "active_sets": [],
                "remaining_time": {"output_idle": [], "output_active": [[1, 1]]},
            }
        ]
    }

    drawn = live_values(table, 9).draw(FLOW_STATE, (), 1)

    assert drawn == {"ac_input_power": 0}


def test_the_same_seed_draws_the_same_values() -> None:
    assert flow_draws(10, 100) == flow_draws(10, 100)
    assert flow_draws(10, 100) != flow_draws(11, 100)


def test_draws_fall_due_on_the_period_grid() -> None:
    live = live_values(FLOW_TABLE, 12, period=1.0)

    assert [live.due(t) for t in (5.0, 5.5, 6.2, 6.9, 8.0)] == [0, None, 1, None, 2]


def test_a_float_sum_just_short_of_a_mark_reaches_it() -> None:
    live = live_values(FLOW_TABLE, 14, period=0.1)
    live.due(0.0)

    assert 0.7 / 0.1 < 7  # the float shortfall under test
    assert live.due(0.7) == 7  # seven periods of 0.1 s


def test_a_bytes_value_in_a_counted_field_is_refused() -> None:
    live = live_values(FLOW_TABLE, 13)
    live.draw(FLOW_STATE, (), 0.0)

    with pytest.raises(TypeError, match="battery_level"):
        live.draw({**FLOW_STATE, "battery_level": b"\x29"}, (), 1.0)


def test_the_table_names_every_field_a_draw_sets() -> None:
    names = LiveTable.from_json(FLOW_TABLE).names

    assert names == {
        *FLOW_LIVE,
        "ac_input_switch",
        "total_output_power",
        "battery_level",
    }


def test_the_stream_carries_only_recorded_values() -> None:
    device, clock = prime_live(20)
    client = prime_client(device, TOKEN)

    frames = stream(device, client, clock, 5)

    assert len(frames) == 50
    for name in PRIME_LIVE:
        assert {frame[name] for frame in frames} <= recorded(PRIME_TABLE, name)
    assert len({frame["pd1_output_power"] for frame in frames}) > 1


def test_the_same_seed_and_clock_steps_give_the_same_stream() -> None:
    runs = []
    for seed in (21, 21, 22):
        device, clock = prime_live(seed)
        runs.append(stream(device, prime_client(device, TOKEN), clock, 3))

    assert runs[0] == runs[1]
    assert runs[0] != runs[2]


def test_frames_built_within_one_period_share_the_draw() -> None:
    device, clock = prime_live(23)
    client = prime_client(device, TOKEN)
    pairs = []

    for _ in range(20):
        clock.advance(1)
        burst = command(device, client, 0x20B, [])
        answered = command(device, client, 0x200, [])
        pairs.append(
            (read_303(device, bytes.fromhex(burst[1][1])), current_303(device))
        )
        assert [cmd for cmd, _ in answered] == ["4a00"]

    assert all(first == later for first, later in pairs)
    assert len({first["pd1_output_power"] for first, _ in pairs}) > 1


def test_a_value_set_on_the_unit_is_pinned() -> None:
    device, clock = prime_live(24)
    client = prime_client(device, TOKEN)
    device.set_values(pd1_output_power=1234)

    frames = stream(device, client, clock, 3)

    assert {frame["pd1_output_power"] for frame in frames} == {1234}
    assert len({frame["pd1_output_voltage"] for frame in frames}) == 1
    assert len({frame["pd2_output_power"] for frame in frames}) > 1


@pytest.mark.parametrize("product", PRIMES)
def test_a_port_with_one_field_pinned_keeps_its_other_fields(
    product: type[A2345 | A91B2],
) -> None:
    """The pinned port isn't drawn, so no frame mixes it with another sample."""
    device, clock = recorded_live(product, 46)
    before = current_303(device)
    device.set_values(pd2_output_power=1234)

    frames = stream(device, prime_client(device, TOKEN), clock, 10)

    assert len(frames) == 100
    for name in ("pd2_output_voltage", "pd2_output_current", "pd2_output_status"):
        assert {frame[name] for frame in frames} == {before[name]}, name
    assert {frame["pd2_output_power"] for frame in frames} == {1234}


def test_a_port_switched_off_stays_idle() -> None:
    device, clock = prime_live(25)
    client = prime_client(device, TOKEN)
    device.set_values(pd2_output_status=0)

    frames = stream(device, client, clock, 3)

    assert {frame["pd2_output_power"] for frame in frames} == {0}
    assert {frame["pd2_output_current"] for frame in frames} == {20}


def test_a_port_unplugged_and_plugged_again_draws_again() -> None:
    device, clock = prime_live(30)
    client = prime_client(device, TOKEN)

    device.unplug(1)
    unplugged = stream(device, client, clock, 5)
    device.plug(1, 0x05AC, 0x7800)
    plugged = stream(device, client, clock, 3)

    assert len(unplugged) == 50
    assert {frame["pd1_output_power"] for frame in unplugged} == {0}
    assert {frame["pd1_output_current"] for frame in unplugged} == {20}
    assert len({frame["pd1_output_power"] for frame in plugged}) > 1


def test_off_by_default_the_stream_is_the_recorded_one() -> None:
    clock = ManualClock()
    device = A2345(clock=clock)
    recorded_push = current_303(device)
    client = prime_client(device, TOKEN)

    frames = stream(device, client, clock, 2)

    assert device.negotiated_module.live is None
    assert frames == [recorded_push] * 20


def test_turning_it_off_stops_the_draws() -> None:
    device, clock = prime_live(26)
    client = prime_client(device, TOKEN)
    stream(device, client, clock, 1)
    device.vary_live_values(None)

    frames = stream(device, client, clock, 2)

    assert device.negotiated_module.live is None
    assert all(frame == frames[0] for frame in frames)


def test_a_push_on_demand_draws_first() -> None:
    device, clock = prime_live(27)
    prime_client(device, TOKEN)
    module = device.negotiated_module
    pushed: list[Output] = []
    device.listen(pushed.append)

    device.push(MSGTYPE_303)
    first = current_303(device)
    clock.advance(1)
    device.push(MSGTYPE_303)

    assert [len(out.frames) for out in pushed] == [1, 1]
    assert module.live is not None
    assert module.live.due(clock.now()) is None
    assert first["pd1_output_power"] in recorded(PRIME_TABLE, "pd1_output_power")


def test_a_device_with_no_recorded_live_values_refuses_them() -> None:
    with pytest.raises(LayoutError, match="A1722"):
        A1722().vary_live_values(1)


def test_turning_it_off_where_there_is_none_does_nothing() -> None:
    legacy, device = A1780(), A1722()

    legacy.vary_live_values(None)
    device.vary_live_values(None)

    assert device.negotiated_module.live is None


def test_an_mqtt_frame_on_the_cloud_draws_first() -> None:
    device = A1783()
    device.set_cloud(True)
    module = device.negotiated_module
    module.live = live_values(FLOW_TABLE, 28)

    device.mqtt_frame(0x421)

    assert module.values["total_output_power"] == (
        int(module.values["ac_output_power"]) + int(module.values["usbc_power"])
    )


def test_a_draw_runs_what_it_starts_before_the_event_it_came_due_for() -> None:
    """A draw's zero-delay event runs first; one with the due event's key replaces."""

    def mark(name: str, delay: float, key: str) -> Timed:
        def action(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
            order = state.get("_order", b"")
            if not isinstance(order, bytes):
                raise TypeError(order)
            return Step(values={"_order": order + name.encode()})

        return Timed(delay, key, action)

    class Following(TableLayout):
        def started(self) -> tuple[Timed, ...]:  # a layout hook
            return (mark("t", 1.0, "tick"),)

        def timed(
            self, before: Mapping[str, Value], after: Mapping[str, Value]
        ) -> tuple[Timed, ...]:  # a layout hook
            if before.get("_x") == after.get("_x"):
                return ()
            if after["_x"] == 1:
                return (mark("f", 0.0, "follow"),)
            return (mark("r", 0.0, "tick"),)

    orders = []
    for drawn in (1, 2):
        group: dict[str, Any] = {
            "name": "x",
            "fields": ["_x"],
            "live": ["_x"],
            "idle": [0],
            "samples": [[[drawn], 1]],
        }
        table = {
            "sections": [{"msgtype": 0, "groups": [group], "active_sets": [[["x"], 1]]}]
        }
        clock = ManualClock()
        device = A2345(clock=clock)
        device.use_mcu(McuScript(layout=Following()))
        module = device.negotiated_module
        module.live = live_values(table, 29)
        clock.advance(1.0)
        module.check_pushes()
        orders.append(module.values["_order"])

    assert orders == [b"ft", b"r"]


def test_the_timed_hook_finds_the_names_a_change_set() -> None:
    """``GIVEN`` holds what a unit event or a draw set, not what it implied."""
    seen: list[Value | None] = []

    class Watching(TableLayout):
        def timed(
            self, before: Mapping[str, Value], after: Mapping[str, Value]
        ) -> tuple[Timed, ...]:  # a layout hook
            seen.append(after.get(GIVEN))
            return ()

    group: dict[str, Any] = {
        "name": "x",
        "fields": ["_x"],
        "live": ["_x"],
        "idle": [0],
        "samples": [[[1], 1]],
    }
    table = {
        "sections": [{"msgtype": 0, "groups": [group], "active_sets": [[["x"], 1]]}]
    }
    device = A2345(clock=ManualClock())
    device.use_mcu(McuScript(layout=Watching()))

    device.unit_event("tap", _b=1, _a=2)
    device.negotiated_module.live = live_values(table, 31)
    device.negotiated_module.refresh_live()

    assert seen == [b"_a\n_b", b"_x"]


@pytest.mark.parametrize("product", PRIMES)
def test_a_prime_draws_only_values_its_table_records(
    product: type[A2345 | A91B2],
) -> None:
    device, clock = recorded_live(product, 40)
    table = live_table(device)

    frames = stream(device, prime_client(device, TOKEN), clock, 10)

    assert len(frames) == 100
    names = drawn_fields(table, MSGTYPE_303)
    for name in names:
        assert {frame[name] for frame in frames} <= recorded(table, name), name
    assert sum(len({frame[name] for frame in frames}) > 1 for name in names) >= 3


@pytest.mark.parametrize("product", PRIMES)
def test_a_prime_with_the_same_seed_streams_the_same(
    product: type[A2345 | A91B2],
) -> None:
    runs = []
    for seed in (41, 41, 42):
        device, clock = recorded_live(product, seed)
        runs.append(stream(device, prime_client(device, TOKEN), clock, 3))

    assert runs[0] == runs[1]
    assert runs[0] != runs[2]


@pytest.mark.parametrize("product", PRIMES)
def test_a_prime_keeps_a_pinned_value_and_draws_a_port_by_its_status(
    product: type[A2345 | A91B2],
) -> None:
    """A port with status 0 draws only what was recorded with status 0, or idles."""
    device, clock = recorded_live(product, 43)
    group = next(
        group
        for section in live_table(device)["sections"]
        if section["msgtype"] == MSGTYPE_303
        for group in section["groups"]
        if group["name"] == "pd2"
    )
    at_status_0 = {tuple(s[1:]) for s, _ in group["samples"] if s[0] == 0}
    device.set_values(pd2_output_status=0, pd4_output_power=1234)
    clock.advance(1)

    frames = stream(device, prime_client(device, TOKEN), clock, 5)

    pd2 = {
        tuple(frame[f"pd2_output_{f}"] for f in ("voltage", "current", "power"))
        for frame in frames
    }
    assert pd2 <= at_status_0 | {tuple(group["idle"])}
    assert {frame["pd4_output_power"] for frame in frames} == {1234}


@pytest.mark.parametrize("product", PRIMES)
def test_turning_it_on_draws_at_once(product: type[A2345 | A91B2]) -> None:
    device, clock = recorded_live(product, 44)
    module = device.negotiated_module

    assert module.live is not None
    assert module.live.due(clock.now()) is None
    assert module.live.due(clock.now() + 1) == 1


@pytest.mark.parametrize(("product", "select"), [(A2345, 1), (A91B2, 3)])
def test_a_port_switched_off_idles_until_switched_on_again(
    product: type[A2345 | A91B2], select: int
) -> None:
    """``4207`` off: the relay match fails, so pd2 idles; on again, it draws."""
    device, clock = recorded_live(product, 45)
    device.set_values(pd2_output_status=1)
    client = prime_client(device, TOKEN)
    idle = next(
        group["idle"]
        for section in live_table(device)["sections"]
        for group in section["groups"]
        if group["name"] == "pd2"
    )
    port = (0xA2, bytes([0x01, select]))

    command(device, client, 0x207, [port, (0xA3, b"\x01\x00")])
    clock.advance(1)
    off = stream(device, client, clock, 5)
    command(device, client, 0x207, [port, (0xA3, b"\x01\x01")])
    clock.advance(1)
    on = stream(device, client, clock, 5)

    fields = [f"pd2_output_{f}" for f in ("voltage", "current", "power")]
    assert len(off) == 50
    assert {tuple(frame[f] for f in fields) for frame in off} == {tuple(idle)}
    assert len({tuple(frame[f] for f in fields) for frame in on}) > 1


def test_a_table_naming_a_field_the_device_doesnt_send_is_refused() -> None:
    device = A1722()
    profile = device.profile
    if not isinstance(profile, Profile):
        raise TypeError(profile)
    device.profile = replace(profile, live=LiveSource("a2345_live.json", 1.0))

    with pytest.raises(LayoutError, match="A1722 sends no typed field"):
        device.vary_live_values(1)
