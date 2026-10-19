# Copyright (c) 2026 Shawn Stricker
"""Prime Charger 250W (A2345)."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from construct import (
    BitsInteger,
    BitStruct,
    ExprValidator,
    Flag,
    If,
    Padding,
    Struct,
    this,
)

from anker_ble_emulator.layouts import PORT_FIELDS, PortStatus, ui
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    CommandDispatchLayout,
    ParameterContainer,
    ParameterDict,
    Parameters,
    StructFieldLayout,
    decode_field,
    decode_request,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport
from anker_ble_emulator.validators import OptionsValidator

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator.clock import Clock
    from anker_ble_emulator.layouts import Value

#: This device's two telemetry pushes.
MSGTYPE_303 = 0x303
MSGTYPE_A00 = 0xA00


# --- Telemetry: 0303 and 0a00 ------------------------------------------------

#: Ports share names across 0303 and 0a00, at different tags in each.
PORTS_0303 = {
    "usbc_1": "a2",
    "usbc_2": "a3",
    "usbc_3": "a4",
    "usbc_4": "a5",
    "usba_1": "a6",
    "usba_2": "a7",
}
PORTS_0A00 = {
    "usbc_1": "a4",
    "usbc_2": "a5",
    "usbc_3": "a6",
    "usbc_4": "a7",
    "usba_1": "a8",
    "usba_2": "a9",
}

SCHEDULE_FIELDS = (
    "switch",
    "start_switch",
    "start_hour",
    "start_minute",
    "start_weekdays",
    "end_switch",
    "end_hour",
    "end_minute",
    "end_weekdays",
    "timer_switch",
    "timer_seconds",
    "timer_remaining_seconds",
    "priority",
)
Schedule = Struct(
    "switch" / Flag,
    # start_switch/end_switch are 3-state (0xff "unset", not just 0/1): ui(1)
    # round-trips 0xff byte-exact, where Flag would corrupt it to 0x01 on
    # any rebuild of this tag (PiField/docs/re/prime-a2345-a91b2.md: "An
    # unset schedule half keeps 0c 00 behind its ff").
    "start_switch" / ui(1),
    "start_hour" / ui(1),
    "start_minute" / ui(1),
    "start_weekdays" / ui(1),
    "end_switch" / ui(1),
    "end_hour" / ui(1),
    "end_minute" / ui(1),
    "end_weekdays" / ui(1),
    "timer_switch" / Flag,
    "timer_seconds" / ui(4),
    "timer_remaining_seconds" / ui(4),
    "priority" / ui(1),
)
#: 0a00-only per-port schedule/countdown/priority. usbc 1-4 each have their
#: own slot; the two USB-A ports share a single slot ("4a00 snapshot" doc:
#: "per port (usbc 1-4, usba)" -- a genuine asymmetry, not a modeling gap).
SCHEDULES = {
    "usbc_1": "aa",
    "usbc_2": "ab",
    "usbc_3": "ac",
    "usbc_4": "ad",
    "usba": "ae",
}
_SCHEDULE_ZERO = dict.fromkeys(SCHEDULE_FIELDS, 0)

#: ``af``: clock/theme switch, 0a00-only. The 0x80|index convention in the
#: 4205 set command matches clock_switch (bit7) + theme_index (low bits);
#: holiday is a separate bit, set only by 420f (PiField/docs/re/
#: prime-a2345-a91b2.md "Commands" 4205/420f).
Theme = BitStruct(
    "clock_switch" / Flag,
    "holiday" / Flag,
    "theme_index" / BitsInteger(6),
    "theme_id" / BitsInteger(32, swapped=True),
)
#: ``b9``: screensaver active-hours window, 0a00-only.
SCREENSAVER_FIELDS = (
    "screensaver_start_hour",
    "screensaver_start_minute",
    "screensaver_end_hour",
    "screensaver_end_minute",
    "screensaver_weekdays",
)
Screensaver = Struct(*(name / ui(1) for name in SCREENSAVER_FIELDS))
#: 0a00-only single-instance fields: tag -> (codec, its type byte, its leaf names).
SINGLE_FIELDS = {
    "af": (Theme, 0x04, ("clock_switch", "holiday", "theme_index", "theme_id")),
    "b9": (Screensaver, 0x04, SCREENSAVER_FIELDS),
}
#: 0a00-only plain scalars, all a single byte with type 0x01.
SCALAR_FIELDS = {
    "b0": "display_timeout_mode",
    "b1": "charging_mode",
    "b3": "display_brightness",
    "b4": "knob_direction",
    "b5": "clock_format",
    "bb": "compatibility_switch",
    "bf": "device_identification_switch",
}


# --- Commands (fid 0x0f) ------------------------------------------------------
# PiField/docs/re/prime-a2345-a91b2.md "Commands (fid 0x0f, table 0x00072250,
# 36 slots)": every command below has a captured or firmware-documented
# payload shape. 4201 (factory reset), 4202/4218 (reads), 420a (region/owner,
# no telemetry effect modeled), 4211-4213's theme-schedule/custom-switch
# variants, and 4206 mode 5 (custom charge profile, byte layout not fully
# specified) stay unanswered rather than fabricated.

#: Port index 0-3 = usbc 1-4; 4 = usba (combined, per the doc's own "open"
#: note on that mapping) -- shared by 4207/4208/4209's port selector.
PORT_BY_SELECT = {0: "usbc_1", 1: "usbc_2", 2: "usbc_3", 3: "usbc_4", 4: "usba"}


def _status_names(port_select: int) -> tuple[str, ...]:
    """Return the ``PortStatus`` leaf name(s) a port selector's switch affects.

    usba's one combined selector drives both telemetry ports (a8/a9); every
    other selector names exactly one.
    """
    name = PORT_BY_SELECT[port_select]
    return ("usba_1", "usba_2") if name == "usba" else (name,)


PORT_SELECT = OptionsValidator(ui(1), *PORT_BY_SELECT)
SWITCH = OptionsValidator(ui(1), 0, 1)
SCREEN_TIMEOUT = OptionsValidator(ui(1), 0, 1, 2, 3, 4)
CHARGING_MODE = OptionsValidator(ui(1), 0, 1, 2, 3, 4)
#: ``4208 a3``: 1 sets the on-time half of a schedule, 0 the off-time half.
SCHEDULE_SLOT = OptionsValidator(ui(1), 0, 1)
#: ``4205 a2``: the clock switch (bit 7) over the theme index (bits 0-5).
ClockTheme = BitStruct(
    "clock_switch" / Flag, Padding(1), "theme_index" / BitsInteger(6)
)
#: ``4208 a4``: one schedule half.
ScheduleHalf = Struct(
    "switch" / Flag, "hour" / ui(1), "minute" / ui(1), "weekdays" / ui(1)
)
#: ``4209 a3``: the countdown switch, then its seconds unless disarming.
Countdown = Struct("switch" / Flag, "seconds" / If(this.switch, ui(4)))
#: ``420c a2``: the 4-bit usbc designation mask.
PRIORITY_MASK = ExprValidator(ui(1), lambda obj, _: obj <= 0x0F)  # noqa: PLR2004


def _validate_4203(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screen timeout if ``a2`` is one of its five enum values."""
    return decode_request(request, {"a2": ("display_timeout_mode", SCREEN_TIMEOUT)})


def _validate_4204(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screen brightness: no confirmed clamp, stores raw."""
    return decode_request(request, {"a2": ("display_brightness", ui(1))})


def _validate_4205(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock switch and theme index packed in ``a2``."""
    packed = decode_field(request, "a2", ClockTheme)
    return {"clock_switch": packed.clock_switch, "theme_index": packed.theme_index}


def _validate_420f(request: ParameterDict) -> dict[str, Value] | None:
    """Return the holiday-theme switch if ``a2`` is 0 or 1."""
    return {"holiday": bool(decode_field(request, "a2", SWITCH))}


def _validate_4206(request: ParameterDict) -> dict[str, Value] | None:
    """Return the charging mode for modes 0-4; mode 5's profile is unmodeled."""
    mode = decode_field(request, "a2", CHARGING_MODE)
    return {"charging_mode": mode} if mode else {}


def _validate_4207(request: ParameterDict) -> dict[str, Value] | None:
    """Return a port's switch state, if ``a2`` (port) and ``a3`` (state) fit."""
    port_select = decode_field(request, "a2", PORT_SELECT)
    switch = decode_field(request, "a3", SWITCH)
    return {f"{name}_status": switch for name in _status_names(port_select)}


def _validate_4208(request: ParameterDict) -> dict[str, Value] | None:
    """Return a port's on-time or off-time schedule half if its values are accepted."""
    name = PORT_BY_SELECT[decode_field(request, "a2", PORT_SELECT)]
    prefix = "start" if decode_field(request, "a3", SCHEDULE_SLOT) else "end"
    half = decode_field(request, "a4", ScheduleHalf)
    return {
        f"{name}_{prefix}_{field}": half[field]
        for field in ("switch", "hour", "minute", "weekdays")
    }


def _validate_4209(request: ParameterDict) -> dict[str, Value] | None:
    """Return a port's countdown switch, and seconds unless disarming."""
    name = PORT_BY_SELECT[decode_field(request, "a2", PORT_SELECT)]
    countdown = decode_field(request, "a3", Countdown)
    changes: dict[str, Value] = {f"{name}_timer_switch": countdown.switch}
    if countdown.switch:
        changes[f"{name}_timer_seconds"] = countdown.seconds
    return changes


def _validate_420c(request: ParameterDict) -> dict[str, Value] | None:
    """Return each usbc port's priority byte from the 4-bit designation mask."""
    mask = decode_field(request, "a2", PRIORITY_MASK)
    return {f"usbc_{i + 1}_priority": 2 if mask & (1 << i) else 1 for i in range(4)}


def _validate_420e(request: ParameterDict) -> dict[str, Value] | None:
    """Return the knob direction if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("knob_direction", SWITCH)})


def _validate_4210(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock time format if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("clock_format", SWITCH)})


def _validate_4213(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screensaver active-hours window."""
    parsed = decode_field(request, "a2", Screensaver)
    return {name: parsed[name] for name in SCREENSAVER_FIELDS}


def _validate_4214(request: ParameterDict) -> dict[str, Value] | None:
    """Return the compatibility switch if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("compatibility_switch", SWITCH)})


def _validate_4223(request: ParameterDict) -> dict[str, Value] | None:
    """Return the device-identification switch if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("device_identification_switch", SWITCH)})


class A2345Layout(StructFieldLayout, CommandDispatchLayout):
    """A2345's own command/telemetry code; satisfies ``LayoutProtocol``."""

    #: Every command this device answers with real validation, by msgtype.
    COMMANDS = MappingProxyType(
        {
            0x203: _validate_4203,
            0x204: _validate_4204,
            0x205: _validate_4205,
            0x206: _validate_4206,
            0x207: _validate_4207,
            0x208: _validate_4208,
            0x209: _validate_4209,
            0x20C: _validate_420c,
            0x20E: _validate_420e,
            0x20F: _validate_420f,
            0x210: _validate_4210,
            0x213: _validate_4213,
            0x214: _validate_4214,
            0x223: _validate_4223,
        }
    )

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names a device-wide ``set_values()`` can reach in ``msgtype``."""
        if msgtype not in (MSGTYPE_303, MSGTYPE_A00):
            return frozenset()
        ports = PORTS_0303 if msgtype == MSGTYPE_303 else PORTS_0A00
        leaves = {f"{name}_{sub}" for name in ports for sub in PORT_FIELDS}
        if msgtype == MSGTYPE_A00:
            leaves |= {f"{name}_{sub}" for name in SCHEDULES for sub in SCHEDULE_FIELDS}
            leaves |= {name for _, _, names in SINGLE_FIELDS.values() for name in names}
            leaves |= set(SCALAR_FIELDS.values())
        return frozenset(leaves)

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages with a named field matching ``name``."""
        return tuple(
            msgtype
            for msgtype in (MSGTYPE_303, MSGTYPE_A00)
            if name in self.names(msgtype)
        )

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Validate that each named value fits its field; the bytes aren't used.

        A2345's telemetry is always recorded, never built from zero; this
        exists so ``set_values()`` can check a value against its field's
        type before storing it.

        Raises:
            TypeError: If a value doesn't fit its field.

        """
        given = values or {}
        ports = PORTS_0303 if msgtype == MSGTYPE_303 else PORTS_0A00
        for name, value in given.items():
            for port_name in ports:
                for sub in PORT_FIELDS:
                    if name == f"{port_name}_{sub}":
                        PortStatus.build({**dict.fromkeys(PORT_FIELDS, 0), sub: value})
            if msgtype != MSGTYPE_A00:
                continue
            for schedule_name in SCHEDULES:
                for sub in SCHEDULE_FIELDS:
                    if name == f"{schedule_name}_{sub}":
                        Schedule.build({**_SCHEDULE_ZERO, sub: value})
            for codec, _, field_names in SINGLE_FIELDS.values():
                if name in field_names:
                    codec.build({**dict.fromkeys(field_names, 0), name: value})
            if name in SCALAR_FIELDS.values():
                ui(1).build(value)
        return b""

    def update(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> bytes:
        """Return ``payload`` with the given named port/timer fields set."""
        params = Parameters.parse(payload)
        ports = PORTS_0303 if msgtype == MSGTYPE_303 else PORTS_0A00
        for name, tag in ports.items():
            changed = {
                s: values[f"{name}_{s}"] for s in PORT_FIELDS if f"{name}_{s}" in values
            }
            if changed:
                self._encode_field(params, tag, PortStatus, 0x04, **changed)
        if msgtype == MSGTYPE_A00:
            for name, tag in SCHEDULES.items():
                changed = {
                    s: values[f"{name}_{s}"]
                    for s in SCHEDULE_FIELDS
                    if f"{name}_{s}" in values
                }
                if changed:
                    self._encode_field(params, tag, Schedule, 0x04, **changed)
            for tag, (codec, type_byte, field_names) in SINGLE_FIELDS.items():
                changed = {n: values[n] for n in field_names if n in values}
                if changed:
                    self._encode_field(params, tag, codec, type_byte, **changed)
            for tag, name in SCALAR_FIELDS.items():
                if name in values:
                    params[tag] = ParameterContainer(
                        key=bytes.fromhex(tag),
                        type=0x01,
                        value=ui(1).build(values[name]),
                    )
        return Parameters.build(params)


PROFILE = Profile(
    serial="AQLQJB0000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_9_7,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(
        local_name=None,
        version_code=0x01,
        bind_type=0x00,
        product_type=bytes.fromhex("b402"),
        sku=b"QJB",
        capability=0x04,
        prime_name=True,
    ),
    data=("a2345.json",),
    replies={
        0x200: (0xA00,),
        0x20A: (0xA0A,),
        0x20B: (0xA0B, 0x303),
    },
    pushes=(0x303,),
    device_version="v2.1.1.6",
    version_names=("A2345", "A2345_mcu", "A2345_esp32"),
    layout=A2345Layout(),
)
register(Product.A2345, PROFILE)


class A2345(EmulatedDevice):
    """Prime Charger 250W: encrypted outer, ECDH, owner confirmation by button.

    Its module build (v0.2.9.7) also accepts the plain outer.
    """

    def __init__(  # noqa: PLR0913  # the identity plus the four protocol choices
        self,
        serial: str | Unset | None = UNSET,
        mac: str = DEFAULT_MAC,
        transport: Transport | None = None,
        *,
        outer: Outer | None = None,
        path: Path | None = None,
        module: ModuleBuild | None = None,
        clock: Clock | None = None,
        mtu: int | None = None,
    ) -> None:
        """Build a Prime Charger 250W; see ``EmulatedDevice``."""
        super().__init__(
            Product.A2345,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
            mtu=mtu,
        )
