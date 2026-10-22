# Copyright (c) 2026 Shawn Stricker
"""What the Prime A2345 and A91B2 share: their pushes, port blocks and setters.

PiField/docs/re/prime-a2345-a91b2.md "4a00 snapshot" and "Commands". The app
logs no property names for these devices, so fields take its names for the same
thing on the A1783 (``pd1OutputPower``, ``usb1OutputStatus``) and its Prime
handler names (``setACPortTimedTask``, ``setACPortCountdownTask``,
``setDCRelaySwitch``, ``setPortPriority``): reference/app-names.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from construct import Flag, If, Struct, this

from anker_ble_emulator.layouts import TYPE_BIN, TYPE_UI, ui
from anker_ble_emulator.mcu import Burst
from anker_ble_emulator.parameters import (
    FieldGroup,
    ParameterDict,
    container,
    decode_field,
    decode_request,
    leaf_zeros,
)
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

SWITCH = OptionsValidator(ui(1), 0, 1)
#: ``4203 a2``: the screen timeout, the same enum as the snapshot's.
SCREEN_TIMEOUT = OptionsValidator(ui(1), 0, 1, 2, 3, 4)
#: ``4208 a3``: 1 sets the start half of a timed task, 0 the end half.
SCHEDULE_SLOT = OptionsValidator(ui(1), 0, 1)
HALF_BY_SLOT = {1: "start", 0: "end"}
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
                "a3": container("a3", TYPE_UI, bytes([other_slot])),
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
