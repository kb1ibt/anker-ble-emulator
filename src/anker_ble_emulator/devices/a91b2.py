# Copyright (c) 2026 Shawn Stricker
"""Prime Charging Station 240W (A91B2)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING

from construct import (
    BitsInteger,
    BitStruct,
    Flag,
    Nibble,
    Padding,
    Struct,
)

from anker_ble_emulator.layouts import (
    CLOCK,
    RTC,
    RTC_SECOND_AT,
    STATUS_ACCEPTED,
    TYPE_BIN,
    TYPE_UI,
    ui,
)
from anker_ble_emulator.live import LiveSource
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    FieldGroup,
    FieldSpec,
    ParameterDict,
    Parameters,
    TableLayout,
    clock_field,
    container,
    decode_field,
    decode_request,
    leaf_zeros,
    one_field,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product
from anker_ble_emulator.timed import Step, Timed
from anker_ble_emulator.validators import OptionsValidator

from .base import Advert, ProductDevice, Profile, register
from .prime import (
    BLE_ROUTE,
    COUNTDOWN_FIELDS,
    DAY,
    EVERY_DAY,
    HALF_BY_SLOT,
    MINUTE,
    MSGTYPE_303,
    MSGTYPE_A00,
    PORT_CLOCKED,
    PORT_ECHOES,
    PORT_GROUPS,
    PUSH_ROUTES,
    REALTIME_BURST,
    RTC_DAY0_WEEKDAY,
    SCHEDULE_HEAD,
    SCHEDULE_HEAD_FIELDS,
    STATS_PERIOD,
    SWITCH,
    SYSTICK_PHASE,
    TIMED_TASK_LEADS,
    WEEK,
    WIFI_UP,
    CountdownRecordRemainingFirst,
    ac_port_timed_task,
    check_systick_phase,
    countdown_shown_events,
    countdown_shown_name,
    date_once_slots,
    date_rtc,
    every,
    grid_delay,
    hidden_set,
    latch_name,
    mqtt_routed,
    next_minute,
    once_compare,
    once_date_name,
    reaches_ble,
    schedule_slot,
    set_screen_timeout,
    slot_disabled_push,
    slot_written,
    stats_push,
    timed_push,
    timed_task_push,
    wifi_events,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator.layouts import Value


# --- Telemetry: 0303 and 0a00 ------------------------------------------------

TIMER_FIELDS = SCHEDULE_HEAD_FIELDS
Timer = Struct(*SCHEDULE_HEAD)
#: 0a00-only per-port switch and tasks: the two AC outlets and pd1-pd4.
TIMERS = FieldGroup(
    Timer,
    leaf_zeros(*TIMER_FIELDS),
    {
        "ac1": "aa",
        "ac2": "ab",
        "pd1": "b0",
        "pd2": "b1",
        "pd3": "b2",
        "pd4": "b3",
    },
)
ClockTheme = BitStruct(
    "screen_saver_switch" / Flag, "screen_saver_theme" / BitsInteger(7)
)
Display = BitStruct("screen_brightness" / Nibble, "screen_timeout" / Nibble)
Charging = Struct("charging_mode" / ui(1), "charging_submode" / ui(1))
Ota = Struct("is_ota" / ui(1), "upgrade_progress" / ui(1))

#: The 0a00-only single blocks: PiField/docs/re/prime-a2345-a91b2.md "4a00
#: snapshot" (A91B2): ac screensaver switch + theme, ad brightness/timeout,
#: ae charging mode/submode, af OTA busy/progress; b4 time format, b5 AC light.
SNAPSHOT_FIELDS: dict[str, FieldSpec] = {
    "a2": one_field("firmware_version", ui(2), 0x02),
    "ac": (ClockTheme, 0x01, leaf_zeros("screen_saver_switch", "screen_saver_theme")),
    "ad": (Display, 0x01, leaf_zeros("screen_brightness", "screen_timeout")),
    "ae": (Charging, 0x04, leaf_zeros("charging_mode", "charging_submode")),
    "af": (Ota, 0x04, leaf_zeros("is_ota", "upgrade_progress")),
    "b4": one_field("time_format", ui(1), 0x01),
    "b5": one_field("ac_light_mode", ui(1), 0x01),
}


# --- Commands (fid 0x0f) ------------------------------------------------------
# PiField/docs/re/prime-a2345-a91b2.md "Commands (fid 0x0f...)": every command
# below has a captured or firmware-confirmed shape. Commands not here (420a,
# 421b/421c OTA) have no reply to build from, so they go unanswered; the no-op
# stubs (420c-420f, 4211-4213, 4215-4217, 4219, 421a) are silent on the device
# too.


#: ``a2``'s value (which port) -> the per-port record its switch is in.
PORT_BY_SELECT = {
    0: "ac1",
    1: "ac2",
    2: "pd1",
    3: "pd2",
    4: "pd3",
    5: "pd4",
}
PORT_SELECT = OptionsValidator(ui(1), *PORT_BY_SELECT)
#: The USB-C ports: a disarm zeroes their remaining time (prime-a2345-a91b2.md,
#: the A91B2's "MCU pushes" ``0307`` row).
USB_C = frozenset({"pd1", "pd2", "pd3", "pd4"})
#: ``4205 a2``'s highest theme index (themes 0-2).
LAST_THEME = 2
#: ``4204 a2``: the firmware keeps the low nibble.
Brightness = BitStruct(Padding(4), "screen_brightness" / Nibble)
#: ``4206 a2``: ``mode16_set`` takes mode 0 (byte1 unchecked) or mode 1 with
#: sub-mode 0-2.
HIGH_POWER, LAST_SUBMODE = 1, 2
#: ``0a00 a2``: the module firmware version (type ``02``).
SW_VERSION = ui(2)


def _set_reset(
    request: ParameterDict,  # noqa: ARG001  # display_flag_set(8); the firmware ignores the payload
) -> dict[str, Value] | None:
    """Accept unconditionally: benign here (the A2345's 4201 is a factory reset)."""
    return {}


def _set_screen_brightness(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screen brightness: the firmware applies no clamp, stores raw."""
    brightness = decode_field(request, "a2", Brightness)
    return {"screen_brightness": brightness.screen_brightness}


def _get_screensaver(
    request: ParameterDict,  # noqa: ARG001  # the handler reads no request field
) -> dict[str, Value] | None:
    """Accept: ``4202`` reads the screensaver byte and sets nothing."""
    return {}


def _screensaver_read(
    request: ParameterDict,  # noqa: ARG001  # the reply reads no request field
    state: Mapping[str, Value],
) -> ParameterDict:
    """``4a02``: the byte ``4205`` writes (0x08008ba4), then the unit's clock."""
    packed = ClockTheme.build(
        {
            "screen_saver_switch": state["screen_saver_switch"],
            "screen_saver_theme": state["screen_saver_theme"],
        }
    )
    return ParameterDict({"a2": container("a2", TYPE_UI, packed)} | clock_field(state))


def _set_screensaver(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screensaver switch and theme packed in ``a2``.

    A theme past the last one is stored as the last (0x0800cb94).
    """
    packed = decode_field(request, "a2", ClockTheme)
    return {
        "screen_saver_switch": packed.screen_saver_switch,
        "screen_saver_theme": min(packed.screen_saver_theme, LAST_THEME),
    }


def _set_charging_mode(request: ParameterDict) -> dict[str, Value] | None:
    """Return the charging mode and sub-mode ``mode16_set`` stores.

    ``cmd206_set_mode16`` acks first; a mode past 1, or mode 1 with a
    sub-mode past 2, then changes nothing (doc A91B2 ``4206`` row).
    """
    parsed = decode_field(request, "a2", Charging)
    if parsed.charging_mode > HIGH_POWER or (
        parsed.charging_mode == HIGH_POWER and parsed.charging_submode > LAST_SUBMODE
    ):
        return {}
    return {
        "charging_mode": parsed.charging_mode,
        "charging_submode": parsed.charging_submode,
    }


def _set_relay_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Return a port's switch (its record's ``[0]``), if ``a2`` and ``a3`` fit."""
    name = PORT_BY_SELECT[decode_field(request, "a2", PORT_SELECT)]
    return {f"{name}_relay_switch": bool(decode_field(request, "a3", SWITCH))}


#: ``4209 a3``: the enable, then the seconds (doc A91B2 ``4209`` row).
CountdownSet = Struct("switch" / Flag, "seconds" / ui(4))


def _set_countdown(request: ParameterDict) -> dict[str, Value] | None:
    """Return a port's countdown: the enable, and the seconds into set and remaining.

    ``ac_countdown_set`` / ``usbc_countdown_set`` write them for every
    ``4209``, armed or not; a port past 5 is acked and dropped (doc A91B2
    ``4209`` row). The next timer pass settles a disarm.
    """
    select = decode_field(request, "a2", ui(1))
    countdown = decode_field(request, "a3", CountdownSet)
    if select not in PORT_BY_SELECT:
        return {}
    name = PORT_BY_SELECT[select]
    return {
        f"{name}_countdown_task_switch": countdown.switch,
        f"{name}_countdown_task_seconds": countdown.seconds,
        f"{name}_countdown_task_remaining_seconds": countdown.seconds,
    }


def _set_time_format(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock time format if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("time_format", SWITCH)})


def _set_ac_light_mode(request: ParameterDict) -> dict[str, Value] | None:
    """Return the AC light mode if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("ac_light_mode", SWITCH)})


# --- Timers: the TIM1 task scheduler (doc "Timer timing") ---------------------
# ``sched_tasks_register?`` registers every task at boot, on one phase
# (``systick_phase``); a flag-0 task first runs one period after boot. Tasks
# due at one tick run in slot order: ``ac_switch_push_0302?`` (100 ms), then
# ``timer_group_tick_150?`` (150 ms), then ``usbc_switch_push_0302?`` (100 ms).

SWITCH_TASK = 0.1
TIMER_GROUP = 0.15
AC_TASK_AFTER = 1e-6
TIMER_AFTER = 2e-6
USBC_TASK_AFTER = 3e-6
TIMER_KEY = "timer_group"
AC_PORTS = frozenset({"ac1", "ac2"})
#: ``switch_0302_debounce?``: the marking call counts 1 and the 5th sends, 4
#: periods after it.
USBC_SEND_AFTER = 4 * SWITCH_TASK
#: The RTC second interrupt steps every armed ``remaining`` (doc "Countdown
#: timing").
RTC_SECOND = 1.0


def _shadow(port: str) -> str:
    """Return the hidden name of a port's last-reported switch (``0x200000f2``)."""
    return f"_{port}_switch_shadow"


def _switch_task(select: int, state: Mapping[str, Value]) -> Timed:
    """Return a port's switch task on its next period-100 pass."""
    port = PORT_BY_SELECT[select]
    after = AC_TASK_AFTER if port in AC_PORTS else USBC_TASK_AFTER
    delay = grid_delay(state, SWITCH_TASK, after, float(state[CLOCK]))
    return Timed(delay, f"{port}_switch_task", partial(_switch_seen, select))


def _switch_seen(
    select: int,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Run a port's switch task: one off its shadow is reported or marked.

    ``ac_switch_push_0302?`` sends ``0302`` on this pass; ``usbc_switch_push_0302?``
    marks it and sends 4 periods on, a new change marking it again.
    """
    port = PORT_BY_SELECT[select]
    current = bool(state[f"{port}_relay_switch"])
    if current == bool(state.get(_shadow(port), current)):
        return Step()
    if port in AC_PORTS:
        return Step(
            values={_shadow(port): current}, frames=(_switch_push(select, state),)
        )
    send = Timed(USBC_SEND_AFTER, f"{port}_switch_send", partial(_switch_sent, select))
    return Step(values={_shadow(port): current}, then=(send,))


def _switch_sent(
    select: int,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Send a marked USB-C port's ``0302``: the shadow its last marking set.

    The pass compares before it counts, so a switch off its shadow here is
    marked again instead (doc A91B2 "MCU pushes", the USB-C shadow bullet).
    """
    port = PORT_BY_SELECT[select]
    shadow = bool(state[_shadow(port)])
    if bool(state[f"{port}_relay_switch"]) != shadow:
        return Step()
    return Step(frames=(_switch_push(select, state),))


def _switch_push(select: int, state: Mapping[str, Value]) -> tuple[int, bytes]:
    """Return ``0302``: a port and its switch."""
    on = bool(state[f"{PORT_BY_SELECT[select]}_relay_switch"])
    fields = {
        "a2": container("a2", TYPE_UI, ui(1).build(select)),
        "a3": container("a3", TYPE_UI, ui(1).build(on)),
    }
    return timed_push(0x302, fields, state)


def _switch_events(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return the switch task of each port a change switched, its shadow first set.

    A port never reported yet takes the switch it had before as its shadow.
    """
    events = []
    for select, port in PORT_BY_SELECT.items():
        relay = f"{port}_relay_switch"
        if bool(before[relay]) == bool(after[relay]):
            continue
        if _shadow(port) not in after:
            prior = bool(before[relay])
            shadow = partial(hidden_set, _shadow(port), prior, _timer_plan)
            events.append(Timed(0, _shadow(port), shadow))
        events.append(_switch_task(select, after))
    return tuple(events)


def _countdown_steps(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return each armed countdown's steps on the RTC's own seconds.

    Armed, re-armed or given a new remaining time, or with the RTC set again,
    ``remaining`` steps from the RTC's next second edge.
    """
    clock_set = before[RTC] != after[RTC]
    edge = float(after[RTC_SECOND_AT]) + RTC_SECOND - float(after[CLOCK])
    events = []
    for port in PORT_BY_SELECT.values():
        switch = f"{port}_countdown_task_switch"
        remaining = f"{port}_countdown_task_remaining_seconds"
        started = not before[switch] or before[remaining] != after[remaining]
        if after[switch] and int(after[remaining]) and (started or clock_set):
            step = partial(_countdown_step, port)
            events.append(Timed(edge, f"{port}_countdown", step))
    return tuple(events)


def _countdown_step(
    port: str,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Step a countdown's ``remaining`` down one; the timer group ends it at 0."""
    remaining = f"{port}_countdown_task_remaining_seconds"
    if not state[f"{port}_countdown_task_switch"] or not int(state[remaining]):
        return Step()
    left = int(state[remaining]) - 1
    step = Timed(RTC_SECOND, f"{port}_countdown", partial(_countdown_step, port))
    return Step(values={remaining: left}, then=(step,) if left else ())


def _timer_plan(state: Mapping[str, Value]) -> tuple[Timed, ...]:
    """Return the next timer group pass with work to do, if any."""
    now = float(state[CLOCK])
    soon = [
        due
        for port in PORT_BY_SELECT.values()
        for due in (_countdown_due(state, port), *_slots_due(state, port))
        if due is not None
    ]
    if not soon:
        return ()
    delay = grid_delay(state, TIMER_GROUP, TIMER_AFTER, max(min(soon), now))
    return (Timed(delay, TIMER_KEY, _timer_pass),)


def _disarmed_remaining(state: Mapping[str, Value], port: str) -> int | None:
    """Return the ``remaining`` a pass gives a disarmed countdown, if it changes.

    0 on USB-C, the seconds set on AC (``usbc/ac_countdown_tick``).
    """
    if state[f"{port}_countdown_task_switch"]:
        return None
    settled = 0 if port in USB_C else int(state[f"{port}_countdown_task_seconds"])
    if int(state[f"{port}_countdown_task_remaining_seconds"]) == settled:
        return None
    return settled


def _countdown_due(state: Mapping[str, Value], port: str) -> float | None:
    """Return when a port's countdown next needs the timer group.

    At once for an edge not shown yet, a 0 or a disarm to settle; else the
    RTC edge it steps to 0.
    """
    armed = bool(state[f"{port}_countdown_task_switch"])
    shown = state.get(countdown_shown_name(port))
    edge = shown is not None and bool(shown) != armed
    if edge or _disarmed_remaining(state, port) is not None:
        return float(state[CLOCK])
    if not armed:
        return None
    remaining = int(state[f"{port}_countdown_task_remaining_seconds"])
    if not remaining:
        return float(state[CLOCK])
    return float(state[RTC_SECOND_AT]) + remaining * RTC_SECOND


def _slots_due(state: Mapping[str, Value], port: str) -> list[float | None]:
    """Return when each of a port's enabled slots next needs the timer group."""
    return [_slot_due(state, port, half) for half in HALF_BY_SLOT.values()]


def _slot_due(state: Mapping[str, Value], port: str, half: str) -> float | None:
    """Return when an enabled slot next needs the timer group.

    A once slot at its date and minute; a repeating one at second 0 of its
    next minute, or, latched, at the next second, which clears the latch.
    """
    slot = schedule_slot(state, port, half)
    if slot["switch"] != 1:
        return None
    rtc = int(state[RTC])
    days = int(slot["weekdays"]) & EVERY_DAY
    at = int(slot["hour"]) * 60 + int(slot["minute"])
    if not days:
        target = date_rtc(int(state.get(once_date_name(port, half), 0)), at)
    elif state.get(latch_name(port, half)):
        target = rtc + 1
    else:
        target = next_minute(rtc, at, days)
        if target < rtc:
            target = next_minute(rtc - rtc % MINUTE + MINUTE, at, days)
    return float(state[RTC_SECOND_AT]) + target - rtc


@dataclass
class _TimerRun:
    """One ``timer_group_tick_150?`` pass at work: the state as it goes, its output.

    Attributes:
        work: The state with the pass's changes so far.
        changes: The values it set.
        frames: The pushes it sent, in order.
        later: The events it starts.

    """

    work: dict[str, Value]
    changes: dict[str, Value]
    frames: list[tuple[int, bytes]]
    later: list[Timed]

    def put(self, name: str, value: Value) -> None:
        """Set ``name`` for the rest of the pass and after it."""
        self.changes[name] = value
        self.work[name] = value

    def switch(self, select: int, *, on: bool) -> None:
        """Switch a port (``ac_switch_set`` / ``usbc_switch_set``) if that changes."""
        port = PORT_BY_SELECT[select]
        relay = f"{port}_relay_switch"
        if bool(self.work[relay]) == on:
            return
        if _shadow(port) not in self.work:
            self.put(_shadow(port), bool(self.work[relay]))
        self.put(relay, on)
        self.later.append(_switch_task(select, self.work))

    def countdown(self, select: int) -> None:
        """``ac/usbc_countdown_tick_push_0307?``: expire one at 0, then show the edge.

        At 0 the port switches off and the countdown disarms. A disarmed
        countdown's ``remaining`` is 0 on USB-C and the seconds set on AC. A
        switch that differs from the one last shown pushes ``0307`` (doc
        "Countdown timing", ``0307`` row, ``4209`` row).
        """
        port = PORT_BY_SELECT[select]
        switch = f"{port}_countdown_task_switch"
        remaining = f"{port}_countdown_task_remaining_seconds"
        if self.work[switch] and not int(self.work[remaining]):
            self.switch(select, on=False)
            self.put(switch, value=False)
        settled = _disarmed_remaining(self.work, port)
        if settled is not None:
            self.put(remaining, settled)
        armed = bool(self.work[switch])
        shown = self.work.get(countdown_shown_name(port))
        if shown is None or bool(shown) == armed:
            return
        self.put(countdown_shown_name(port), armed)
        record = {name: self.work[f"{port}_{name}"] for name in COUNTDOWN_FIELDS}
        fields = {
            "a2": container("a2", TYPE_UI, ui(1).build(select)),
            "a3": container(
                "a3", TYPE_BIN, CountdownRecordRemainingFirst.build(record)
            ),
        }
        self.frames.append(timed_push(0x307, fields, self.work))

    def slot(self, select: int, half: str) -> None:
        """``ac_timer_tick`` / ``usbc_timer_tick``: fire or end a slot.

        Doc "Schedule firing".

        A repeating one fires when its hour and minute match, the seconds read
        0 and its weekday bit is set, latched until a call that doesn't match;
        a once one fires in second 0 of its date and minute, and fired or
        passed is disabled with a ``0308``.
        """
        port = PORT_BY_SELECT[select]
        slot = schedule_slot(self.work, port, half)
        if slot["switch"] != 1:
            return
        on = half == "start"
        days = int(slot["weekdays"]) & EVERY_DAY
        if days:
            rtc = int(self.work[RTC])
            weekday = (rtc // DAY + RTC_DAY0_WEEKDAY) % WEEK
            at = int(slot["hour"]) * 60 + int(slot["minute"])
            match = (
                rtc % DAY // MINUTE == at and not rtc % MINUTE and days >> weekday & 1
            )
            latched = bool(self.work.get(latch_name(port, half)))
            if match and not latched:
                self.put(latch_name(port, half), value=True)
                self.switch(select, on=on)
            elif not match and latched:
                self.put(latch_name(port, half), value=False)
            return
        compared = once_compare(self.work, port, half, seconds=True)
        if compared < 0:
            return
        if not compared:
            self.switch(select, on=on)
        self.put(f"{port}_timed_task_{half}_switch", 0)
        self.frames.append(slot_disabled_push(select, half, slot, self.work))


def _timer_pass(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Run ``timer_group_tick_150?``: both countdown ticks, then both timer ticks."""
    run = _TimerRun(dict(state), {}, [], [])
    for select in PORT_BY_SELECT:
        run.countdown(select)
    for select in PORT_BY_SELECT:
        for half in ("start", "end"):
            run.slot(select, half)
    return Step(
        values=run.changes,
        frames=tuple(run.frames),
        then=(*run.later, *_timer_plan(run.work)),
    )


#: The MCU's fid 0x0f table, 0x200-0x21c, and the ops below 0x200 it handles
#: inline (prime-a2345-a91b2.md "Commands"): every real handler acks ``00``
#: where nothing recorded or modeled answers, an op past the table too. No
#: reply: the no-op stubs and the OTA ``421b``/``421c``. Any other op below
#: 0x200 is dropped.
COMMAND_TABLE: dict[int, int | None] = {
    **dict.fromkeys([0x059, 0x05D, 0x072, *range(0x200, 0x21D)], STATUS_ACCEPTED),
    **dict.fromkeys([*range(0x20C, 0x210), *range(0x211, 0x214)]),
    **dict.fromkeys([*range(0x215, 0x21D)]),
}


class A91B2Layout(TableLayout):
    """A91B2's own command/telemetry code; satisfies ``LayoutProtocol``."""

    COMMANDS = MappingProxyType(
        {
            0x201: _set_reset,
            0x202: _get_screensaver,
            0x203: set_screen_timeout,
            0x204: _set_screen_brightness,
            0x205: _set_screensaver,
            0x206: _set_charging_mode,
            0x207: _set_relay_switch,
            0x208: ac_port_timed_task(PORT_BY_SELECT),
            0x209: _set_countdown,
            0x210: _set_time_format,
            0x214: _set_ac_light_mode,
        }
    )
    ECHOES = MappingProxyType(PORT_ECHOES)
    CLOCKED = PORT_CLOCKED
    LEADING = MappingProxyType(TIMED_TASK_LEADS)
    READ_REPLIES = MappingProxyType({0x202: _screensaver_read})
    SIDE_EFFECTS = MappingProxyType(
        {
            0x208: slot_written(PORT_BY_SELECT),
        }
    )
    #: ``0308``: a timed-task half a ``4208`` collision disabled (doc "MCU
    #: pushes"). A ``4207``'s ``0302`` comes from the port's switch task and a
    #: ``4209``'s ``0307`` from the next timer group pass, as every one does.
    FOLLOW_UPS = MappingProxyType({0x208: ((0x308, timed_task_push(PORT_BY_SELECT)),)})
    FIELDS = MappingProxyType({MSGTYPE_A00: SNAPSHOT_FIELDS})
    GROUPS = MappingProxyType(
        {
            MSGTYPE_303: (PORT_GROUPS[MSGTYPE_303],),
            MSGTYPE_A00: (PORT_GROUPS[MSGTYPE_A00], TIMERS),
        }
    )

    def timed(
        self, before: Mapping[str, Value], after: Mapping[str, Value]
    ) -> tuple[Timed, ...]:
        """Return the events a change starts.

        Those are countdown steps and shown switches, once-slot dates, the next
        timer group pass, the switch tasks of switched ports, and WiFi's.
        """
        return (
            *_countdown_steps(before, after),
            *countdown_shown_events(PORT_BY_SELECT, before, after, _timer_plan),
            *date_once_slots(PORT_BY_SELECT, before, after, _timer_plan),
            *_timer_plan(after),
            *_switch_events(before, after),
            *wifi_events(before, after, PERIODICS),
        )

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names a frame reads; the snapshot the hidden SysTick phase."""
        names = super().names(msgtype)
        return names | {SYSTICK_PHASE} if msgtype == MSGTYPE_A00 else names

    def resolve(
        self, values: Mapping[str, Value], state: Mapping[str, Value]
    ) -> dict[str, Value]:
        """Return what ``values`` set, the SysTick phase checked.

        Raises:
            TypeError: If the SysTick phase is outside 0-3 s or not a number.

        """
        check_systick_phase(values)
        return super().resolve(values, state)

    def sends(self, msgtype: int, values: Mapping[str, Value]) -> bool:
        """Whether a push reaches the BLE link: with WiFi up, most go to MQTT."""
        return reaches_ble(ROUTES, msgtype, values)

    def mqtt_routed(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> tuple[tuple[int, bytes], ...]:
        """Return a push's MQTT copies in the unit's link state (doc "Push routing")."""
        return mqtt_routed(ROUTES, msgtype, payload, values)


#: Its MQTT-only periodic while WiFi is up: ``0300`` stats, power unrounded
#: (doc "``state_info``"); it has no ``0312``.
PERIODICS = (every(STATS_PERIOD, "stats_0300", partial(stats_push, step=1)),)
#: Its push routes: the A2345's, but ``0308`` goes to ``34`` with WiFi up
#: (doc "Push routing").
ROUTES = {**PUSH_ROUTES, 0x308: (BLE_ROUTE, (0x34,))}

#: ``esp_op09_set_clock`` sets the clock and the RTC one second past the UTC it
#: is given (doc fid 0x10 op ``0x09``).
CLOCK_LEAD = 1

PROFILE = Profile(
    serial="AFYJTB0000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_9_7,
    auth_mode=AuthMode.OPEN,
    advert=Advert(
        local_name=None,
        version_code=0x01,
        bind_type=0x00,
        product_type=bytes.fromhex("b401"),
        sku=b"JTB",
        capability=0x00,
        prime_name=True,
    ),
    data=("a91b2.json",),
    replies={
        0x200: (0xA00,),
        0x20A: (0xA0A,),
        0x20B: (0xA0B,),
    },
    pushes=(0x303,),
    device_version="v1.1.2.4",
    version_names=("A91B2", "A91B2_mcu", "A91B2_esp32"),
    # The MCU's fid 0x0f handlers, no-op stubs left out.
    known_commands=COMMAND_TABLE,
    past_table_status=STATUS_ACCEPTED,
    layout=A91B2Layout(),
    burst=REALTIME_BURST,
    live=LiveSource("a91b2_live.json", REALTIME_BURST.period),
    clock_lead=CLOCK_LEAD,
)
register(Product.A91B2, PROFILE)


class A91B2(ProductDevice):
    """Prime Charging Station 240W: plain outer, ECDH, CBC session, auth mode 0.

    It authorizes at the key exchange; ``0027`` answers and then drops the link.
    """

    PRODUCT = Product.A91B2

    def set_wifi(self, up: bool) -> None:  # noqa: FBT001  # a switch
        """Bring the unit's WiFi up or down (link-state bit 1, doc "Push routing").

        With it up most pushes go to MQTT, not the BLE link; coming up sends
        ``0301``.
        """
        self.unit_event("wifi_up" if up else "wifi_down", **{WIFI_UP: up})

    @property
    def sw_version(self) -> str:
        """The module firmware version, read from the current ``0a00`` snapshot.

        Each digit of the raw ``a2`` int, dot-joined -- same derivation as
        SolixBLE's ``C300.software_version``, not a 4-part byte/nibble field.
        """
        snapshot = self.mcu.respond(0x200, values=self.negotiated_module.values)[0]
        params = Parameters.parse(snapshot.payload)
        return ".".join(str(decode_field(params, "a2", SW_VERSION)))
