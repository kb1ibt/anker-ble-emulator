# Copyright (c) 2026 Shawn Stricker
"""A1783 live values: drawn once a ``c421`` period, carried by every frame built."""

from __future__ import annotations

import pytest

from anker_ble_emulator import A1763, A1765, A1783, A1785, ManualClock
from anker_ble_emulator.c_g2_490 import MSGTYPE_490
from anker_ble_emulator.devices.solix_c_gen2 import MSGTYPE_421, MSGTYPE_900
from anker_ble_emulator.layouts import LayoutError
from tests.fixtures.cgen2 import (
    A1783_TABLE,
    INPUT_POWERS,
    OUTPUT_POWERS,
    a1783_live,
    a1783_real,
    authorized_client,
    counters,
    named,
    poll,
    real_table,
    run,
)
from tests.fixtures.live import recorded


TOKEN = b"owner-token"
#: Polls at a mix of rates: faster than, at and slower than the 1 s period.
MIXED = (0.25, 0.5, 1.0, 3.0, 0.7, 7.3) * 40
#: Whole 0490 periods: each step's post and poll fall in one ``c421`` period.
POSTS = (540.0,) * 8
#: Names the ``0421``/``0900`` and the ``0490`` share: one state value.
SHARED = (
    "battery_level",
    "total_output_power",
    "ac_output_power",
    "pv1_input_power",
    "remaining_time",
)
LIVE_421 = (*OUTPUT_POWERS, "ac_input_power", "remaining_time")
#: The port outputs ``a6[0:2]`` sums (the survey: exact in 3 054 231 frames).
SEVEN_PORTS = (
    "ac_output_power",
    "car_charge1_output_power",
    "external_device_output_power",
    "pd1_output_power",
    "pd2_output_power",
    "pd3_output_power",
    "usb1_output_power",
)
PAIRED = ("pack_current", "mcu_input_power", "mcu_output_power")


def test_every_drawn_a1783_value_is_one_the_table_records() -> None:
    """Each live field reads a recorded sample, its idle value or its split."""
    device, clock, client = a1783_live(1, pd1_output_status=1)
    battery = {value for value, _ in A1783_TABLE["sections"][0]["slow"][0]["values"]}

    frames = run(device, client, clock, MIXED + POSTS)

    for _, msgtype, clear in frames:
        values = named(device, msgtype, clear)
        live = PAIRED if msgtype == MSGTYPE_490 else LIVE_421
        for name in live:
            assert values[name] in recorded(A1783_TABLE, name), name
        assert min(battery) <= int(values["battery_level"]) <= max(battery)


def test_the_a1783_totals_hold_in_every_frame() -> None:
    """``a6`` sums the output ports; ``.23.4`` the inputs, as drawn.

    The 540 s steps put each post and its poll in one period.
    """
    device, clock, client = a1783_live(
        2, pd1_output_status=1, ac_input_disable_switch=0
    )

    posting, posting_clock, posting_client = a1783_live(
        2, pd1_output_status=1, ac_input_disable_switch=0
    )

    frames = run(device, client, clock, MIXED)
    posted = run(posting, posting_client, posting_clock, POSTS * 3)

    outputs = [named(device, m, c) for _, m, c in frames if m != MSGTYPE_490]
    assert all(
        v["total_output_power"] == sum(int(v[p]) for p in OUTPUT_POWERS)
        for v in outputs
    )
    assert len({v["total_output_power"] for v in outputs}) > 1
    by_time = {(t, m): named(posting, m, c) for t, m, c in posted}
    inputs = set()
    for (t, m), post in by_time.items():
        if m == MSGTYPE_490:
            status = by_time[t, MSGTYPE_421]
            total = sum(int({**status, **post}[p]) for p in INPUT_POWERS)
            assert post["total_input_power"] == total
            inputs.add(total)
    assert len(inputs) > 1


def test_a_shared_name_agrees_between_the_0421_and_the_0490() -> None:
    """The ``0490`` posted in a period carries that period's ``0421`` values."""
    device, clock, client = a1783_live(3, pd1_output_status=1)

    frames = run(device, client, clock, POSTS)

    posts = {t: named(device, m, c) for t, m, c in frames if m == MSGTYPE_490}
    polled = {t: named(device, m, c) for t, m, c in frames if m == MSGTYPE_421}
    assert len(posts) == len(POSTS)
    for t, post in posts.items():
        assert {n: post[n] for n in SHARED} == {n: polled[t][n] for n in SHARED}
    assert len({post["battery_level"] for post in posts.values()}) > 1


def test_the_0490_pack_flow_follows_the_drawn_totals() -> None:
    """``.14.29``/``.14.31``/``.14.32`` take a pair recorded beside the totals.

    An unpaired draw holds the powers at the recording's or an earlier pair's,
    and ``.14.29`` follows the net port flow: 0 on the main, the expansion
    holding the bus as recorded.
    """
    device, clock, client = a1783_live(
        4, pd1_output_status=1, ac_input_disable_switch=0
    )
    block = A1783_TABLE["sections"][1]["paired"][0]
    pairs = {tuple(given): tuple(fields) for (given, fields), _ in block["samples"]}
    held = {(0, *fields[1:]) for fields in (*pairs.values(), (0, 0, 0))}

    frames = run(device, client, clock, POSTS * 3)

    matched = 0
    for _, msgtype, clear in frames:
        if msgtype != MSGTYPE_490:
            continue
        post = named(device, msgtype, clear)
        given = tuple(post[n] for n in block["given"])
        flow = tuple(post[n] for n in block["fields"])
        assert flow == pairs.get(given) or flow in held
        matched += flow == pairs.get(given) and any(flow)
    assert matched


def test_a1783_counters_stay_as_recorded() -> None:
    """The ``0490`` counters (energies, posts, cumulative charge) aren't drawn."""
    device, clock, client = a1783_live(5, pd1_output_status=1)
    quiet = A1783(clock=(quiet_clock := ManualClock()))
    quiet_client = authorized_client(quiet, TOKEN)

    drawn = run(device, client, clock, POSTS)
    today = run(quiet, quiet_client, quiet_clock, POSTS)

    assert [counters(named(device, m, c)) for _, m, c in drawn if m == MSGTYPE_490] == [
        counters(named(quiet, m, c)) for _, m, c in today if m == MSGTYPE_490
    ]
    assert counters(named(device, *drawn[0][1:]))


def test_a_pinned_a1783_value_never_changes() -> None:
    """A value set on the unit stays as set; the total still counts it."""
    device, clock, client = a1783_live(6, pd1_output_status=1, pd2_output_power=7)

    frames = run(device, client, clock, MIXED)

    outputs = [named(device, m, c) for _, m, c in frames if m != MSGTYPE_490]
    assert {v["pd2_output_power"] for v in outputs} == {7}
    assert {v["total_output_power"] for v in outputs} >= {7, 7 + 89}


def test_a_switched_off_a1783_port_stays_idle() -> None:
    """A port whose status is off has no recorded sample, so it reads idle."""
    device, clock, client = a1783_live(7, pd2_output_status=0, pd1_output_status=1)

    frames = run(device, client, clock, MIXED)

    outputs = [named(device, m, c) for _, m, c in frames if m != MSGTYPE_490]
    assert {v["pd2_output_power"] for v in outputs} == {0}
    assert {v["pd1_output_power"] for v in outputs} > {0}


def test_the_same_seed_reproduces_the_a1783_run() -> None:
    """The same seed and clock steps give the same frames; another seed doesn't."""
    runs = []
    for seed in (8, 8, 9):
        device, clock, client = a1783_live(seed, pd1_output_status=1)
        runs.append(run(device, client, clock, MIXED + POSTS))

    assert runs[0] == runs[1]
    assert runs[0] != runs[2]


def test_two_polls_within_one_period_are_identical() -> None:
    """Frames built inside one ``c421`` period share its draw, the poll's pair too."""
    device, clock, client = a1783_live(10, pd1_output_status=1)
    polls = []
    for _ in range(30):
        clock.advance(0.25)
        first = poll(device, client)
        clock.advance(0.5)
        assert poll(device, client) == first
        clock.advance(0.25)
        status, pushed = (
            named(device, m, first[m]) for m in (MSGTYPE_900, MSGTYPE_421)
        )
        assert status == pushed
        polls.append(first[MSGTYPE_421])
    assert len(set(polls)) > 1


def test_off_by_default_the_a1783_frames_never_move() -> None:
    """With no draws the polls are the recording, period after period.

    The posts move only by their counters: the counter page turns on its own.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)

    frames = run(device, client, clock, MIXED + POSTS)

    polls = {(m, clear) for _, m, clear in frames if m != MSGTYPE_490}
    posts = [named(device, m, clear) for _, m, clear in frames if m == MSGTYPE_490]
    assert device.negotiated_module.live is None
    assert sorted(m for m, _ in polls) == [MSGTYPE_421, MSGTYPE_900]
    assert len(posts) == len(POSTS)
    assert all(
        {n: v for n, v in post.items() if n not in counters(post)}
        == {n: v for n, v in posts[0].items() if n not in counters(posts[0])}
        for post in posts
    )


@pytest.mark.parametrize("model", [A1763, A1765, A1785])
def test_a_c_gen_2_model_with_no_recordings_refuses_live_values(
    model: type[A1783],
) -> None:
    """Only the A1783 has recordings to draw from."""
    with pytest.raises(LayoutError, match="no recorded live values"):
        model().vary_live_values(1)


def test_the_a1783_output_total_sums_all_seven_ports() -> None:
    """The built ``a6[0:2]`` total: all 7 port outputs, exact in every frame."""
    totals = {
        total["field"]: set(total["sum_of"])
        for section in real_table()["sections"]
        for total in section["totals"]
    }

    assert totals["total_output_power"] == set(SEVEN_PORTS)


def test_the_a1783_table_draws_nothing_the_layout_derives() -> None:
    """The flow state, the battery levels and ``.23.4`` follow, never drawn."""
    layout = A1783().layout
    assert layout is not None
    drawn = {
        name
        for section in real_table()["sections"]
        for name in (
            *(name for group in section["groups"] for name in group["live"]),
            *(total["field"] for total in section["totals"]),
            *(name for block in section["paired"] for name in block["fields"]),
            *(block["field"] for block in section["slow"]),
        )
    }

    assert drawn
    assert not drawn & layout.derived


def test_the_a1783_draws_only_what_its_recordings_hold() -> None:
    """Every drawn port power and time left is recorded; the slow fields stay in range.

    The ``a6[0:2]`` total holds in every ``0421`` / ``0900``.
    """
    table = real_table()
    device, clock, client = a1783_real(12)
    ports = [
        name
        for group in table["sections"][0]["groups"]
        for name in group["live"]
        if name.endswith("_power")
    ]
    ranges = {
        (section["msgtype"] == MSGTYPE_490, block["field"]): (
            min(v for v, _ in block["values"]),
            max(v for v, _ in block["values"]),
        )
        for section in table["sections"]
        for block in section["slow"]
    }

    frames = run(device, client, clock, MIXED + POSTS)

    seen: dict[str, set[object]] = {}
    for _, msgtype, clear in frames:
        values = named(device, msgtype, clear)
        if msgtype != MSGTYPE_490:
            for name in (*ports, "remaining_time"):
                assert values[name] in recorded(table, name), name
                seen.setdefault(name, set()).add(values[name])
            assert values["total_output_power"] == sum(
                int(values[p]) for p in SEVEN_PORTS
            )
        for (post, name), (low, high) in ranges.items():
            if post == (msgtype == MSGTYPE_490):
                assert low <= values[name] <= high, name
    assert any(len(values) > 1 for values in seen.values())


def test_a_shared_name_agrees_between_the_0421_and_the_0490_on_the_a1783_table() -> (
    None
):
    """The ``0490`` posted in a period carries that period's ``0421`` values."""
    device, clock, client = a1783_real(13)

    frames = run(device, client, clock, POSTS)

    posts = {t: named(device, m, c) for t, m, c in frames if m == MSGTYPE_490}
    polled = {t: named(device, m, c) for t, m, c in frames if m == MSGTYPE_421}
    assert len(posts) == len(POSTS)
    for t, post in posts.items():
        assert {n: post[n] for n in SHARED} == {n: polled[t][n] for n in SHARED}


def test_the_same_seed_reproduces_the_a1783_table_run() -> None:
    """``vary_live_values(seed)``: the same seed and steps give the same frames."""
    runs = []
    for seed in (14, 14, 15):
        device, clock, client = a1783_real(seed)
        runs.append(run(device, client, clock, MIXED))

    assert runs[0] == runs[1]
    assert runs[0] != runs[2]
