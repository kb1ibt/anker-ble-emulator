# Copyright (c) 2026 Shawn Stricker
"""Prime Charger 250W (A2345)."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from construct import (
    Array,
    BitsInteger,
    BitStruct,
    Bytes,
    ExprValidator,
    Flag,
    Padding,
    Struct,
)

from anker_ble_emulator.layouts import STATUS_ACCEPTED, TYPE_BIN, TYPE_SILE, TYPE_UI, ui
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    FieldGroup,
    FieldSpec,
    ParameterContainer,
    ParameterDict,
    TableLayout,
    container,
    decode_field,
    decode_request,
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
    ac_port_countdown_task,
    ac_port_timed_task,
    countdown_push,
    set_screen_timeout,
    timed_task_conflict,
    timed_task_push,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator.layouts import Value


# --- Telemetry: 0303 and 0a00 ------------------------------------------------

SCHEDULE_FIELDS = (*SCHEDULE_HEAD_FIELDS, "port_priority")
Schedule = Struct(*SCHEDULE_HEAD, "port_priority" / ui(1))
#: 0a00-only per-port switch, tasks and priority. pd1-pd4 each have their own
#: slot; the two USB-A ports share a single slot ("4a00 snapshot" doc: "per
#: port (usbc 1-4, usba)" -- a genuine asymmetry, not a modeling gap).
SCHEDULES = FieldGroup(
    Schedule,
    leaf_zeros(*SCHEDULE_FIELDS),
    {"pd1": "aa", "pd2": "ab", "pd3": "ac", "pd4": "ad", "usb": "ae"},
)

#: ``af``: screensaver switch and theme, 0a00-only. The 0x80|index convention
#: in the 4205 set command matches the switch (bit7) + theme (low bits); the
#: holiday switch is a separate bit, set only by 420f (PiField/docs/re/
#: prime-a2345-a91b2.md "Commands" 4205/420f).
Theme = BitStruct(
    "screen_saver_switch" / Flag,
    "holiday_screensaver_switch" / Flag,
    "screen_saver_theme" / BitsInteger(6),
    "screen_saver_theme_id" / BitsInteger(32, swapped=True),
)
#: ``4a02``, the ``4202`` read: ``a2`` the screensaver switch, holiday switch
#: and theme (bit 7 / 6 / low 6), ``a3`` the theme id (doc "Commands" ``4202``),
#: ``af``'s own names, so one value sets both; ``a6`` the flag ``4222`` sets.
MSGTYPE_A02 = 0xA02
ScreensaverRead = BitStruct(
    "screen_saver_switch" / Flag,
    "holiday_screensaver_switch" / Flag,
    "screen_saver_theme" / BitsInteger(6),
)
SCREENSAVER_READ_FIELDS: dict[str, FieldSpec] = {
    "a2": (
        ScreensaverRead,
        0x01,
        leaf_zeros(
            "screen_saver_switch", "holiday_screensaver_switch", "screen_saver_theme"
        ),
    ),
    "a3": one_field("screen_saver_theme_id", ui(4), 0x04),
    "a6": one_field("port_widgets_switch", ui(1), 0x01),
}
#: ``b9``: screensaver active-hours window, 0a00-only; the app names none of it.
SCREENSAVER_FIELDS = (
    "screensaver_start_hour",
    "screensaver_start_minute",
    "screensaver_end_hour",
    "screensaver_end_minute",
    "screensaver_weekdays",
)
Screensaver = Struct(*(name / ui(1) for name in SCREENSAVER_FIELDS))
#: ``b8``: the custom (mode 5) charge profile, 7 B, as ``030d a3`` carries it.
CustomChargeProfile = Struct("custom_charge_profile" / Bytes(7))
#: The 0a00-only single blocks, and its plain one-byte (type 0x01) scalars.
SNAPSHOT_FIELDS: dict[str, FieldSpec] = {
    "af": (
        Theme,
        0x04,
        leaf_zeros(
            "screen_saver_switch",
            "holiday_screensaver_switch",
            "screen_saver_theme",
            "screen_saver_theme_id",
        ),
    ),
    "b8": (CustomChargeProfile, 0x04, leaf_zeros(custom_charge_profile=bytes(7))),
    "b9": (Screensaver, 0x04, leaf_zeros(*SCREENSAVER_FIELDS)),
    **{
        tag: one_field(name, ui(1), 0x01)
        for tag, name in {
            "b0": "screen_timeout",
            "b1": "charging_mode",
            "b3": "screen_brightness",
            "b4": "button_rotate_direction",
            "b5": "time_format",
            "bb": "compatibility_switch",
            "bf": "equipment_identification_switch",
        }.items()
    },
}


# --- Commands (fid 0x0f) ------------------------------------------------------
# PiField/docs/re/prime-a2345-a91b2.md "Commands (fid 0x0f, table 0x00072250,
# 36 slots)": every command below has a captured or firmware-documented
# payload shape. 4201 (factory reset), 4202 (a read), 420a (region/owner) and
# 4212 (holiday theme schedule) have no handler here: a recording or the
# command table answers them, and the profile's factory_reset runs 4201's
# reset. 4206 mode 5 (custom charge profile) isn't modeled.

#: Port index 0-3 = pd1-pd4; 4 = usb (combined, per the doc's own "open" note
#: on that mapping) -- shared by 4207/4208/4209's port selector.
PORT_BY_SELECT = {0: "pd1", 1: "pd2", 2: "pd3", 3: "pd4", 4: "usb"}
PORT_SELECT = OptionsValidator(ui(1), *PORT_BY_SELECT)
CHARGING_MODE = OptionsValidator(ui(1), 0, 1, 2, 3, 4)
#: ``4205 a2``: the screensaver switch (bit 7) over the theme (bits 0-5).
ScreensaverRequest = BitStruct(
    "screen_saver_switch" / Flag, Padding(1), "screen_saver_theme" / BitsInteger(6)
)
#: ``420c a2``: the 4-bit pd1-pd4 designation mask.
PRIORITY_MASK = ExprValidator(ui(1), lambda obj, _: obj <= 0x0F)  # noqa: PLR2004
#: ``b1`` Port Priority, the mode ``420c`` enters, and the per-port priority bytes.
PORT_PRIORITY_MODE = 2
UNSET = 0
NORMAL = 1
DESIGNATED = 2


def _output_ports(port_select: int) -> tuple[str, ...]:
    """Return the output port(s) a port selector's switch affects.

    The one combined USB-A selector drives both telemetry ports (a8/a9); every
    other selector names exactly one.
    """
    name = PORT_BY_SELECT[port_select]
    return ("usb1", "usb2") if name == "usb" else (name,)


def _set_screen_brightness(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screen brightness: no confirmed clamp, stores raw."""
    return decode_request(request, {"a2": ("screen_brightness", ui(1))})


def _set_screensaver(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screensaver switch, its theme and the ``a3`` theme id.

    Switching it off ignores the theme (the stored one stays) and clears the
    holiday switch.
    """
    packed = decode_field(request, "a2", ScreensaverRequest)
    if not packed.screen_saver_switch:
        return {"screen_saver_switch": False, "holiday_screensaver_switch": False}
    return {
        "screen_saver_switch": True,
        "screen_saver_theme": packed.screen_saver_theme,
    } | decode_request(request, {"a3": ("screen_saver_theme_id", ui(4))})


def _set_holiday_screensaver(request: ParameterDict) -> dict[str, Value] | None:
    """Return the holiday screensaver switch if ``a2`` is 0 or 1."""
    return {"holiday_screensaver_switch": bool(decode_field(request, "a2", SWITCH))}


def _set_charging_mode(request: ParameterDict) -> dict[str, Value] | None:
    """Return the charging mode for modes 0-4; mode 5's profile is unmodeled."""
    mode = decode_field(request, "a2", CHARGING_MODE)
    return {"charging_mode": mode} if mode else {}


def _set_dc_relay_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Return a port's output status, if ``a2`` (port) and ``a3`` (state) fit."""
    port_select = decode_field(request, "a2", PORT_SELECT)
    switch = decode_field(request, "a3", SWITCH)
    return {f"{name}_output_status": switch for name in _output_ports(port_select)}


def _set_port_priority(request: ParameterDict) -> dict[str, Value] | None:
    """Enter Port Priority mode; each pd port's priority byte from the mask."""
    mask = decode_field(request, "a2", PRIORITY_MASK)
    changes: dict[str, Value] = {"charging_mode": PORT_PRIORITY_MODE}
    for i in range(4):
        changes[f"pd{i + 1}_port_priority"] = DESIGNATED if mask & (1 << i) else NORMAL
    return changes


def _leave_port_priority(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Clear every pd port's priority byte when a ``4206`` leaves Port Priority."""
    mode = decode_field(request, "a2", CHARGING_MODE)
    if state["charging_mode"] != PORT_PRIORITY_MODE or mode in {0, PORT_PRIORITY_MODE}:
        return {}
    return {f"pd{i + 1}_port_priority": UNSET for i in range(4)}


def _designated_ports(state: Mapping[str, Value]) -> int:
    """Return the designated-port mask: bit i set where usbc i+1 is designated."""
    return sum(
        1 << i for i in range(4) if state[f"pd{i + 1}_port_priority"] == DESIGNATED
    )


def _switch_change(
    request: ParameterDict, before: Mapping[str, Value], after: Mapping[str, Value]
) -> ParameterDict | None:
    """``0302``: a port's switch, when a ``4207`` changed it (doc "MCU pushes")."""
    ports = _output_ports(decode_field(request, "a2", PORT_SELECT))
    name = "{}_output_status"
    if all(before[name.format(p)] == after[name.format(p)] for p in ports):
        return None
    return ParameterDict({"a2": request["a2"], "a3": request["a3"]})


def _charging_mode_change(
    request: ParameterDict,  # noqa: ARG001  # a change watcher: state only
    before: Mapping[str, Value],
    after: Mapping[str, Value],
) -> ParameterDict | None:
    """``030d``: the mode, ``b8`` and the designated ports, on a mode change."""
    if before["charging_mode"] == after["charging_mode"]:
        return None
    profile = {"custom_charge_profile": after["custom_charge_profile"]}
    return ParameterDict(
        {
            "a2": container("a2", TYPE_UI, ui(1).build(after["charging_mode"])),
            "a3": container("a3", TYPE_BIN, CustomChargeProfile.build(profile)),
            "a4": container("a4", TYPE_UI, bytes([_designated_ports(after)])),
        }
    )


def _designated_ports_change(
    request: ParameterDict,  # noqa: ARG001  # a change watcher: state only
    before: Mapping[str, Value],
    after: Mapping[str, Value],
) -> ParameterDict | None:
    """``030f``: the designated-port mask, when it changed."""
    mask = _designated_ports(after)
    if _designated_ports(before) == mask:
        return None
    return ParameterDict({"a3": container("a3", TYPE_UI, bytes([mask]))})


def _set_button_rotate_direction(request: ParameterDict) -> dict[str, Value] | None:
    """Return the knob direction if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("button_rotate_direction", SWITCH)})


def _set_time_format(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock time format if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("time_format", SWITCH)})


def _set_custom_charge_mode_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Accept the custom charge-mode switch (0 or 1); no telemetry field shows it."""
    decode_field(request, "a2", SWITCH)
    return {}


def _set_screensaver_window(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screensaver active-hours window."""
    parsed = decode_field(request, "a2", Screensaver)
    return {name: parsed[name] for name in SCREENSAVER_FIELDS}


def _set_compatibility_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Return the compatibility switch if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("compatibility_switch", SWITCH)})


def _set_equipment_identification_switch(
    request: ParameterDict,
) -> dict[str, Value] | None:
    """Return the equipment-identification switch if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("equipment_identification_switch", SWITCH)})


def _set_easter_egg(request: ParameterDict) -> dict[str, Value] | None:
    """Accept an ``a2`` typed ``02`` with a value; refuse anything else (0x00043938)."""
    field = request.get("a2")
    if field is None or field.get("type") != TYPE_SILE or not field.get("value"):
        return None
    return {}


def _upload_easter_egg_trigger(
    request: ParameterDict,
    before: Mapping[str, Value],  # noqa: ARG001  # every nonzero request triggers
    after: Mapping[str, Value],  # noqa: ARG001
) -> ParameterDict | None:
    """``0305``: trigger 0, after a ``420d`` with a nonzero value (0x0001475c)."""
    if not any(request["a2"]["value"]):
        return None
    return ParameterDict({"a2": container("a2", TYPE_UI, bytes([0]))})


def _set_port_widgets_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Return the ``4a02 a6`` switch for ``a2`` 0 or 1; any other value changes nothing.

    The handler (0x0004ff84) refuses only a request without ``a2``.
    """
    if "a2" not in request:
        return None
    switch = decode_field(request, "a2", ui(1))
    return {"port_widgets_switch": switch} if switch in {0, 1} else {}


def _get_usbc_status(
    request: ParameterDict,  # noqa: ARG001  # the handler reads no request field
) -> dict[str, Value] | None:
    """Accept: ``4218`` reads the USB-C power budgets and sets nothing."""
    return {}


_IDLE_OR_ONE = (140, 100, 100, 100)
#: Each USB-C port's offered power-sharing budget in W, pd1-pd4, by the active
#: pd1-pd4 bitmap: (no USB-A active, a USB-A active). Baseline (ungrouped)
#: profile of 0x000114b0 (prime-a2345-a91b2.md "4218 field5 power-sharing budget").
POWER_SHARING_BUDGET: dict[int, tuple[tuple[int, ...], tuple[int, ...]]] = {
    **dict.fromkeys((0, 1, 2, 4, 8), (_IDLE_OR_ONE, _IDLE_OR_ONE)),
    0x3: ((140, 100, 45, 45), (100, 100, 30, 30)),
    0x5: ((140, 65, 100, 45), (100, 90, 100, 30)),
    0x6: ((140, 100, 100, 45), (100, 100, 100, 30)),
    0x7: ((140, 65, 45, 30), (100, 90, 30, 30)),
    0x9: ((140, 65, 65, 100), (100, 90, 90, 100)),
    0xA: ((140, 100, 100, 100), (100, 100, 90, 100)),
    0xB: ((140, 65, 30, 45), (100, 90, 45, 30)),
    0xC: ((140, 100, 100, 100), (100, 100, 100, 100)),
    0xD: ((140, 100, 65, 45), (100, 65, 90, 30)),
    0xE: ((100, 100, 100, 45), (70, 100, 90, 30)),
    0xF: ((100, 100, 30, 20), (70, 65, 45, 45)),
}
#: ``4a18 a2``: pd1-pd4's budgets, then their ``0303`` output statuses.
UsbcStatus = Struct(
    "power_sharing_budget" / Array(4, ui(1)), "output_status" / Array(4, ui(1))
)


def _usbc_status_read(
    request: ParameterDict,  # noqa: ARG001  # the reply carries no request field
    state: Mapping[str, Value],
) -> ParameterDict:
    """``4a18``: each pd port's power budget, then its ``0303`` output status.

    ``a2`` is 8 raw bytes with no type byte (0x0001daf0).
    """
    statuses = [state[f"pd{i + 1}_output_status"] for i in range(4)]
    bitmap = sum(1 << i for i, status in enumerate(statuses) if status)
    usba = any(state[f"usb{i}_output_status"] for i in (1, 2))
    value = UsbcStatus.build(
        {
            "power_sharing_budget": POWER_SHARING_BUDGET[bitmap][usba],
            "output_status": statuses,
        }
    )
    return ParameterDict(
        {"a2": ParameterContainer(key=b"\xa2", type=None, value=value)}
    )


#: The MCU's fid 0x0f table, 0x200-0x223, and the ops below 0x200 it handles
#: inline (prime-a2345-a91b2.md "Commands"): every real handler acks ``00``
#: where nothing recorded or modeled answers, an op past the table too. No
#: reply: the no-op stubs, ``4212`` (the theme schedule's handler sends none)
#: and the inline BLE OTA ops ``4063``-``4067``. Any other op below 0x200 is
#: dropped.
COMMAND_TABLE: dict[int, int | None] = {
    **dict.fromkeys([0x059, 0x05D, 0x072, *range(0x200, 0x224)], STATUS_ACCEPTED),
    **dict.fromkeys([*range(0x063, 0x068), 0x212, 0x215, 0x216, 0x217]),
    **dict.fromkeys(range(0x219, 0x220)),
}


class A2345Layout(TableLayout):
    """A2345's own command/telemetry code; satisfies ``LayoutProtocol``."""

    COMMANDS = MappingProxyType(
        {
            0x203: set_screen_timeout,
            0x204: _set_screen_brightness,
            0x205: _set_screensaver,
            0x206: _set_charging_mode,
            0x207: _set_dc_relay_switch,
            0x208: ac_port_timed_task(PORT_BY_SELECT),
            0x209: ac_port_countdown_task(
                PORT_BY_SELECT, disarm_clears=frozenset(PORT_BY_SELECT.values())
            ),
            0x20C: _set_port_priority,
            0x20D: _set_easter_egg,
            0x20E: _set_button_rotate_direction,
            0x20F: _set_holiday_screensaver,
            0x210: _set_time_format,
            0x211: _set_custom_charge_mode_switch,
            0x213: _set_screensaver_window,
            0x214: _set_compatibility_switch,
            0x218: _get_usbc_status,
            0x222: _set_port_widgets_switch,
            0x223: _set_equipment_identification_switch,
        }
    )
    ECHOES = MappingProxyType(PORT_ECHOES)
    SILENT = frozenset({0x222})
    READ_REPLIES = MappingProxyType({0x218: _usbc_status_read})
    SIDE_EFFECTS = MappingProxyType(
        {0x206: _leave_port_priority, 0x208: timed_task_conflict(PORT_BY_SELECT)}
    )
    #: The MCU's change watchers, by the commands that can trip them (doc "MCU
    #: pushes"): ``0302`` a switch, ``0305`` an easter-egg trigger, ``0307`` a
    #: countdown arming or disarming, ``0308`` a collision-disabled half,
    #: ``030f`` the designated ports and ``030d`` the mode, in the order the
    #: app logs them.
    FOLLOW_UPS = MappingProxyType(
        {
            0x20D: ((0x305, _upload_easter_egg_trigger),),
            0x206: (
                (0x30F, _designated_ports_change),
                (0x30D, _charging_mode_change),
            ),
            0x207: ((0x302, _switch_change),),
            0x208: ((0x308, timed_task_push(PORT_BY_SELECT)),),
            0x209: ((0x307, countdown_push(PORT_BY_SELECT, remaining_first=False)),),
            0x20C: (
                (0x30F, _designated_ports_change),
                (0x30D, _charging_mode_change),
            ),
        }
    )
    FIELDS = MappingProxyType(
        {MSGTYPE_A00: SNAPSHOT_FIELDS, MSGTYPE_A02: SCREENSAVER_READ_FIELDS}
    )
    GROUPS = MappingProxyType(
        {
            MSGTYPE_303: (PORT_GROUPS[MSGTYPE_303],),
            MSGTYPE_A00: (PORT_GROUPS[MSGTYPE_A00], SCHEDULES),
        }
    )


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
        0x202: (MSGTYPE_A02,),
        0x20A: (0xA0A,),
        0x20B: (0xA0B,),
    },
    pushes=(0x303,),
    device_version="v2.1.1.6",
    version_names=("A2345", "A2345_mcu", "A2345_esp32"),
    layout=A2345Layout(),
    burst=REALTIME_BURST,
    known_commands=COMMAND_TABLE,
    past_table_status=STATUS_ACCEPTED,
    factory_reset=0x201,
)
register(Product.A2345, PROFILE)


class A2345(ProductDevice):
    """Prime Charger 250W: encrypted outer, ECDH, owner confirmation by button.

    Its module build (v0.2.9.7) also accepts the plain outer.
    """

    PRODUCT = Product.A2345
