# Copyright (c) 2026 Shawn Stricker
"""What the Prime A2345 and A91B2 share: their pushes, port blocks and setters.

PiField/docs/re/prime-a2345-a91b2.md "4a00 snapshot" and "Commands". The app
logs no property names for these devices, so fields take its names for the same
thing on the A1783 (``pd1OutputPower``, ``usb1OutputStatus``) and its Prime
handler names (``setACPortTimedTask``, ``setACPortCountdownTask``,
``setDCRelaySwitch``, ``setPortPriority``): reference/app-names.md.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from construct import Array, Flag, If, Int8sl, Int8ul, Int16ul, Struct, this

from anker_ble_emulator.layouts import TYPE_BIN, TYPE_SILE, TYPE_UI, TYPE_VAR, ui
from anker_ble_emulator.mcu import EPOCH, RTC, Burst
from anker_ble_emulator.messages import BLE_REPLY_ROUTE
from anker_ble_emulator.parameters import (
    FieldGroup,
    ParameterContainer,
    ParameterDict,
    Parameters,
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

#: What the port setters' accepted acks carry back from the request: the port,
#: ``4208``'s slot, and the clock (captured ``0a07``/``0a08``/``0a09``; the doc's
#: ``4207`` "reply ``a2`` port ``fe``"). The device writes its own clock; the
#: request's stands in for it.
PORT_ECHOES = {0x207: ("a2", "fe"), 0x208: ("a2", "a3", "fe"), 0x209: ("a2", "fe")}
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
#: An armed countdown's remaining time counts down once a second.
COUNTDOWN_TICK = 1.0
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
    """Return a push the MCU sends on its own: its route, ``fields``, its clock.

    The clock is the device's UTC epoch, where a command's pushes carry the
    request's.
    """
    clock = container("fe", TYPE_VAR, ui(4).build(state[EPOCH]))
    return untimed_push(msgtype, {**fields, "fe": clock})


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


def _half(state: Mapping[str, Value], port: str, half: str) -> dict[str, Value]:
    """Return one timed-task half of a port, by ``HALF_FIELDS`` name."""
    return {field: state[f"{port}_timed_task_{half}_{field}"] for field in HALF_FIELDS}


def _days_overlap(days: int, other: int) -> bool:
    """Whether two weekday masks share a day; two once (0) halves count as one day."""
    return bool(days & other) or days == other == 0


def timed_task_conflict(port_by_select: Mapping[int, str]) -> SideEffect:
    """Return ``4208``'s conflict-clear for a device's ``a2`` port numbering.

    A written half that collides with the port's other half (both enabled, the
    unset ``ff`` included, at the same time on a shared day) disables the other.
    """

    def clear_colliding_half(
        request: ParameterDict, state: Mapping[str, Value]
    ) -> dict[str, Value]:
        port = port_by_select[decode_field(request, "a2", ui(1))]
        slot = decode_field(request, "a3", ui(1))
        written = decode_field(request, "a4", ScheduleHalf)
        other = _half(state, port, HALF_BY_SLOT[1 - slot])
        collides = (
            written["switch"]
            and other["switch"]
            and (written["hour"], written["minute"]) == (other["hour"], other["minute"])
            and _days_overlap(int(written["weekdays"]), int(other["weekdays"]))
        )
        if not collides:
            return {}
        return {f"{port}_timed_task_{HALF_BY_SLOT[1 - slot]}_switch": 0}

    return clear_colliding_half


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
                    "a4", TYPE_BIN, ScheduleHalf.build(_half(after, port, half))
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


def countdown_timers(
    port_by_select: Mapping[int, str],
    before: Mapping[str, Value],
    after: Mapping[str, Value],
    *,
    disarm_clears: Collection[str],
    remaining_first: bool,
) -> tuple[Timed, ...]:
    """Return the countdown events a change starts, one ``<port>_countdown`` each.

    A port armed, re-armed or given a new remaining time counts down from it,
    once a second; a disarmed one's pending expiry is dropped. The remaining
    time is ticked live, so every frame reads it as it stands.
    """
    events = []
    for select, port in port_by_select.items():
        switch = f"{port}_countdown_task_switch"
        remaining = f"{port}_countdown_task_remaining_seconds"
        key = f"{port}_countdown"
        if after[switch] and (
            not before[switch] or before[remaining] != after[remaining]
        ):
            tick = _Countdown(port, select, port in disarm_clears, remaining_first)
            events.append(Timed(COUNTDOWN_TICK, key, tick))
        elif before[switch] and not after[switch]:
            events.append(Timed(0, key, drop_queued))
    return tuple(events)


def schedule_timers(
    port_by_select: Mapping[int, str],
    before: Mapping[str, Value],
    after: Mapping[str, Value],
) -> tuple[Timed, ...]:
    """Return the timed-task events a change starts, one ``<port>_timed_task_<half>``.

    An enabled half (switch 1; the unset ``ff`` never fires) is timed to its
    next hh:mm on one of its weekdays by the RTC, again when a clock set moves
    the RTC; one disabled or changed drops what was queued.
    """
    clock_set = before[RTC] != after[RTC]
    events = []
    for select, port in port_by_select.items():
        for half in HALF_BY_SLOT.values():
            was, now = _half(before, port, half), _half(after, port, half)
            key = f"{port}_timed_task_{half}"
            if now["switch"] == 1 and (was != now or clock_set):
                delay, weekday = _next_firing(int(after[RTC]), now)
                task = _TimedTask(port, select, half, int(now["weekdays"]), weekday)
                events.append(Timed(delay, key, task))
            elif was != now:
                events.append(Timed(0, key, drop_queued))
    return tuple(events)


def _next_firing(rtc: int, half: Mapping[str, Value]) -> tuple[int, int]:
    """Return the seconds to a half's next hh:mm on its weekdays, and that weekday.

    A once half (weekdays 0) fires at its next hh:mm: the firmware keeps its
    date off the wire, which the emulator doesn't.
    """
    day, now = divmod(rtc, DAY)
    today = (day + RTC_DAY0_WEEKDAY) % WEEK
    days = int(half["weekdays"]) & EVERY_DAY or EVERY_DAY
    at = int(half["hour"]) * 3600 + int(half["minute"]) * 60
    return next(
        (ahead * DAY + at - now, (today + ahead) % WEEK)
        for ahead in range(WEEK + 1)
        if ahead * DAY + at > now and days >> (today + ahead) % WEEK & 1
    )


@dataclass(frozen=True)
class _TimedTask:
    """A timed-task half firing: its port switched on (start) or off (end).

    Firing is firmware-only (the per-minute run, ``schedule_tick_push_0308``):
    ``0302`` reports the switch if it changed (the A2345's switch watcher sends
    on a change; assumed the same for the A91B2). A once half is then disabled
    and reported in ``0308`` with en 00; a repeating one fires again on its
    next weekday.

    Attributes:
        port: The port's name.
        select: Its ``a2`` port number.
        half: ``start`` or ``end``.
        weekdays: Its weekday mask, bit 0 Monday; 0 for once.
        weekday: The weekday it fires on, 0 Monday.

    """

    port: str
    select: int
    half: str
    weekdays: int
    weekday: int

    def __call__(
        self,
        values: Mapping[str, Value],  # noqa: ARG002  # reads the whole state
        state: Mapping[str, Value],
    ) -> Step:
        """Return the firing's step: the switch, its pushes, the next firing."""
        on = self.half == "start"
        relay = f"{self.port}_relay_switch"
        port = container("a2", TYPE_UI, ui(1).build(self.select))
        frames = []
        if bool(state[relay]) != on:
            switch = container("a3", TYPE_UI, ui(1).build(on))
            frames.append(timed_push(0x302, {"a2": port, "a3": switch}, state))
        if not self.weekdays & EVERY_DAY:
            enable = f"{self.port}_timed_task_{self.half}_switch"
            record = _half(state, self.port, self.half) | {"switch": 0}
            fields = {
                "a2": port,
                "a3": container("a3", TYPE_UI, ui(1).build(SLOT_BY_HALF[self.half])),
                "a4": container("a4", TYPE_BIN, ScheduleHalf.build(record)),
            }
            frames.append(timed_push(0x308, fields, state))
            return Step(values={relay: on, enable: 0}, frames=tuple(frames))
        ahead = next(
            ahead
            for ahead in range(1, WEEK + 1)
            if self.weekdays >> (self.weekday + ahead) % WEEK & 1
        )
        then = replace(self, weekday=(self.weekday + ahead) % WEEK)
        key = f"{self.port}_timed_task_{self.half}"
        return Step(
            values={relay: on},
            frames=tuple(frames),
            then=(Timed(ahead * DAY, key, then),),
        )


def drop_queued(
    values: Mapping[str, Value],  # noqa: ARG001  # replaces a pending event
    state: Mapping[str, Value],  # noqa: ARG001
) -> Step:
    """Do nothing: this event only replaces the one queued under its key."""
    return Step()


@dataclass(frozen=True)
class _Countdown:
    """One second of a port's countdown: the remaining time down one, or expiry.

    The expiry is firmware-only (``usbc_countdown_tick`` / ``ac_countdown_tick``):
    the port switches off, ``0302`` reports it if it was on (the A2345's switch
    watcher sends on a change; assumed the same for the A91B2), then ``0307``
    disarms, with the remaining time a ``4209`` disarm would leave.

    Attributes:
        port: The port's name.
        select: Its ``a2`` port number.
        clears: Expiry zeroes the remaining time; otherwise it is the seconds set.
        remaining_first: ``0307`` carries the remaining time before the seconds.

    """

    port: str
    select: int
    clears: bool
    remaining_first: bool

    def __call__(
        self,
        values: Mapping[str, Value],  # noqa: ARG002  # reads the whole state
        state: Mapping[str, Value],
    ) -> Step:
        """Return this second's step: the next tick, or the expiry."""
        remaining = f"{self.port}_countdown_task_remaining_seconds"
        left = int(state[remaining]) - 1
        if left > 0:
            key = f"{self.port}_countdown"
            return Step(
                values={remaining: left}, then=(Timed(COUNTDOWN_TICK, key, self),)
            )
        seconds = state[f"{self.port}_countdown_task_seconds"]
        record = {
            "countdown_task_switch": False,
            "countdown_task_seconds": seconds,
            "countdown_task_remaining_seconds": 0 if self.clears else seconds,
        }
        relay = f"{self.port}_relay_switch"
        port = container("a2", TYPE_UI, ui(1).build(self.select))
        frames = []
        if state[relay]:
            off = container("a3", TYPE_UI, ui(1).build(0))
            frames.append(timed_push(0x302, {"a2": port, "a3": off}, state))
        codec = (
            CountdownRecordRemainingFirst if self.remaining_first else CountdownRecord
        )
        a3 = container("a3", TYPE_BIN, codec.build(record))
        frames.append(timed_push(0x307, {"a2": port, "a3": a3}, state))
        return Step(
            values={relay: False} | {f"{self.port}_{n}": v for n, v in record.items()},
            frames=tuple(frames),
        )


def countdown_push(
    port_by_select: Mapping[int, str], *, remaining_first: bool
) -> PushBuilder:
    """Return the ``0307`` builder: a port's countdown record when it arms or disarms.

    Re-arming an armed countdown draws none. With ``remaining_first`` the
    record carries the remaining time before the seconds.
    """
    codec = CountdownRecordRemainingFirst if remaining_first else CountdownRecord

    def countdown_edge(
        request: ParameterDict,
        before: Mapping[str, Value],
        after: Mapping[str, Value],
    ) -> ParameterDict | None:
        port = port_by_select[decode_field(request, "a2", ui(1))]
        switch = f"{port}_countdown_task_switch"
        if bool(before[switch]) == bool(after[switch]):
            return None
        record = {name: after[f"{port}_{name}"] for name in COUNTDOWN_FIELDS}
        return ParameterDict(
            {"a2": request["a2"], "a3": container("a3", TYPE_BIN, codec.build(record))}
        )

    return countdown_edge
