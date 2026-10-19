# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C Gen 2 line (A1763, A1765, A1783, A1785): one display-board build.

The display board answers the same commands on every model, so the MCU replies
and the ``0490`` push (schema ``charging_pps_series_c``) are shared; frames
that name the unit (``0421``, ``0900``, ``0425``) come from each model's own
recording. The push was recorded with an expansion attached; a model without
one zeroes the expansion's entries.

Commands and telemetry are hand-written from PiField/docs/re/a1783-control.md
and a1783-telemetry.md, the same approach as the Prime devices. The display
board always acks setters ``00`` (``Profile.rejects=False``): a refused value
is still accepted on the wire, and the only way to see whether it took is the
telemetry it would have changed. Confirmed against both docs plus
firmware/mainmcu.md and firmware/opcode-map-all.md: ops 0x101-0x104 answer
only through ``fid0f_ack_reply?``, which derives status from the request's
own routing byte with no ``param_err`` path, so none of this family's group
setters reject a value at all; every validator below returns ``None`` only
where the firmware documents an actual clamp (the SoC limits), never to
produce a wire-visible ``04``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, TypeAlias

from construct import (
    BitsInteger,
    BitStruct,
    Bytes,
    Const,
    Construct,
    ConstructError,
    Container,
    ExprAdapter,
    Flag,
    GreedyBytes,
    Int8ul,
    Optional,
    Padding,
    Prefixed,
    Struct,
    Terminated,
)

from anker_ble_emulator.layouts import Value, ui
from anker_ble_emulator.parameters import (
    CommandDispatchLayout,
    ParameterContainer,
    ParameterDict,
    Parameters,
    StructFieldLayout,
    decode_field,
    decode_request,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: A typed TLV sub-struct: its codec, its type byte, and each leaf's zero value.
FieldSpec: TypeAlias = "tuple[Construct[Container[Any], Any], int, Mapping[str, Value]]"


#: Recordings of the line's identity-free MCU replies and pushes.
DATA = "solix_c_gen2.json"
#: The ``0490`` push's field names.
PUSH_490_FIELDS = "c490_fields.json"

#: Reply msgtypes by request msgtype; every setter is followed by a ``0421``.
REPLIES = {
    0x057: (0x857,),
    0x05E: (0x85E, 0x421),
    0x063: (0x863,),
    0x072: (0x872,),
    0x089: (0x889,),
    0x090: (0x890, 0x421),
    0x091: (0x891, 0x421),
    0x092: (0x892, 0x421),
    0x100: (0x900, 0x421),
    0x101: (0x901, 0x421),
    0x102: (0x902, 0x421),
    0x103: (0x903, 0x421),
    0x104: (0x904, 0x421),
}

#: Pushes every model can send.
PUSHES = (0x421, 0x489, 0x490)
#: The display board's fid ``0x0f`` command table (every handled request).
KNOWN_COMMANDS = frozenset(
    {0x051, 0x057, 0x059, 0x05E, 0x063, 0x064, 0x065, 0x066, 0x072, 0x089}
    | {0x090, 0x091, 0x092, 0x100, 0x101, 0x102, 0x103, 0x104}
)
#: The display-board firmware ``0830`` reports.
DEVICE_VERSION = "v1.2.1.6"


def version_names(pn: str) -> tuple[str, str, str]:
    """Return ``0830`` ``a3``-``a5``: the OTA type names for ``pn``."""
    return f"{pn}_low", f"{pn}_mcu_low", f"{pn}_esp32_low"


# --- Telemetry: 0421 ----------------------------------------------------------

#: This device family's one telemetry message (push and ``4100`` reply alike).
MSGTYPE_421 = 0x421
#: ``0100``'s reply: a status byte, then ``0421``'s own TLV.
MSGTYPE_900 = 0x900


def _flat(
    raw: Construct[Container[Any], Any], groups: Mapping[str, tuple[str, ...]]
) -> Construct[Container[Any], Any]:
    """Promote each packed-bit group's members to the struct's flat top level."""

    def decode(obj: Container[Any], _: object) -> Container[Any]:
        out = Container(obj)
        for group in groups:
            out.update(out.pop(group))
        return out

    def encode(obj: Container[Any], _: object) -> Container[Any]:
        out = Container(obj)
        for group, names in groups.items():
            out[group] = Container({name: out.pop(name) for name in names})
        return out

    return ExprAdapter(raw, decoder=decode, encoder=encode)


def _zeros(*names: str, **overrides: Value) -> dict[str, Value]:
    """Return each leaf's zero value: 0 unless overridden (a bytes field)."""
    return {**dict.fromkeys(names, 0), **overrides}


#: ``a4`` setUpData: the AC/DC/system settings block every group command
#: (4101-4103) writes into; 26 B on MainMcu v1.2.1.1 (the A1763 recording),
#: 33 B on v1.2.1.6, which appends reserved zeros and the ``[31]`` flags
#: byte. PiField/docs/re/a1783-control.md "Telemetry from local state
#: (display board)"; byte offsets and Thom's names (anker-solix-api
#: ``_A1783_0421``) are both from that table.
SETTINGS_FIELDS = (
    "ac_output_timeout_seconds",
    "ac_input_limit",
    "ac_frequency",
    "ac_output_mode",
    "dc_output_timeout_seconds",
    "dc_12v_output_mode",
    "device_timeout_minutes",
    "display_timeout_seconds",
    "display_mode",
    "temp_unit_fahrenheit",
    "ac_fast_charge_switch",
    "display_switch",
    "port_memory_switch",
    "max_soc",
    "min_soc",
    "display_language",
    "ac_input_disable_state",
    "outage_alert_switch",
)
#: ``a4[31]`` (v1.2.1.6): the AC input disable state and the outage alert; the
#: other bits are carried as recorded.
SettingsFlags = BitStruct(
    "flags_high" / BitsInteger(4),
    "ac_input_disable_state" / BitsInteger(2),
    "outage_alert_switch" / Flag,
    "flags_low" / BitsInteger(1),
)
#: ``a4[26:33]``, appended by v1.2.1.6: reserved zeros around the flags byte.
SettingsTail = Struct(
    "reserved" / Bytes(5), "flags" / SettingsFlags, "reserved_end" / Bytes(1)
)
_RawSettings = Struct(
    "ac_output_timeout_seconds" / ui(4),
    "ac_input_limit" / ui(2),
    "ac_frequency" / ui(1),
    "ac_output_mode" / ui(1),
    "dc_output_timeout_seconds" / ui(4),
    "dc_12v_output_mode" / ui(1),
    "device_timeout_minutes" / ui(2),
    "display_timeout_seconds" / ui(2),
    "display_mode" / ui(1),
    Padding(1),
    "temp_unit_fahrenheit" / ui(1),
    "ac_fast_charge_switch" / ui(1),
    "display_switch" / ui(1),
    "port_memory_switch" / ui(1),
    "max_soc" / ui(1),
    "min_soc" / ui(1),
    "display_language" / ui(1),
    "tail" / Optional(SettingsTail),
    "rest" / GreedyBytes,
)
FLAG_ZEROS: Container[Any] = Container(
    flags_high=0, ac_input_disable_state=0, outage_alert_switch=False, flags_low=0
)
#: The tail a block built from zeros gets: the v1.2.1.6 shape, every bit clear.
TAIL_ZEROS: Container[Any] = Container(
    reserved=bytes(5), flags=FLAG_ZEROS, reserved_end=bytes(1)
)
FLAG_FIELDS = ("ac_input_disable_state", "outage_alert_switch")


def _settings_decode(obj: Container[Any], _: object) -> Container[Any]:
    """Lift the two flag fields out of the v1.2.1.6 tail; 0 on the short block."""
    out = Container(obj)
    flags: Container[Any] = out["tail"].flags if out["tail"] is not None else FLAG_ZEROS
    out.update({name: flags[name] for name in FLAG_FIELDS})
    return out


def _settings_encode(obj: Container[Any], _: object) -> Container[Any]:
    """Put the two flag fields back in the tail; a short block has nowhere for them."""
    out = Container(obj)
    values = {name: out.pop(name) for name in FLAG_FIELDS}
    out.setdefault("rest", b"")
    tail = out.get("tail", TAIL_ZEROS)
    if tail is not None:
        out["tail"] = Container(tail, flags=Container(tail.flags, **values))
    return out


Settings = ExprAdapter(_RawSettings, decoder=_settings_decode, encoder=_settings_encode)

#: ``a7``: AC output group (6 B). a1783-telemetry.md "c421/c900/0421 tags".
AcOutput = Struct(
    "ac_output_status" / ui(1),
    "ac_output_power" / ui(2),
    "ac_input_status" / ui(1),
    "ac_input_power" / ui(2),
)
#: ``b2``: 12 V car socket (3 B); only byte 0 is documented in either doc,
#: so the rest stays padding -- a genuine gap, not an unread one.
DcOutput = Struct("dc_output_status" / ui(1), Padding(2))

#: ``a5``: main-pack battery (5 B); ``[4]`` is undocumented. a1783-telemetry.md
#: "a5" table. ``battery_soh`` is only the low byte of the real value (the
#: high byte lives outside ``a5``, not reconstructable from this tag alone).
Battery = Struct(
    "temperature" / ui(1, signed=True),
    "charge_discharge_status" / ui(1),
    "battery_soc" / ui(1),
    "battery_soh" / ui(1),
    Padding(1),
)

#: ``d9`` touData (6 + 3n + 19 B): the TOU plan ``4090`` stores, the SoC
#: limits (``[3]``/``[4]`` read the same settings bytes as ``a4[23]``/``[24]``)
#: and the backup-plan tail. a1783-control.md "d9 (touData)"; names are
#: Thom's (``_A1783_0421`` ``d9`` sequence). ``tou_mode_schedule`` is the
#: count byte plus 3 B per period ``{type, start hour, end hour}``.
TOU_RECORD_SIZE = 3
#: The schedule's wire form: the count, then exactly that many periods.
TouRecords = Struct(
    "count" / ui(1),
    "records" / Bytes(lambda this: this.count * TOU_RECORD_SIZE),
    Terminated,
)


def _tou_schedule_decode(obj: Container[Any], _: object) -> bytes:
    return TouRecords.build(obj)


def _tou_schedule_encode(obj: Value, _: object) -> Container[Any]:
    msg = "tou_mode_schedule is a count byte then 3 bytes per period"
    if not isinstance(obj, bytes):
        raise TypeError(msg)
    try:
        return TouRecords.parse(obj)
    except ConstructError as error:
        raise TypeError(msg) from error


#: ``tou_mode_schedule``'s value is its wire bytes: the count and the periods.
TouSchedule = ExprAdapter(
    Struct(
        "count" / ui(1),
        "records" / Bytes(lambda this: this.count * TOU_RECORD_SIZE),
    ),
    decoder=_tou_schedule_decode,
    encoder=_tou_schedule_encode,
)
Tou = Struct(
    "active_tariff" / ui(1),
    "usage_mode" / ui(1),
    "backup_soc" / ui(1),
    "backup_charge_soc" / ui(1),
    "backup_discharge_soc" / ui(1),
    "tou_mode_schedule" / TouSchedule,
    "backup_status" / ui(1),
    "backup_switch" / ui(1),
    "storm_guard_switch" / ui(1),
    "backup_start_timestamp" / ui(4),
    "backup_end_timestamp" / ui(4),
    "auto_backup_start_timestamp" / ui(4),
    "auto_backup_end_timestamp" / ui(4),
)
TOU_ZEROS = _zeros(
    "active_tariff",
    "usage_mode",
    "backup_soc",
    "backup_charge_soc",
    "backup_discharge_soc",
    "backup_status",
    "backup_switch",
    "storm_guard_switch",
    "backup_start_timestamp",
    "backup_end_timestamp",
    "auto_backup_start_timestamp",
    "auto_backup_end_timestamp",
    tou_mode_schedule=b"\x00",
)
#: ``a4[23]``/``[24]`` and ``d9[3]``/``[4]`` read the same two settings bytes.
MIRRORS = (("max_soc", "backup_charge_soc"), ("min_soc", "backup_discharge_soc"))

#: ``da`` clock screen (23 B), the read side of ``4091``; its first 16 bytes
#: are also ``4092``'s ``a2``. a1783-control.md "Clock screen (4091 -> da)";
#: names are Thom's except ``clock_theme_id``/``clock_theme_size`` (the
#: doc's "clock theme id" / "second theme word (size)", unnamed upstream).
#: ``[1]`` is the theme download state, written only by the firmware.
_CLOCK_HEAD = (
    "theme" / BitStruct("screen_saver_switch" / Flag, "lcd_theme" / BitsInteger(7)),
    Padding(1),
    "clock_theme_id" / ui(4),
    "clock_theme_size" / ui(4),
    "lcd_time_format" / ui(1),
    "day_start_minutes" / ui(2),
    "day_end_minutes" / ui(2),
    "lcd_repeat_cycle" / ui(1),
)
_CLOCK_TAIL = (
    "day_night_split_switch" / ui(1),
    "day_brightness_level" / ui(1),
    "night_brightness_level" / ui(1),
    "night_start_minutes" / ui(2),
    "night_end_minutes" / ui(2),
)
_THEME_BITS = {"theme": ("screen_saver_switch", "lcd_theme")}
ClockScreen = _flat(Struct(*_CLOCK_HEAD, *_CLOCK_TAIL), _THEME_BITS)
ClockScreenHead = _flat(Struct(*_CLOCK_HEAD), _THEME_BITS)
CLOCK_HEAD_ZEROS = _zeros(
    "screen_saver_switch",
    "lcd_theme",
    "clock_theme_id",
    "clock_theme_size",
    "lcd_time_format",
    "day_start_minutes",
    "day_end_minutes",
    "lcd_repeat_cycle",
)
CLOCK_ZEROS = _zeros(
    *CLOCK_HEAD_ZEROS,
    "day_night_split_switch",
    "day_brightness_level",
    "night_brightness_level",
    "night_start_minutes",
    "night_end_minutes",
)

#: ``dc`` memorySwitchData (5 B): the BMS fault blocking the output-memory
#: restore and its recovery epoch; no setter. a1783-telemetry.md ``dc`` row.
MemorySwitch = Struct(
    "memory_switch_error_code" / ui(1),
    "memory_switch_error_timestamp" / ui(4),
)

#: ``f9`` DeviceVer (28 B): seven 4-byte versions, one decimal digit per
#: byte, last digit first (``06 01 02 01`` = v1.2.1.6). ``[8:12]`` is never
#: written. a1783-telemetry.md "Versions (f9)"; Thom's names, and the app's
#: ``mcuVersion`` for ``[0:4]``, which the map leaves unnamed.
VERSION_SIZE = 4
DeviceVersions = Struct(
    "mcu_version" / Bytes(VERSION_SIZE),
    "sub_mcu_version" / Bytes(VERSION_SIZE),
    Padding(VERSION_SIZE),
    "inverter_version" / Bytes(VERSION_SIZE),
    "bms_version" / Bytes(VERSION_SIZE),
    "sub_package_version" / Bytes(VERSION_SIZE),
    "module_version" / Bytes(VERSION_SIZE),
)
VERSION_NAMES = (
    "mcu_version",
    "sub_mcu_version",
    "inverter_version",
    "bms_version",
    "sub_package_version",
    "module_version",
)
VERSION_ZEROS = _zeros(**dict.fromkeys(VERSION_NAMES, bytes(VERSION_SIZE)))

#: ``fa`` configNegotiation (20 B): the capability flags, constants per
#: build; bits LSB first. a1783-telemetry.md "Capability flags (fa)"; Thom's
#: names (``unknown_0421_fa_06_b5`` included).
_RawCapabilities = Struct(
    "support_ac_energy_mode" / ui(1),
    "support_car_charge_mode" / ui(1),
    "support_parallel_upgrade" / ui(1),
    "support_get_upgrade_info" / ui(1),
    "station_protocol_version" / ui(1),
    "flags_5"
    / BitStruct(
        "support_set_country_code" / Flag,
        "support_charging_limits" / Flag,
        "support_output_status" / Flag,
        "support_screen_time_period" / Flag,
        "support_upgrade_record" / Flag,
        "support_backup_disaster" / Flag,
        "support_memory_switch" / Flag,
        "support_transaction_id" / Flag,
    ),
    "flags_6"
    / BitStruct(
        Padding(2),
        "unknown_0421_fa_06_b5" / Flag,
        "support_ac_input_disable" / Flag,
        "support_outage_alert" / Flag,
        "support_silent_recharge" / Flag,
        "memory_switch_two" / Flag,
        "support_pps_tou" / Flag,
    ),
    Padding(13),
)
_CAPABILITY_BITS = {
    "flags_5": (
        "support_set_country_code",
        "support_charging_limits",
        "support_output_status",
        "support_screen_time_period",
        "support_upgrade_record",
        "support_backup_disaster",
        "support_memory_switch",
        "support_transaction_id",
    ),
    "flags_6": (
        "unknown_0421_fa_06_b5",
        "support_ac_input_disable",
        "support_outage_alert",
        "support_silent_recharge",
        "memory_switch_two",
        "support_pps_tou",
    ),
}
Capabilities = _flat(_RawCapabilities, _CAPABILITY_BITS)
CAPABILITY_ZEROS = _zeros(
    "support_ac_energy_mode",
    "support_car_charge_mode",
    "support_parallel_upgrade",
    "support_get_upgrade_info",
    "station_protocol_version",
    *_CAPABILITY_BITS["flags_5"],
    *_CAPABILITY_BITS["flags_6"],
)

#: status + power (u16 LE), 3 B: the shape ``a8``/``aa``/``ab``/``ac``/``ae``
#: all share. a1783-telemetry.md "c421/c900/0421 tags". Kept generically
#: named (not per-tag) so one named group, not ``SINGLE_FIELDS``, can
#: prefix it per port -- the same shape ``PORTS_0303``/``PORTS_0A00``
#: use on the Prime devices.
POWER_FIELDS = ("status", "power")
PowerStatus = Struct("status" / ui(1), "power" / ui(2))
#: tag -> this port's name, for ``POWER_FIELDS``-shaped telemetry.
POWER_PORTS = {
    "a8": "dc_input",
    "aa": "pd1",
    "ab": "pd2",
    "ac": "pd3",
    "ae": "usba",
}

#: ``0421``'s typed tags on every model: tag -> (codec, type byte, leaf zeros).
SINGLE_FIELDS: dict[str, FieldSpec] = {
    "a4": (Settings, 0x04, _zeros(*SETTINGS_FIELDS)),
    "a5": (
        Battery,
        0x04,
        _zeros("temperature", "charge_discharge_status", "battery_soc", "battery_soh"),
    ),
    "a7": (
        AcOutput,
        0x04,
        _zeros(
            "ac_output_status", "ac_output_power", "ac_input_status", "ac_input_power"
        ),
    ),
    "b2": (DcOutput, 0x04, _zeros("dc_output_status")),
    "d9": (Tou, 0x04, TOU_ZEROS),
    "da": (ClockScreen, 0x04, CLOCK_ZEROS),
    "dc": (
        MemorySwitch,
        0x04,
        _zeros("memory_switch_error_code", "memory_switch_error_timestamp"),
    ),
    "f9": (DeviceVersions, 0x04, VERSION_ZEROS),
    "fa": (Capabilities, 0x04, CAPABILITY_ZEROS),
}


# --- Telemetry: the expansion port (A1783, A1785) -----------------------------

#: ``c0`` expansion block (35 B with no pack, 44 B with one): both length
#: prefixes move the later offsets. a1783-telemetry.md "Expansion block
#: (c0)"; Thom's names (``_A1783_0421`` ``c0`` sequence).
Expansion = Struct(
    "exp_1_sn" / Prefixed(Int8ul, GreedyBytes),
    "exp_1_number" / ui(1),
    "exp_1_version" / Bytes(VERSION_SIZE),
    "exp_1_temperature" / ui(1),
    "exp_1_status" / ui(1),
    "exp_1_soc" / ui(1),
    "exp_1_health" / ui(1),
    "exp_1_blink_status" / ui(1),
    "exp_1_switch_status" / ui(1),
    "exp_1_error_code" / ui(1),
    "exp_1_connection_status" / ui(1),
    "exp_1_balance_status" / ui(1),
    "exp_1_type" / Prefixed(Int8ul, GreedyBytes),
)
EXPANSION_ZEROS = _zeros(
    "exp_1_number",
    "exp_1_temperature",
    "exp_1_status",
    "exp_1_soc",
    "exp_1_health",
    "exp_1_blink_status",
    "exp_1_switch_status",
    "exp_1_error_code",
    "exp_1_connection_status",
    "exp_1_balance_status",
    exp_1_sn=b"",
    exp_1_version=bytes(VERSION_SIZE),
    exp_1_type=b"",
)
#: ``ce`` altSampData (43 B): the car-alternator charger accessory.
#: a1783-control.md "Other local-state tags" ``ce``; Thom's names.
PN_SIZE = 16
SN_SIZE = 17
Alternator = Struct(
    Const(b"\x10"),
    "device_1_pn" / Bytes(PN_SIZE),
    Const(b"\x01"),
    Const(b"\x11"),
    "device_1_sn" / Bytes(SN_SIZE),
    Padding(4),
    "device_1_mode" / ui(1),
    "device_1_output_power" / ui(2),
)
EXPANSION_FIELDS: dict[str, FieldSpec] = {
    "c0": (Expansion, 0x04, EXPANSION_ZEROS),
    "ce": (
        Alternator,
        0x04,
        _zeros(
            "device_1_mode",
            "device_1_output_power",
            device_1_pn=bytes(PN_SIZE),
            device_1_sn=bytes(SN_SIZE),
        ),
    ),
}


# --- Telemetry: the other messages --------------------------------------------

#: ``0425`` event report (push). a1783-control.md "0425 event report": ``a2``
#: the event, ``a3``/``a4`` the window's u32 epochs; a grid loss (``0x59``)
#: carries the SoC as a 1-byte ``a3`` and no ``a4``. Thom's names, and
#: ``event_soc`` for the grid-loss byte the map doesn't type.
MSGTYPE_425 = 0x425
EVENT_GRID_LOSS = 0x59
EVENT_SOC = "event_soc"
EVENT_FIELDS: dict[str, FieldSpec] = {
    "a2": (Struct("event_code" / ui(1)), 0x01, _zeros("event_code")),
    "a3": (
        Struct("event_start_timestamp" / ui(4)),
        0x04,
        _zeros("event_start_timestamp"),
    ),
    "a4": (Struct("event_end_timestamp" / ui(4)), 0x04, _zeros("event_end_timestamp")),
}
#: ``0489`` push and ``4089`` reply: OTA readiness, ``a2`` allowed and ``a3``
#: the block reason (0 / 2 low SoC / 4 "connect AC"). a1783-control.md
#: "4089"; Thom's names. ``a4``-``a6`` are the recordings' constants.
MSGTYPE_489 = 0x489
MSGTYPE_889 = 0x889
OTA_FIELDS: dict[str, FieldSpec] = {
    "a2": (Struct("ota_allowed" / ui(1)), 0x01, _zeros("ota_allowed")),
    "a3": (Struct("ota_block_reason" / ui(1)), 0x01, _zeros("ota_block_reason")),
}
#: ``4092`` reply: ``a2`` = ``da[0:16]``, ``a3`` = the theme name ``4091 a5``
#: stored (empty on every recording). a1783-control.md "4092".
MSGTYPE_892 = 0x892
LCD_THEME_NAME = "lcd_theme_name"
CLOCK_READ_FIELDS: dict[str, FieldSpec] = {
    "a2": (ClockScreenHead, 0x04, CLOCK_HEAD_ZEROS),
    "a3": (Struct(LCD_THEME_NAME / GreedyBytes), 0x04, {LCD_THEME_NAME: b""}),
}
#: Typed tags of every message but ``0421``, whose table is per layout.
MESSAGE_FIELDS: dict[int, Mapping[str, FieldSpec]] = {
    MSGTYPE_425: EVENT_FIELDS,
    MSGTYPE_489: OTA_FIELDS,
    MSGTYPE_889: OTA_FIELDS,
    MSGTYPE_892: CLOCK_READ_FIELDS,
}


# --- Commands (fid 0x0f) ------------------------------------------------------
# PiField/docs/re/a1783-control.md "Command map", "Groups 4101-4103", "TOU
# (4090)", "Clock screen (4091 -> da)" and "4104": every command below has a
# captured or firmware-documented payload shape. 4051 (restore defaults),
# 4057 (streaming countdown -- a module timer, not a telemetry field),
# 405e (backup-plan group: which table a plan lands in and when its window
# goes active depend on the clock), 4063/4064 (BLE OTA) and 4072 (daily
# update window) stay unanswered rather than guessed.


#: ``4101`` AC group: tag -> (the field it sets, its codec).
AC_GROUP = {
    "a2": ("ac_output_status", ui(1)),
    "a3": ("ac_output_timeout_seconds", ui(4)),
    "a4": ("ac_input_limit", ui(2)),
    "a5": ("ac_frequency", ui(1)),
    "a6": ("ac_output_mode", ui(1)),
    "a7": ("ac_fast_charge_switch", ui(1)),
    "ab": ("ac_input_disable_state", ui(2)),
}
#: ``4102`` DC/car group.
DC_GROUP = {
    "a2": ("dc_output_status", ui(1)),
    "a3": ("dc_output_timeout_seconds", ui(4)),
    "a4": ("dc_12v_output_mode", ui(1)),
}
#: ``4103`` system group, but for the SoC limits and the outage alert.
SYSTEM_GROUP = {
    "a2": ("display_switch", ui(1)),
    "a3": ("display_mode", ui(1)),
    "a4": ("display_timeout_seconds", ui(2)),
    "a5": ("temp_unit_fahrenheit", ui(1)),
    "a6": ("device_timeout_minutes", ui(2)),
    "a8": ("port_memory_switch", ui(1)),
    "a9": ("display_language", ui(1)),
}
SOC_LIMITS = {"aa": ("max_soc", ui(1)), "ab": ("min_soc", ui(1))}
#: ``4090`` TOU plan: ``a6`` (the period count) and ``a7`` (the periods) are read apart.
TOU_PLAN = {"a2": ("usage_mode", ui(1)), "a5": ("backup_soc", ui(1))}
#: ``4104``: ``a4`` the expansion's switch; ``a5`` (locate blink) sets no field.
EXPANSION_GROUP = {"a4": ("exp_1_switch_status", ui(1))}


def _validate_4101(request: ParameterDict) -> dict[str, Value] | None:
    """Return the AC group's changed fields; any subset of its tags may be present."""
    return decode_request(request, AC_GROUP)


def _validate_4102(request: ParameterDict) -> dict[str, Value] | None:
    """Return the DC/car group's changed fields; any subset may be present."""
    return decode_request(request, DC_GROUP)


def _soc_limits(request: ParameterDict) -> dict[str, Value]:
    """Return the max/min SoC clamp (``dev_cmd_15_soc_limits_set?``).

    max <= min resets both to 100/1; else max outside 2-100 clamps to 100
    and min = 0 clamps to 1.
    """
    limits = decode_request(request, SOC_LIMITS)
    max_soc, min_soc = limits.get("max_soc"), limits.get("min_soc")
    if max_soc is not None and min_soc is not None and max_soc <= min_soc:
        max_soc, min_soc = 100, 1
    else:
        if max_soc is not None and not 2 <= max_soc <= 100:  # noqa: PLR2004  # the firmware's own clamp bounds
            max_soc = 100
        if min_soc == 0:
            min_soc = 1
    changes: dict[str, Value] = {}
    if max_soc is not None:
        changes["max_soc"] = max_soc
    if min_soc is not None:
        changes["min_soc"] = min_soc
    return changes


def _validate_4103(request: ParameterDict) -> dict[str, Value] | None:
    """Return the system group's changed fields; any subset may be present."""
    changes = decode_request(request, SYSTEM_GROUP)
    if "aa" in request or "ab" in request:
        changes.update(_soc_limits(request))
    if "b0" in request and request["b0"]["length"] > 1:
        changes["outage_alert_switch"] = decode_field(request, "b0", Flag)
    return changes


def _validate_4090(request: ParameterDict) -> dict[str, Value] | None:
    """Return the TOU plan ``4090`` stores (``fid0f_op90_tou_set?``).

    ``a2`` the mode, ``a5`` the reserve SoC, ``a6`` the period count and
    ``a7`` the periods, copied raw; ``a3``/``a4`` are never read and nothing
    is checked. Periods past the count are dropped, missing ones read zero.
    """
    changes = decode_request(request, TOU_PLAN)
    if "a6" in request or "a7" in request:
        periods = decode_field(request, "a7", GreedyBytes) if "a7" in request else b""
        count = (
            decode_field(request, "a6", ui(1))
            if "a6" in request
            else len(periods) // TOU_RECORD_SIZE
        )
        size = count * TOU_RECORD_SIZE
        records = Bytes(size).parse(periods + bytes(size))
        changes["tou_mode_schedule"] = TouRecords.build(
            {"count": count, "records": records}
        )
    return changes


#: ``4091`` tag -> (the ``da`` leaf it writes, its width): a1783-control.md
#: "Clock screen (4091 -> da)".
CLOCK_TAGS = {
    "a3": ("clock_theme_id", ui(4)),
    "a4": ("clock_theme_size", ui(4)),
    "a5": (LCD_THEME_NAME, GreedyBytes),
    "a7": ("lcd_time_format", ui(1)),
    "a8": ("day_start_minutes", ui(2)),
    "a9": ("day_end_minutes", ui(2)),
    "aa": ("lcd_repeat_cycle", ui(1)),
    "ab": ("day_night_split_switch", ui(1)),
    "ac": ("day_brightness_level", ui(1)),
    "ad": ("night_brightness_level", ui(1)),
    "ae": ("night_start_minutes", ui(2)),
    "af": ("night_end_minutes", ui(2)),
}
#: ``4091 a2``: the screensaver switch (bit 7) over the clock theme.
ClockTheme = BitStruct("screen_saver_switch" / Flag, "lcd_theme" / BitsInteger(7))


def _validate_4091(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock-screen fields ``4091`` writes; the app sends only what changed.

    ``a2`` packs the screensaver switch (bit 7) over the theme; ``a5`` is the
    theme name ``4092`` returns; ``a6`` (theme data) sets no telemetry.
    """
    changes = decode_request(request, CLOCK_TAGS)
    if "a2" in request:
        theme = decode_field(request, "a2", ClockTheme)
        changes["screen_saver_switch"] = theme.screen_saver_switch
        changes["lcd_theme"] = theme.lcd_theme
    return changes


def _validate_4104(request: ParameterDict) -> dict[str, Value] | None:
    """Return the expansion's switch state; ``a5`` (locate blink) changes no field.

    The real pack re-closes an "off" within ~2 s (``contactor_fsm?``); the
    emulator reports the commanded state.
    """
    return decode_request(request, EXPANSION_GROUP)


#: Every command the line answers with real validation, by msgtype.
LINE_COMMANDS: dict[int, Callable[[ParameterDict], dict[str, Value] | None]] = {
    0x090: _validate_4090,
    0x091: _validate_4091,
    0x101: _validate_4101,
    0x102: _validate_4102,
    0x103: _validate_4103,
}


def _mirrored(values: Mapping[str, Value]) -> dict[str, Value]:
    """Return ``values`` with each SoC limit set in both tags that carry it."""
    out = dict(values)
    for a4_name, d9_name in MIRRORS:
        if a4_name in out and d9_name not in out:
            out[d9_name] = out[a4_name]
        elif d9_name in out and a4_name not in out:
            out[a4_name] = out[d9_name]
    return out


def _shape_grid_loss(params: ParameterDict, values: Mapping[str, Value]) -> None:
    """Give a grid-loss report its 1-byte ``a3`` (the SoC) and drop ``a4``."""
    if decode_field(params, "a2", ui(1)) != EVENT_GRID_LOSS:
        return
    params["a3"] = ParameterContainer(
        key=b"\xa3", type=0x04, value=ui(1).build(values.get(EVENT_SOC, 0))
    )
    params.pop("a4", None)


class CGen2Layout(StructFieldLayout, CommandDispatchLayout):
    """The C Gen 2 line's own command/telemetry code; satisfies ``LayoutProtocol``."""

    COMMANDS = MappingProxyType(LINE_COMMANDS)
    #: ``0421``'s typed tags; a chassis with an expansion port adds its own.
    FIELDS_421: ClassVar[Mapping[str, FieldSpec]] = MappingProxyType(SINGLE_FIELDS)

    def _fields(self, msgtype: int) -> Mapping[str, FieldSpec]:
        if msgtype in (MSGTYPE_421, MSGTYPE_900):
            return self.FIELDS_421
        return MESSAGE_FIELDS.get(msgtype, {})

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names a device-wide ``set_values()`` can reach in ``msgtype``."""
        leaves = {
            name for _, _, zeros in self._fields(msgtype).values() for name in zeros
        }
        if msgtype in (MSGTYPE_421, MSGTYPE_900):
            leaves |= {
                f"{name}_{sub}" for name in POWER_PORTS.values() for sub in POWER_FIELDS
            }
        if msgtype == MSGTYPE_425:
            leaves.add(EVENT_SOC)
        return frozenset(leaves)

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages with a named field matching ``name``."""
        return tuple(
            msgtype
            for msgtype in (MSGTYPE_421, MSGTYPE_900, *MESSAGE_FIELDS)
            if name in self.names(msgtype)
        )

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Validate that each named value fits its field; the bytes aren't used.

        This device's telemetry is always recorded, never built from zero;
        this exists so ``set_values()`` can check a value against its
        field's type before storing it.

        Raises:
            TypeError: If a value doesn't fit its field.

        """
        for name, value in (values or {}).items():
            for codec, _, zeros in self._fields(msgtype).values():
                if name in zeros:
                    codec.build({**zeros, name: value})
            if msgtype in (MSGTYPE_421, MSGTYPE_900):
                for port_name in POWER_PORTS.values():
                    for sub in POWER_FIELDS:
                        if name == f"{port_name}_{sub}":
                            PowerStatus.build(
                                {**dict.fromkeys(POWER_FIELDS, 0), sub: value}
                            )
            if msgtype == MSGTYPE_425 and name == EVENT_SOC:
                ui(1).build(value)
        return b""

    def update(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> bytes:
        """Return ``payload`` with the given named fields set."""
        fields = self._fields(msgtype)
        if not fields:
            return payload
        values = _mirrored(values)
        params = Parameters.parse(payload)
        for tag, (codec, type_byte, zeros) in fields.items():
            changed = {n: values[n] for n in zeros if n in values}
            if changed:
                self._encode_field(params, tag, codec, type_byte, **changed)
        if msgtype in (MSGTYPE_421, MSGTYPE_900):
            for tag, name in POWER_PORTS.items():
                changed = {
                    s: values[f"{name}_{s}"]
                    for s in POWER_FIELDS
                    if f"{name}_{s}" in values
                }
                if changed:
                    self._encode_field(params, tag, PowerStatus, 0x04, **changed)
        if msgtype == MSGTYPE_425:
            _shape_grid_loss(params, values)
        return Parameters.build(params)


class ExpansionTelemetry(CommandDispatchLayout):
    """The expansion port's telemetry (``c0``, ``ce``) and its switch (``4104``).

    Mixed in ahead of ``CGen2Layout`` on the chassis with the port (A1783,
    A1785), which emit both tags with or without a pack attached; the C1000
    Gen 2 chassis emits neither.
    """

    FIELDS_421: ClassVar[Mapping[str, FieldSpec]] = MappingProxyType(
        {**SINGLE_FIELDS, **EXPANSION_FIELDS}
    )
    COMMANDS = MappingProxyType({**LINE_COMMANDS, 0x104: _validate_4104})


class CGen2ExpansionLayout(ExpansionTelemetry, CGen2Layout):
    """The C2000 Gen 2 / C2000X Gen 2 layout: the line's plus the expansion port's."""
