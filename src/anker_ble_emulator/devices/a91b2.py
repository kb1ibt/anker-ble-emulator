# Copyright (c) 2026 Shawn Stricker
"""Prime Charging Station 240W (A91B2)."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from construct import (
    BitsInteger,
    BitStruct,
    ExprValidator,
    Flag,
    If,
    Nibble,
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

TIMER_FIELDS = (
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
)
Timer = Struct(
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
)
ClockTheme = BitStruct("clock_switch" / Flag, "theme_index" / BitsInteger(7))
Display = BitStruct("display_brightness" / Nibble, "display_timeout_mode" / Nibble)
Charging = Struct("charging_mode" / ui(1), "charging_submode" / ui(1))
Ota = Struct("ota_busy" / ui(1), "ota_progress" / ui(1))

#: 0a00-only single-instance fields: tag -> (codec, its type byte, its own
#: field names -- these are the final leaf names, no prefix).
#: PiField/docs/re/prime-a2345-a91b2.md "4a00 snapshot" (A91B2): ac clock
#: display + theme index, ad brightness/timeout, ae charging mode/submode,
#: af OTA busy/progress.
SINGLE_FIELDS = {
    "ac": (ClockTheme, 0x01, ("clock_switch", "theme_index")),
    "ad": (Display, 0x01, ("display_brightness", "display_timeout_mode")),
    "ae": (Charging, 0x04, ("charging_mode", "charging_submode")),
    "af": (Ota, 0x04, ("ota_busy", "ota_progress")),
}
#: 0a00-only plain scalars: tag -> leaf name. ``b4`` clock format, ``b5`` AC LED.
SCALAR_FIELDS = {"b4": "clock_format", "b5": "ac_led"}

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
TIMERS = {
    "ac_1": "aa",
    "ac_2": "ab",
    "usbc_1": "b0",
    "usbc_2": "b1",
    "usbc_3": "b2",
    "usbc_4": "b3",
}
#: All-zero defaults, for validating one named field in isolation.
_PORT_ZERO = dict.fromkeys(PORT_FIELDS, 0)
_TIMER_ZERO = dict.fromkeys(TIMER_FIELDS, 0)


# --- Commands (fid 0x0f) ------------------------------------------------------
# PiField/docs/re/prime-a2345-a91b2.md "Commands (fid 0x0f...)": every command
# below has a captured or firmware-confirmed shape. Commands not here (4202,
# 4218 reads; 420a, 421b/421c OTA) have no captured reply to build from, so
# they stay unanswered rather than fabricated; the noop stubs (420c-420f,
# 4211-4213, 4215-4217, 4219, 421a) are correctly silent by not being mapped.


#: ``a2``'s value (which port) -> the timer block its switch bit is in.
PORT_BY_SELECT = {
    0: "ac_1",
    1: "ac_2",
    2: "usbc_1",
    3: "usbc_2",
    4: "usbc_3",
    5: "usbc_4",
}
PORT_SELECT = OptionsValidator(ui(1), *PORT_BY_SELECT)
SWITCH = OptionsValidator(ui(1), 0, 1)
SCREEN_TIMEOUT = OptionsValidator(ui(1), 0, 1, 2, 3, 4)
#: ``4208 a3``: 1 sets the on-time half of a schedule, 0 the off-time half.
SCHEDULE_SLOT = OptionsValidator(ui(1), 0, 1)
#: ``4204 a2``: the firmware keeps the low nibble.
Brightness = BitStruct(Padding(4), "display_brightness" / Nibble)
#: ``4206 a2``: mode 0 or 1; mode 1 takes submode 0-2.
ChargingRequest = Struct(
    "charging_mode" / OptionsValidator(ui(1), 0, 1),
    "charging_submode"
    / ExprValidator(ui(1), lambda obj, ctx: ctx.charging_mode != 1 or obj <= 2),  # noqa: PLR2004
)
#: ``4208 a4``: one schedule half.
ScheduleHalf = Struct(
    "switch" / Flag, "hour" / ui(1), "minute" / ui(1), "weekdays" / ui(1)
)
#: ``4209 a3``: the countdown switch, then its seconds unless disarming.
Countdown = Struct("switch" / Flag, "seconds" / If(this.switch, ui(4)))
#: ``0a00 a2``: the module firmware version (type ``02``).
SW_VERSION = ui(2)


def _validate_4201(
    request: ParameterDict,  # noqa: ARG001  # display_flag_set(8); the firmware ignores the payload
) -> dict[str, Value] | None:
    """Accept unconditionally: benign here (the A2345's 4201 is a factory reset)."""
    return {}


def _validate_4203(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screen timeout if ``a2`` is one of its five enum values."""
    return decode_request(request, {"a2": ("display_timeout_mode", SCREEN_TIMEOUT)})


def _validate_4204(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screen brightness: the firmware applies no clamp, stores raw."""
    brightness = decode_field(request, "a2", Brightness)
    return {"display_brightness": brightness.display_brightness}


def _validate_4205(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock display switch and theme index packed in ``a2``."""
    packed = decode_field(request, "a2", ClockTheme)
    return {"clock_switch": packed.clock_switch, "theme_index": packed.theme_index}


def _validate_4206(request: ParameterDict) -> dict[str, Value] | None:
    """Return the charging mode/submode if ``a2``'s values are accepted."""
    parsed = decode_field(request, "a2", ChargingRequest)
    return {
        "charging_mode": parsed.charging_mode,
        "charging_submode": parsed.charging_submode,
    }


def _validate_4207(request: ParameterDict) -> dict[str, Value] | None:
    """Return the AC/USB-C port switch, if ``a2`` (port) and ``a3`` (state) fit."""
    name = PORT_BY_SELECT[decode_field(request, "a2", PORT_SELECT)]
    return {f"{name}_status": decode_field(request, "a3", SWITCH)}


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


def _validate_4210(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock format if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("clock_format", SWITCH)})


def _validate_4214(request: ParameterDict) -> dict[str, Value] | None:
    """Return the AC LED mode if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("ac_led", SWITCH)})


class A91B2Layout(StructFieldLayout, CommandDispatchLayout):
    """A91B2's own command/telemetry code; satisfies ``LayoutProtocol``."""

    #: Every command this device answers with real validation, by msgtype.
    COMMANDS = MappingProxyType(
        {
            0x201: _validate_4201,
            0x203: _validate_4203,
            0x204: _validate_4204,
            0x205: _validate_4205,
            0x206: _validate_4206,
            0x207: _validate_4207,
            0x208: _validate_4208,
            0x209: _validate_4209,
            0x210: _validate_4210,
            0x214: _validate_4214,
        }
    )

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names a device-wide ``set_values()`` can reach in ``msgtype``."""
        if msgtype not in (MSGTYPE_303, MSGTYPE_A00):
            return frozenset()
        ports = PORTS_0303 if msgtype == MSGTYPE_303 else PORTS_0A00
        leaves = {f"{name}_{sub}" for name in ports for sub in PORT_FIELDS}
        if msgtype == MSGTYPE_A00:
            leaves |= {f"{name}_{sub}" for name in TIMERS for sub in TIMER_FIELDS}
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

        A91B2's telemetry is always recorded, never built from zero; this
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
                        PortStatus.build({**_PORT_ZERO, sub: value})
            if msgtype != MSGTYPE_A00:
                continue
            for timer_name in TIMERS:
                for sub in TIMER_FIELDS:
                    if name == f"{timer_name}_{sub}":
                        Timer.build({**_TIMER_ZERO, sub: value})
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
            for name, tag in TIMERS.items():
                changed = {
                    s: values[f"{name}_{s}"]
                    for s in TIMER_FIELDS
                    if f"{name}_{s}" in values
                }
                if changed:
                    self._encode_field(params, tag, Timer, 0x04, **changed)
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
        0x20B: (0xA0B, 0x303),
    },
    pushes=(0x303,),
    device_version="v1.1.2.4",
    version_names=("A91B2", "A91B2_mcu", "A91B2_esp32"),
    # The MCU's fid 0x0f handlers, no-op stubs left out.
    known_commands=frozenset(range(0x200, 0x20C)) | {0x210, 0x214, 0x218, 0x21B, 0x21C},
    layout=A91B2Layout(),
)
register(Product.A91B2, PROFILE)


class A91B2(EmulatedDevice):
    """Prime Charging Station 240W: plain outer, ECDH, CBC session, auth mode 0.

    It authorizes at the key exchange; ``0027`` answers and then drops the link.
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
        """Build a Prime Charging Station 240W; see ``EmulatedDevice``."""
        super().__init__(
            Product.A91B2,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
            mtu=mtu,
        )

    @property
    def sw_version(self) -> str:
        """The module firmware version, read from the current ``0a00`` snapshot.

        Each digit of the raw ``a2`` int, dot-joined -- same derivation as
        SolixBLE's ``C300.software_version``, not a 4-part byte/nibble field.
        """
        snapshot = self.mcu.respond(0x200, values=self.negotiated_module.values)[0]
        params = Parameters.parse(snapshot.payload)
        return ".".join(str(decode_field(params, "a2", SW_VERSION)))
