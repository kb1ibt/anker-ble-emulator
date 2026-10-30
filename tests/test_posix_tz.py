# Copyright (c) 2026 Shawn Stricker
"""POSIX TZ strings: the offset the module's TZ timer sends, and when it changes."""

import calendar

import pytest

from anker_ble_emulator.posix_tz import PosixTz


EASTERN = "EST5EDT,M3.2.0,M11.1.0"
#: 2026's US transitions: 2026-03-08 07:00 and 2026-11-01 06:00 UTC.
SPRING = calendar.timegm((2026, 3, 8, 7, 0, 0))
FALL = calendar.timegm((2026, 11, 1, 6, 0, 0))


@pytest.mark.parametrize(
    ("utc", "west"),
    [
        pytest.param(SPRING - 1, 18000, id="before spring forward: EST"),
        pytest.param(SPRING, 14400, id="spring forward: EDT"),
        pytest.param(FALL - 1, 14400, id="before fall back: EDT"),
        pytest.param(FALL, 18000, id="fall back: EST"),
    ],
)
def test_a_northern_zone_switches_at_its_rules(utc: int, west: int) -> None:
    assert PosixTz.parse(EASTERN).offset_west(utc) == west


def test_a_southern_zone_keeps_dst_across_the_new_year() -> None:
    """AEDT runs from October to April; the end time is in DST local time."""
    sydney = PosixTz.parse("AEST-10AEDT,M10.1.0,M4.1.0/3")
    ends = calendar.timegm((2026, 4, 4, 16, 0, 0))

    assert sydney.offset_west(calendar.timegm((2026, 1, 15, 0, 0, 0))) == -39600
    assert sydney.offset_west(ends - 1) == -39600
    assert sydney.offset_west(ends) == -36000
    assert sydney.offset_west(calendar.timegm((2026, 7, 1, 0, 0, 0))) == -36000


def test_the_next_change_is_the_next_rule_instant() -> None:
    eastern = PosixTz.parse(EASTERN)

    assert eastern.next_change(SPRING - 10) == SPRING
    assert eastern.next_change(SPRING) == FALL
    assert eastern.next_change(FALL) == calendar.timegm((2027, 3, 14, 7, 0, 0))


@pytest.mark.parametrize(
    ("text", "west"),
    [
        pytest.param("JST-9", -32400, id="no dst"),
        pytest.param("<+0530>-5:30", -19800, id="quoted name, minutes"),
        pytest.param("UTC0", 0, id="utc"),
    ],
)
def test_a_zone_without_dst_never_changes(text: str, west: int) -> None:
    zone = PosixTz.parse(text)

    assert zone.offset_west(SPRING) == west
    assert zone.next_change(SPRING) is None


def test_julian_and_zero_based_day_rules() -> None:
    """``Jn`` skips Feb 29; ``n`` counts it; the DST offset defaults to an hour less."""
    julian = PosixTz.parse("XST3XDT,J60/0,J300/0")
    counted = PosixTz.parse("XST3XDT,59/0,299/0")
    leap_march_1 = calendar.timegm((2028, 3, 1, 3, 0, 0))

    assert julian.offset_west(leap_march_1) == 7200
    assert julian.offset_west(leap_march_1 - 1) == 10800
    assert julian.offset_west(calendar.timegm((2026, 3, 1, 3, 0, 0))) == 7200
    assert counted.offset_west(calendar.timegm((2028, 2, 29, 3, 0, 0))) == 7200


def test_week_5_is_the_months_last_such_day() -> None:
    """CEST ends on October's last Sunday, the 4th in 2026, at 03:00 CEST."""
    central = PosixTz.parse("CET-1CEST,M3.5.0,M10.5.0/3")
    ends = calendar.timegm((2026, 10, 25, 1, 0, 0))

    assert central.offset_west(ends - 1) == -7200
    assert central.offset_west(ends) == -3600


def test_a_string_that_isnt_a_posix_tz_is_refused() -> None:
    with pytest.raises(ValueError, match="POSIX TZ"):
        PosixTz.parse("America/New_York")
