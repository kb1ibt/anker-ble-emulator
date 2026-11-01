# Copyright (c) 2026 Shawn Stricker
"""What the Prime A2345 and A91B2 share: their pushes, port blocks and setters.

PiField/docs/re/prime-a2345-a91b2.md "4a00 snapshot" and "Commands". The app
logs no property names for these devices, so fields take its names for the same
thing on the A1783 (``pd1OutputPower``, ``usb1OutputStatus``) and its Prime
handler names (``setACPortTimedTask``, ``setACPortCountdownTask``,
``setDCRelaySwitch``, ``setPortPriority``): reference/app-names.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from construct import Array, Flag, If, Int8sl, Int8ul, Int16ul, Struct, this

from anker_ble_emulator.layouts import (
    CLOCK,
    EPOCH,
    RTC,
    TYPE_BIN,
    TYPE_SILE,
    TYPE_UI,
    ui,
)
from anker_ble_emulator.mcu import COMMAND, Burst
from anker_ble_emulator.messages import BLE_REPLY_ROUTE
from anker_ble_emulator.parameters import (
    FieldGroup,
    ParameterContainer,
    ParameterDict,
    Parameters,
    clock_field,
    container,
    decode_field,
    decode_request,
    leaf_zeros,
)
from anker_ble_emulator.timed import Step, Timed
from anker_ble_emulator.validators import OptionsValidator


if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from anker_ble_emulator.layouts import Value
    from anker_ble_emulator.parameters import PushBuilder, SideEffect

    Validator = Callable[[ParameterDict], dict[str, Value] | None]

#: The two telemetry pushes: the realtime stream and the snapshot.
MSGTYPE_303 = 0x303
MSGTYPE_A00 = 0xA00
#: ``420b`` buys exactly 10 ``0303`` pushes ~1 s apart, the first with its
#: ``4a0b`` ack; a new ``420b`` sets the count back to 10. Both MCUs
#: (A2345 ``DAT_20001018``, A91B2 ``0x20000118``; doc "MCU pushes").
REALTIME_BURST = Burst(trigger=0x20B, push=MSGTYPE_303, count=10, period=1.0)

#: One output port's live state: status, then voltage, current and power (u16).
OUTPUT_FIELDS = ("output_status", "output_voltage", "output_current", "output_power")
OutputPort = Struct(
    *(name / ui(1 if name == "output_status" else 2) for name in OUTPUT_FIELDS)
)
#: Each push's output ports: port name -> its tag.
PORT_GROUPS = {
    MSGTYPE_303: FieldGroup(
        OutputPort,
        leaf_zeros(*OUTPUT_FIELDS),
        {
            "pd1": "a2",
            "pd2": "a3",
            "pd3": "a4",
            "pd4": "a5",
            "usb1": "a6",
            "usb2": "a7",
        },
    ),
    MSGTYPE_A00: FieldGroup(
        OutputPort,
        leaf_zeros(*OUTPUT_FIELDS),
        {
            "pd1": "a4",
            "pd2": "a5",
            "pd3": "a6",
            "pd4": "a7",
            "usb1": "a8",
            "usb2": "a9",
        },
    ),
}

#: A port's switch, timed task and countdown task: the head of each device's
#: own per-port record.
SCHEDULE_HEAD = (
    "relay_switch" / Flag,
    # The start/end switches are 3-state (0xff "unset", not just 0/1): ui(1)
    # round-trips 0xff byte-exact, where Flag would corrupt it to 0x01 on
    # any rebuild of this tag ("An unset schedule half keeps 0c 00 behind its ff").
    "timed_task_start_switch" / ui(1),
    "timed_task_start_hour" / ui(1),
    "timed_task_start_minute" / ui(1),
    "timed_task_start_weekdays" / ui(1),
    "timed_task_end_switch" / ui(1),
    "timed_task_end_hour" / ui(1),
    "timed_task_end_minute" / ui(1),
    "timed_task_end_weekdays" / ui(1),
    "countdown_task_switch" / Flag,
    "countdown_task_seconds" / ui(4),
    "countdown_task_remaining_seconds" / ui(4),
)
SCHEDULE_HEAD_FIELDS = tuple(sc.name for sc in SCHEDULE_HEAD if sc.name)

#: What the port setters' accepted acks carry back from the request: the port
#: and ``4208``'s slot (captured ``0a07``/``0a08``/``0a09``).
PORT_ECHOES = {0x207: ("a2",), 0x208: ("a2", "a3"), 0x209: ("a2",)}
#: The acks that then carry the unit's clock, not the request's (doc
#: "Commands", "Reply ``a1``").
PORT_CLOCKED = frozenset(PORT_ECHOES)
#: A ``4208``'s ``0308`` goes out before its ack (doc ``4208`` row: the ``0308``
#: "arrives before the ack").
TIMED_TASK_LEADS = {0x208: frozenset({0x308})}

SWITCH = OptionsValidator(ui(1), 0, 1)
#: ``4203 a2``: the screen timeout, the same enum as the snapshot's.
SCREEN_TIMEOUT = OptionsValidator(ui(1), 0, 1, 2, 3, 4)
#: ``4208 a3``: 1 sets the start half of a timed task, 0 the end half.
SCHEDULE_SLOT = OptionsValidator(ui(1), 0, 1)
HALF_BY_SLOT = {1: "start", 0: "end"}
SLOT_BY_HALF = {half: slot for slot, half in HALF_BY_SLOT.items()}
#: A timed task's day: weekday masks run bit 0 Monday to bit 6 Sunday, and RTC
#: day 0 (1970-01-01) is a Thursday.
DAY = 86400
WEEK = 7
EVERY_DAY = 0x7F
RTC_DAY0_WEEKDAY = 3
HALF_FIELDS = ("switch", "hour", "minute", "weekdays")
#: ``4208 a4``: one timed-task half; ``0308 a4`` reports one the same way.
ScheduleHalf = Struct(
    "switch" / Flag, "hour" / ui(1), "minute" / ui(1), "weekdays" / ui(1)
)
#: ``4209 a3``: the countdown switch, then its seconds unless disarming.
Countdown = Struct("switch" / Flag, "seconds" / If(this.switch, ui(4)))
#: ``0307 a3``: a port's countdown record.
COUNTDOWN_FIELDS = (
    "countdown_task_switch",
    "countdown_task_seconds",
    "countdown_task_remaining_seconds",
)
CountdownRecord = Struct(
    "countdown_task_switch" / Flag,
    "countdown_task_seconds" / ui(4),
    "countdown_task_remaining_seconds" / ui(4),
)
#: ``0307 a3`` from the A91B2: the remaining time first (``0x0800b686``).
CountdownRecordRemainingFirst = Struct(
    "countdown_task_switch" / Flag,
    "countdown_task_remaining_seconds" / ui(4),
    "countdown_task_seconds" / ui(4),
)


#: The unit's WiFi (link-state bit 1), set by ``set_wifi``; carried in no frame.
WIFI_UP = "_wifi_up"
#: The ``a1`` that reaches the BLE client; every push is built on it.
BLE_ROUTE = 0x31
#: Where each push goes (doc "Push routing"): its ``a1`` with BLE alone (None:
#: not sent), then its ``a1`` s with WiFi up. Only ``0x31`` reaches BLE; the
#: rest are MQTT (``64``/``44`` param_info, ``34`` device update, ``32``
#: state_info). Pushes not listed stay on BLE.
Routes = dict[int, tuple[int | None, tuple[int, ...]]]
PUSH_ROUTES: Routes = {
    0x300: (None, (0x32,)),
    0x301: (None, (0x64,)),
    0x303: (BLE_ROUTE, (0x34,)),
    0x304: (BLE_ROUTE, (0x64, 0x44)),
    0x312: (None, (0x32,)),
    **dict.fromkeys(
        (770, 773, *range(775, 780), 781, *range(783, 786)), (BLE_ROUTE, (100,))
    ),
}


def reaches_ble(routes: Routes, msgtype: int, values: Mapping[str, Value]) -> bool:
    """Whether a push goes out on the BLE link in the unit's link state."""
    if msgtype not in routes:
        return True
    ble, wifi = routes[msgtype]
    return BLE_ROUTE in (wifi if values.get(WIFI_UP) else (ble,))


def mqtt_routed(
    routes: Routes, msgtype: int, payload: bytes, values: Mapping[str, Value]
) -> tuple[tuple[int, bytes], ...]:
    """Return a push's MQTT copies in the unit's link state: (msgtype, payload).

    Each carries its MQTT ``a1``; none while WiFi is down.
    """
    if msgtype not in routes or not values.get(WIFI_UP):
        return ()
    params = Parameters.parse(payload)
    copies = []
    for route in routes[msgtype][1]:
        params["a1"] = ParameterContainer(
            key=b"\xa1", type=None, value=Int8ul.build(route)
        )
        copies.append((msgtype, Parameters.build(params)))
    return tuple(copies)


def wifi_events(
    before: Mapping[str, Value],
    after: Mapping[str, Value],
    timers: tuple[Timed, ...],
) -> tuple[Timed, ...]:
    """Return what the unit's WiFi coming up or going down starts or stops.

    Up sends ``0301`` (doc "Push routing") and starts ``timers``, the MQTT-only
    periodics; down stops them. The MCU's timers free-run from boot, so their
    phase here, a full period after WiFi comes up, is the emulator's.
    """
    if after.get(WIFI_UP) and not before.get(WIFI_UP):
        return (Timed(0, "wifi_up", _wifi_came_up), *timers)
    if before.get(WIFI_UP) and not after.get(WIFI_UP):
        return tuple(Timed(0, timer.key, drop_queued) for timer in timers)
    return ()


def every(
    period: float,
    key: str,
    build: Callable[[Mapping[str, Value]], tuple[int, bytes] | None],
) -> Timed:
    """Return a timer sending ``build``'s push every ``period`` s (none for None)."""
    return Timed(period, key, partial(_tick, period, key, build))


def _tick(
    period: float,
    key: str,
    build: Callable[[Mapping[str, Value]], tuple[int, bytes] | None],
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Send the timer's push, if any, and arm the next."""
    frame = build(state)
    return Step(
        frames=() if frame is None else (frame,),
        then=(every(period, key, build),),
    )


#: ``0300``'s six ports, bit 0 first: C1-C4, A1, A2 (doc "``state_info`` (MQTT)").
STATS_PORTS = ("pd1", "pd2", "pd3", "pd4", "usb1", "usb2")
StatsPorts = Struct("active" / Int8ul, "power" / Array(len(STATS_PORTS), Int16ul))
#: ``0300`` every 6 000 and ``0312`` every 600 ticks of 100 ms (doc "Periodic
#: pushes"), both MQTT ``state_info`` only.
STATS_PERIOD = 600.0
COUNTRY_PERIOD = 60.0
SECONDS_PER_HOUR = 3600


def utc_hours(state: Mapping[str, Value]) -> int:
    """Return the clock's UTC offset in whole hours: local time less UTC."""
    return round((int(state[RTC]) - int(state[EPOCH])) / SECONDS_PER_HOUR)


def stats_push(state: Mapping[str, Value], *, step: int) -> tuple[int, bytes]:
    """``0300``: the active ports' bitmap, six port powers, their total, UTC hours.

    A port is active when its block's status is 1. Power is the port blocks'
    0.01 W, rounded down to a multiple of ``step`` (the A2345's 0.1 W is 10).
    """
    powers = [int(state[f"{p}_output_power"]) // step * step for p in STATS_PORTS]
    active = sum(
        1 << bit
        for bit, port in enumerate(STATS_PORTS)
        if state[f"{port}_output_status"] == 1
    )
    fields = {
        "a2": container(
            "a2", TYPE_BIN, StatsPorts.build({"active": active, "power": powers})
        ),
        "a3": container("a3", TYPE_SILE, ui(2).build(sum(powers))),
        "a4": container("a4", TYPE_UI, Int8sl.build(utc_hours(state))),
    }
    return timed_push(0x300, fields, state)


def _wifi_came_up(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``0301``: the firmware version, as recorded (MQTT ``a10164a203024408``)."""
    version = container("a2", TYPE_SILE, ui(2).build(state["firmware_version"]))
    return Step(frames=(timed_push(0x301, {"a2": version}, state),))


def timed_push(
    msgtype: int, fields: Mapping[str, ParameterContainer], state: Mapping[str, Value]
) -> tuple[int, bytes]:
    """Return a push the MCU sends on its own: its route, ``fields``, its clock."""
    return untimed_push(msgtype, {**fields, **clock_field(state)})


def untimed_push(
    msgtype: int, fields: Mapping[str, ParameterContainer]
) -> tuple[int, bytes]:
    """Return a push the MCU sends on its own that carries no clock (``0311``)."""
    route = ParameterContainer(
        key=b"\xa1", type=None, value=Int8ul.build(BLE_REPLY_ROUTE)
    )
    return msgtype, Parameters.build(ParameterDict({"a1": route, **fields}))


def set_screen_timeout(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screen timeout if ``a2`` is one of its five enum values."""
    return decode_request(request, {"a2": ("screen_timeout", SCREEN_TIMEOUT)})


def ac_port_timed_task(port_by_select: Mapping[int, str]) -> Validator:
    """Return the ``4208`` validator for a device's ``a2`` port numbering."""
    port_select = OptionsValidator(ui(1), *port_by_select)

    def set_ac_port_timed_task(request: ParameterDict) -> dict[str, Value] | None:
        """Return a port's timed-task start or end half if its values fit."""
        name = port_by_select[decode_field(request, "a2", port_select)]
        half = HALF_BY_SLOT[decode_field(request, "a3", SCHEDULE_SLOT)]
        parsed = decode_field(request, "a4", ScheduleHalf)
        return {
            f"{name}_timed_task_{half}_{field}": parsed[field] for field in HALF_FIELDS
        }

    return set_ac_port_timed_task


def schedule_slot(state: Mapping[str, Value], port: str, half: str) -> dict[str, Value]:
    """Return a port's schedule slot (timed-task half), by ``HALF_FIELDS`` name."""
    return {name: state[f"{port}_timed_task_{half}_{name}"] for name in HALF_FIELDS}


def once_date_name(port: str, half: str) -> str:
    """Return the hidden name of a slot's date, off the wire.

    As year * 10000 + month * 100 + day; unset, the zeroed 0/0/0 both MCUs'
    ``settings_defaults_init?`` writes (doc "Defaults and collisions", A91B2
    ``4208`` row).
    """
    return f"_{port}_timed_task_{half}_date"


def latch_name(port: str, half: str) -> str:
    """Return the hidden name of a repeating slot's fired-this-match latch."""
    return f"_{port}_timed_task_{half}_latched"


MINUTE = 60
#: The weekday bit both MCUs give the zeroed date 0/0/0: Monday (doc "Defaults
#: and collisions"; the A91B2's ``weekday_bit_from_date?``).
ZEROED_DATE_WEEKDAY = 0x01


def rtc_date(rtc: int) -> int:
    """Return the RTC's local date as year * 10000 + month * 100 + day."""
    when = datetime.fromtimestamp(rtc, tz=UTC)
    return when.year * 10000 + when.month * 100 + when.day


def date_rtc(date: int, minute: int) -> int:
    """Return the RTC second from which a stored date and minute no longer lie ahead.

    A day past its month's end (a plain day + 1) never equals a real date, so
    it reads as passed from the 1st of the next month; 0/0/0 reads passed.
    """
    year, rest = divmod(date, 10000)
    month, day = divmod(rest, 100)
    if not month:
        return 0
    at = int(datetime(year, month, 1, tzinfo=UTC).timestamp()) + (day - 1) * DAY
    if datetime.fromtimestamp(at, tz=UTC).month != month:
        return at
    return at + minute * MINUTE


def slot_date(rtc: int, hour: int, minute: int) -> int:
    """Return a once slot's date: today if its hh:mm is strictly later, else tomorrow.

    Both MCUs' slot set (doc "Slot record", A91B2 ``4208`` row): an equal
    minute rolls to tomorrow; seconds are never read; the carry is a real,
    leap-aware one.
    """
    day = rtc // DAY
    if day * DAY + (hour * 60 + minute) * MINUTE <= rtc - rtc % MINUTE:
        day += 1
    return rtc_date(day * DAY)


def slot_days(slot: Mapping[str, Value], date: int) -> int:
    """Return a slot's weekday bits: its mask, or a once slot's date's weekday bit."""
    days = int(slot["weekdays"]) & EVERY_DAY
    if days:
        return days
    if not date:
        return ZEROED_DATE_WEEKDAY
    year, rest = divmod(date, 10000)
    month, day = divmod(rest, 100)
    return 1 << datetime(year, month, day, tzinfo=UTC).weekday()


def next_minute(rtc: int, at: int, days: int) -> int:
    """Return the RTC start of the next minute ``at`` (from this one) on ``days``."""
    day = rtc // DAY
    return next(
        (day + ahead) * DAY + at * MINUTE
        for ahead in range(WEEK + 1)
        if (day + ahead) * DAY + at * MINUTE >= rtc - rtc % MINUTE
        and days >> (day + ahead + RTC_DAY0_WEEKDAY) % WEEK & 1
    )


def slot_written(port_by_select: Mapping[int, str]) -> SideEffect:
    """Return ``4208``'s slot set: a once slot's date, the colliding other cleared.

    Both MCUs (doc "Defaults and collisions", A91B2 ``0308`` row): every
    enabled once write (weekdays 0) dates its slot again, an ``en 0`` write
    leaving the date. The other slot collides if both are enabled (the unset
    ``ff`` included) at the same hh:mm and both are once slots, or on a shared
    day, a once slot's day being its date's weekday; the written one wins.
    """

    def slot_set(
        request: ParameterDict, state: Mapping[str, Value]
    ) -> dict[str, Value]:
        port = port_by_select[decode_field(request, "a2", ui(1))]
        slot = decode_field(request, "a3", ui(1))
        written = decode_field(request, "a4", ScheduleHalf)
        half, other_half = HALF_BY_SLOT[slot], HALF_BY_SLOT[1 - slot]
        changes: dict[str, Value] = {}
        date = int(state.get(once_date_name(port, half), 0))
        once = not int(written["weekdays"]) & EVERY_DAY
        if once and written["switch"]:
            date = slot_date(int(state[RTC]), written["hour"], written["minute"])
            changes[once_date_name(port, half)] = date
        other = schedule_slot(state, port, other_half)
        other_date = int(state.get(once_date_name(port, other_half), 0))
        other_once = not int(other["weekdays"]) & EVERY_DAY
        collides = (
            written["switch"]
            and other["switch"]
            and (written["hour"], written["minute"]) == (other["hour"], other["minute"])
            and (
                (once and other_once)
                or slot_days(written, date) & slot_days(other, other_date)
            )
        )
        if collides:
            changes[f"{port}_timed_task_{other_half}_switch"] = 0
        return changes

    return slot_set


def once_compare(state: Mapping[str, Value], port: str, half: str) -> int:
    """Return now against a once slot's date and minute: -1 before, 0 at, 1 past.

    ``date_cmp?``, then ``time_cmp?``, on both MCUs (doc "Schedule timing",
    A91B2 "Schedule firing").
    """
    rtc = int(state[RTC])
    slot = schedule_slot(state, port, half)
    now = (rtc_date(rtc), rtc % DAY // MINUTE)
    bound = (
        int(state.get(once_date_name(port, half), 0)),
        int(slot["hour"]) * 60 + int(slot["minute"]),
    )
    return (now > bound) - (now < bound)


def slot_disabled_push(
    select: int, half: str, slot: Mapping[str, Value], state: Mapping[str, Value]
) -> tuple[int, bytes]:
    """Return ``0308``: a slot the timer run just disabled, its record en 00."""
    fields = {
        "a2": container("a2", TYPE_UI, ui(1).build(select)),
        "a3": container("a3", TYPE_UI, ui(1).build(SLOT_BY_HALF[half])),
        "a4": container("a4", TYPE_BIN, ScheduleHalf.build({**slot, "switch": 0})),
    }
    return timed_push(0x308, fields, state)


def date_once_slots(
    port_by_select: Mapping[int, str],
    before: Mapping[str, Value],
    after: Mapping[str, Value],
    plan: Callable[[Mapping[str, Value]], tuple[Timed, ...]],
) -> tuple[Timed, ...]:
    """Return the dating of each enabled once slot (weekdays 0) a change wrote.

    A ``4208`` dates its slot itself (``slot_written``), so this covers
    ``set_values`` only; ``plan`` times the next timer run by the new date.
    """
    if COMMAND in after:
        return ()
    events = []
    for port in port_by_select.values():
        for half in HALF_BY_SLOT.values():
            slot = schedule_slot(after, port, half)
            once = slot["switch"] == 1 and not int(slot["weekdays"]) & EVERY_DAY
            if once and slot != schedule_slot(before, port, half):
                dated = partial(_once_dated, port, half, plan)
                events.append(Timed(0, once_date_name(port, half), dated))
    return tuple(events)


def _once_dated(
    port: str,
    half: str,
    plan: Callable[[Mapping[str, Value]], tuple[Timed, ...]],
    values: Mapping[str, Value],
    state: Mapping[str, Value],
) -> Step:
    """Date a once slot by the RTC, as ``slot_date`` does; plan by it."""
    slot = schedule_slot(state, port, half)
    date = slot_date(int(state[RTC]), int(slot["hour"]), int(slot["minute"]))
    return hidden_set(once_date_name(port, half), date, plan, values, state)


def hidden_set(
    name: str,
    value: Value,
    plan: Callable[[Mapping[str, Value]], tuple[Timed, ...]],
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Set a hidden value a device's timer run reads, and ``plan`` the run by it."""
    return Step(values={name: value}, then=plan({**state, name: value}))


def countdown_shown_name(port: str) -> str:
    """Return the hidden name of the countdown switch a port's last ``0307`` showed."""
    return f"_{port}_countdown_shown"


def countdown_shown_events(
    port_by_select: Mapping[int, str],
    before: Mapping[str, Value],
    after: Mapping[str, Value],
    plan: Callable[[Mapping[str, Value]], tuple[Timed, ...]],
) -> tuple[Timed, ...]:
    """Return the shown switch of each countdown a change armed or disarmed first.

    Both MCUs' countdown tick pushes ``0307`` on the edge between the switch
    and the one it last showed (doc ``0307`` rows); not shown yet, that is the
    switch before the change.
    """
    events = []
    for port in port_by_select.values():
        switch = f"{port}_countdown_task_switch"
        name = countdown_shown_name(port)
        if name not in after and bool(before[switch]) != bool(after[switch]):
            shown = partial(hidden_set, name, bool(before[switch]), plan)
            events.append(Timed(0, name, shown))
    return tuple(events)


def timed_task_push(port_by_select: Mapping[int, str]) -> PushBuilder:
    """Return the ``0308`` builder: the half a ``4208`` collision disabled."""

    def disabled_half(
        request: ParameterDict,
        before: Mapping[str, Value],
        after: Mapping[str, Value],
    ) -> ParameterDict | None:
        port = port_by_select[decode_field(request, "a2", ui(1))]
        other_slot = 1 - decode_field(request, "a3", ui(1))
        half = HALF_BY_SLOT[other_slot]
        switch = f"{port}_timed_task_{half}_switch"
        if not before[switch] or after[switch]:
            return None
        return ParameterDict(
            {
                "a2": request["a2"],
                "a3": container("a3", TYPE_UI, ui(1).build(other_slot)),
                "a4": container(
                    "a4", TYPE_BIN, ScheduleHalf.build(schedule_slot(after, port, half))
                ),
            }
        )

    return disabled_half


def ac_port_countdown_task(
    port_by_select: Mapping[int, str], *, disarm_clears: Collection[str]
) -> Validator:
    """Return the ``4209`` validator for a device's ``a2`` port numbering.

    Arming starts the remaining time at the seconds set; disarming keeps the
    seconds, and the remaining time too except on the ``disarm_clears`` ports.
    """
    port_select = OptionsValidator(ui(1), *port_by_select)

    def set_ac_port_countdown_task(request: ParameterDict) -> dict[str, Value] | None:
        """Return a port's countdown switch, and seconds unless disarming."""
        name = port_by_select[decode_field(request, "a2", port_select)]
        countdown = decode_field(request, "a3", Countdown)
        changes: dict[str, Value] = {f"{name}_countdown_task_switch": countdown.switch}
        if countdown.switch:
            changes[f"{name}_countdown_task_seconds"] = countdown.seconds
            changes[f"{name}_countdown_task_remaining_seconds"] = countdown.seconds
        elif name in disarm_clears:
            changes[f"{name}_countdown_task_remaining_seconds"] = 0
        return changes

    return set_ac_port_countdown_task


#: ``0302``'s one source byte (``DAT_2000a7ce``) for all ports, in no frame. An
#: app ``4207`` writes it 1, a firmware switch change 2.
SWITCH_SOURCE = "_switch_report_source"
SOURCE_APP = 1
SOURCE_UNIT = 2


#: The A2345's SysTick groups (doc "Scheduler groups") and the A91B2's TIM1
#: tasks (its "Timer timing"): each first fires one period after boot and
#: every period after, nothing at boot itself. Boot's instant on the device
#: clock is a hidden per-run input (0-3 s, default 0), one name for both.
SYSTICK_PHASE = "systick_phase"
SYSTICK_CYCLE = 3.0
#: ``tick_100ms_group``: the grid the screen reports and switch watcher run on.
#: ``screen_report_service_tick`` skips while ``FUN_0001c758()`` reads
#: nonzero, unexplained in the doc and not modeled.
TICK_100MS = 0.1
#: A mark writes count 0 and each tick counts 1 up; the 5th sends (doc
#: "Report timing").
REPORT_TICKS = 5
#: ``port_switch_tick`` runs after ``screen_report_service_tick`` in a tick.
AFTER_SCREEN_REPORTS = 1e-6
#: The float slack a tick instant is matched with.
TICK_SLACK = 1e-9


def on_grid(
    state: Mapping[str, Value], period: float, at: float, *, at_or_after: bool
) -> float:
    """Return a SysTick group's first instant after ``at``, or at it too."""
    phase = float(state.get(SYSTICK_PHASE, 0.0))
    ticks = (at - phase) / period
    if at_or_after:
        count = math.ceil(ticks - TICK_SLACK)
    else:
        count = math.floor(ticks + TICK_SLACK) + 1
    return phase + max(count, 1) * period


def grid_delay(
    state: Mapping[str, Value], period: float, offset: float, at: float
) -> float:
    """Return the delay to a task's first run at or after ``at``, ``offset`` into it.

    A run already made at this instant (``offset`` past its tick) doesn't count.
    """
    now = float(state[CLOCK])
    following = on_grid(state, period, now - offset, at_or_after=False)
    first = on_grid(state, period, at - offset, at_or_after=True)
    return max(following, first) + offset - now


def check_systick_phase(values: Mapping[str, Value]) -> None:
    """Refuse a ``systick_phase`` outside 0-3 s.

    Raises:
        TypeError: If it is set outside that or not a number.

    """
    phase = values.get(SYSTICK_PHASE)
    if phase is not None and (
        isinstance(phase, bytes) or not 0 <= phase < SYSTICK_CYCLE
    ):
        msg = f"{SYSTICK_PHASE} is 0 to {SYSTICK_CYCLE} s"
        raise TypeError(msg)


def report_delay(state: Mapping[str, Value]) -> float:
    """Return the seconds from now to the 5th ``tick_100ms_group`` tick of a count.

    A change counts from the next tick: one from a command or the unit falls
    between ticks, one from the 150 ms pass after that tick's 100 ms group.
    """
    now = float(state[CLOCK])
    first = on_grid(state, TICK_100MS, now, at_or_after=False)
    return first + (REPORT_TICKS - 1) * TICK_100MS - now


@dataclass(frozen=True)
class SwitchWatch:
    """A switch watcher: each port's ``0302`` on the 5th tick after its last change.

    ``port_switch_change_mark?`` marks the port and zeroes its count, so a
    change restarts only that port's (doc "``0302`` timing", "Report timing").
    Each tick's pass, after the screen reports', walks the due ports in order:
    a source byte of 1 drops one and resets the byte to 0; 0 or 2 sends it,
    with the switch as it then stands.

    Attributes:
        ports: The ports, as (select, name), in select order.

    """

    ports: tuple[tuple[int, str], ...]

    def mark(self, select: int, state: Mapping[str, Value]) -> dict[str, Value]:
        """Return a change of port ``select`` marked: the tick it's due on."""
        delay = report_delay(state) + AFTER_SCREEN_REPORTS
        return {self._due(select): float(state[CLOCK]) + delay}

    def marked(
        self, before: Mapping[str, Value], after: Mapping[str, Value]
    ) -> tuple[Timed, ...]:
        """Return the send of each port a change marked anew."""
        return tuple(
            self.report(select, after)
            for select, _ in self.ports
            if before.get(self._due(select)) != after.get(self._due(select))
        )

    def report(self, select: int, after: Mapping[str, Value]) -> Timed:
        """Return port ``select``'s send on its due tick, ``after`` its mark."""
        port = dict(self.ports)[select]
        delay = max(float(after[self._due(select)]) - float(after[CLOCK]), 0.0)
        return Timed(delay, f"{port}_switch_report", self.send)

    def send(
        self,
        values: Mapping[str, Value],  # noqa: ARG002  # reads the whole state
        state: Mapping[str, Value],
    ) -> Step:
        """Walk the ports due on this tick, in order; the source byte decides each."""
        now = float(state[CLOCK])
        source = state.get(SWITCH_SOURCE, 0)
        frames, walked = [], []
        for select, port in self.ports:
            due = state.get(self._due(select))
            if due is None or float(due) > now + TICK_SLACK:
                continue
            walked.append(self._due(select))
            if source == SOURCE_APP:
                source = 0
                continue
            a2 = container("a2", TYPE_UI, ui(1).build(select))
            a3 = container("a3", TYPE_UI, ui(1).build(state[f"{port}_relay_switch"]))
            frames.append(timed_push(0x302, {"a2": a2, "a3": a3}, state))
        if not walked:
            return Step()
        return Step(
            values={SWITCH_SOURCE: source}, unset=tuple(walked), frames=tuple(frames)
        )

    def _due(self, select: int) -> str:
        """Return the hidden tick instant port ``select`` is due on."""
        return f"_{dict(self.ports)[select]}_switch_due"


def drop_queued(
    values: Mapping[str, Value],  # noqa: ARG001  # replaces a pending event
    state: Mapping[str, Value],  # noqa: ARG001
) -> Step:
    """Do nothing: this event only replaces the one queued under its key."""
    return Step()
