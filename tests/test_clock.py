# Copyright (c) 2026 Shawn Stricker
"""Time sources."""

from anker_ble_emulator.clock import ManualClock, MonotonicClock, ScaledClock


def test_monotonic_clock_moves_forward() -> None:
    clock = MonotonicClock()

    first = clock.now()

    assert clock.now() >= first
    assert not clock.bypassed(first)


def test_manual_clock_moves_only_when_advanced() -> None:
    clock = ManualClock(start=5.0)

    clock.advance(2.5)

    assert clock.now() == 7.5
    assert not clock.bypassed(6.0)


def test_a_jump_bypasses_what_comes_due_inside_it() -> None:
    clock = ManualClock(start=10.0)

    clock.jump(5.0)
    clock.advance(5.0)

    assert clock.now() == 20.0
    assert clock.bypassed(12.0)
    assert not clock.bypassed(10.0)
    assert not clock.bypassed(15.0)
    assert not clock.bypassed(17.0)


def test_a_scaled_clock_runs_its_source_at_its_speed() -> None:
    source = ManualClock(start=100.0)
    clock = ScaledClock(60.0, source)

    source.advance(9.0)

    assert clock.now() == 640.0
    assert not clock.bypassed(300.0)
