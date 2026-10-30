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
    Nibble,
    Padding,
    Struct,
)

from anker_ble_emulator.layouts import STATUS_ACCEPTED, TYPE_UI, ui
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    FieldGroup,
    FieldSpec,
    ParameterDict,
    Parameters,
    TableLayout,
    container,
    decode_field,
    decode_request,
    echo,
    leaf_zeros,
    one_field,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product
from anker_ble_emulator.validators import OptionsValidator

from .base import Advert, ProductDevice, Profile, register
from .prime import (
    MSGTYPE_303,
    MSGTYPE_A00,
    PORT_ECHOES,
    PORT_GROUPS,
    REALTIME_BURST,
    SCHEDULE_HEAD,
    SCHEDULE_HEAD_FIELDS,
    SWITCH,
    Countdown,
    ac_port_countdown_task,
    ac_port_timed_task,
    countdown_push,
    countdown_timers,
    schedule_timers,
    set_screen_timeout,
    timed_task_conflict,
    timed_task_push,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator.layouts import Value
    from anker_ble_emulator.timed import Timed


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
#: The USB-C ports: a disarm zeroes their remaining time (every recorded
#: snapshot; launches/2026-09-09_210425.106__3.23.0.log:5164).
USB_C = frozenset({"pd1", "pd2", "pd3", "pd4"})
#: ``4205 a2``'s highest theme index (themes 0-2).
LAST_THEME = 2
#: ``4204 a2``: the firmware keeps the low nibble.
Brightness = BitStruct(Padding(4), "screen_brightness" / Nibble)
#: ``4206 a2``: mode 0 or 1; mode 1 takes submode 0-2.
ChargingRequest = Struct(
    "charging_mode" / OptionsValidator(ui(1), 0, 1),
    "charging_submode"
    / ExprValidator(ui(1), lambda obj, ctx: ctx.charging_mode != 1 or obj <= 2),  # noqa: PLR2004
)
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
    request: ParameterDict, state: Mapping[str, Value]
) -> ParameterDict:
    """``4a02``: the byte ``4205`` writes (0x08008ba4), then the clock.

    The device writes its own clock; the request's stands in for it.
    """
    packed = ClockTheme.build(
        {
            "screen_saver_switch": state["screen_saver_switch"],
            "screen_saver_theme": state["screen_saver_theme"],
        }
    )
    clock = {"fe": request["fe"]} if "fe" in request else {}
    return ParameterDict({"a2": container("a2", TYPE_UI, packed)} | clock)


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
    """Return the charging mode/submode if ``a2``'s values are accepted."""
    parsed = decode_field(request, "a2", ChargingRequest)
    return {
        "charging_mode": parsed.charging_mode,
        "charging_submode": parsed.charging_submode,
    }


def _set_relay_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Return a port's switch (its record's ``[0]``), if ``a2`` and ``a3`` fit."""
    name = PORT_BY_SELECT[decode_field(request, "a2", PORT_SELECT)]
    return {f"{name}_relay_switch": bool(decode_field(request, "a3", SWITCH))}


def _ac_disarm_restores_remaining(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Copy an AC countdown's seconds into its remaining time on disarm (0x08004f3c)."""
    name = PORT_BY_SELECT[decode_field(request, "a2", PORT_SELECT)]
    if decode_field(request, "a3", Countdown).switch or name in USB_C:
        return {}
    seconds = state[f"{name}_countdown_task_seconds"]
    return {f"{name}_countdown_task_remaining_seconds": seconds}


def _set_time_format(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock time format if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("time_format", SWITCH)})


def _set_ac_light_mode(request: ParameterDict) -> dict[str, Value] | None:
    """Return the AC light mode if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("ac_light_mode", SWITCH)})


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
            0x209: ac_port_countdown_task(PORT_BY_SELECT, disarm_clears=USB_C),
            0x210: _set_time_format,
            0x214: _set_ac_light_mode,
        }
    )
    ECHOES = MappingProxyType(PORT_ECHOES)
    READ_REPLIES = MappingProxyType({0x202: _screensaver_read})
    SIDE_EFFECTS = MappingProxyType(
        {
            0x208: timed_task_conflict(PORT_BY_SELECT),
            0x209: _ac_disarm_restores_remaining,
        }
    )
    #: ``0302`` follows every ``4207`` (``uploadACPortSwitchState``, after every
    #: captured ``0a07``); ``0307`` a countdown arming or disarming; ``0308`` a
    #: timed-task half a ``4208`` collision disabled (doc "MCU pushes").
    FOLLOW_UPS = MappingProxyType(
        {
            0x207: ((0x302, echo("a2", "a3")),),
            0x208: ((0x308, timed_task_push(PORT_BY_SELECT)),),
            0x209: ((0x307, countdown_push(PORT_BY_SELECT, remaining_first=True)),),
        }
    )
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
        """Return the events a change starts: each port's countdown and timed tasks."""
        return (
            *countdown_timers(
                PORT_BY_SELECT, before, after, disarm_clears=USB_C, remaining_first=True
            ),
            *schedule_timers(PORT_BY_SELECT, before, after),
        )


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
    clock_lead=CLOCK_LEAD,
)
register(Product.A91B2, PROFILE)


class A91B2(ProductDevice):
    """Prime Charging Station 240W: plain outer, ECDH, CBC session, auth mode 0.

    It authorizes at the key exchange; ``0027`` answers and then drops the link.
    """

    PRODUCT = Product.A91B2

    @property
    def sw_version(self) -> str:
        """The module firmware version, read from the current ``0a00`` snapshot.

        Each digit of the raw ``a2`` int, dot-joined -- same derivation as
        SolixBLE's ``C300.software_version``, not a 4-part byte/nibble field.
        """
        snapshot = self.mcu.respond(0x200, values=self.negotiated_module.values)[0]
        params = Parameters.parse(snapshot.payload)
        return ".".join(str(decode_field(params, "a2", SW_VERSION)))
