# Copyright (c) 2026 Shawn Stricker
"""Time sources."""

from anker_ble_emulator.clock import ManualClock, MonotonicClock


def test_monotonic_clock_moves_forward() -> None:
    clock = MonotonicClock()

    first = clock.now()

    assert clock.now() >= first


def test_manual_clock_moves_only_when_advanced() -> None:
    clock = ManualClock(start=5.0)

    clock.advance(2.5)

    assert clock.now() == 7.5
