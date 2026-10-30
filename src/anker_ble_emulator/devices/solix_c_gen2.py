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

from enum import IntEnum
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from construct import (
    Array,
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
    GreedyRange,
    Int8ul,
    Optional,
    Padded,
    Padding,
    Prefixed,
    Struct,
    Terminated,
    this,
)

from anker_ble_emulator.c_g2_490 import MSGTYPE_490
from anker_ble_emulator.layouts import (
    STATUS_ACCEPTED,
    TYPE_BIN,
    TYPE_SILE,
    TYPE_UI,
    TYPE_VAR,
    Value,
    at_most,
    fitted,
    ui,
)
from anker_ble_emulator.mcu import CLOUD, EPOCH, RTC
from anker_ble_emulator.messages import (
    ROUTE_APP,
    ROUTE_BLE,
    ROUTE_LAYOUT,
    ROUTE_PARAM_INFO,
    ROUTE_STATE_INFO,
)
from anker_ble_emulator.parameters import (
    FieldGroup,
    FieldSpec,
    ParameterContainer,
    ParameterDict,
    Parameters,
    TableLayout,
    container,
    decode_field,
    decode_request,
    flat,
    leaf_zeros,
    one_field,
    struct_zeros,
)
from anker_ble_emulator.timed import Step, Timed


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


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
#: The display board's fid ``0x0f`` command table (a1783-control.md "Reply
#: ``a1`` by opcode", "Command map"): the ``fid0f_ack_reply?`` ``00`` where
#: nothing recorded answers. ``4064`` (before an OTA start) replies nothing.
#: Unlisted ops are dropped.
KNOWN_COMMANDS: dict[int, int | None] = {
    **dict.fromkeys(
        {0x051, 0x057, 0x059, 0x05E, 0x063, 0x065, 0x066, 0x072, 0x089, 0x090},
        STATUS_ACCEPTED,
    ),
    **dict.fromkeys({0x091, 0x092}, STATUS_ACCEPTED),
    **dict.fromkeys({0x100, 0x101, 0x102, 0x103, 0x104}, STATUS_ACCEPTED),
    0x064: None,
}
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


#: ``a4`` setUpData: the AC/DC/system settings block every group command
#: (4101-4103) writes into; 26 B through MainMcu v1.2.1.1 (the A1763 recording
#: on v1.1.2.6, the A1785's on v1.2.1.1), 33 B on v1.2.1.6, which appends
#: reserved zeros and the ``[31]`` flags byte. PiField/docs/re/a1783-control.md
#: "Telemetry from local state (display board)" for the offsets; names are the
#: app's (reference/app-names.md).
SETTINGS_FIELDS = (
    "ac_timer_value",
    "ac_recharge_power",
    "ac_frequency",
    "ac_energy_saving_switch",
    "car_charger_timer_value",
    "car_charger_energy_saving_switch",
    "device_standby_time",
    "screen_off_time",
    "lcd_backlight_brightness",
    "temperature_unit",
    "super_fast_charge_status",
    "screen_switch",
    "memory_switch",
    "charging_limit_value",
    "discharge_limit_value",
    "device_language",
    "ac_input_disable_switch",
    "outage_alert_switch",
)
#: ``a4[31]`` (v1.2.1.6): the AC input disable state and the outage alert; the
#: other bits are carried as recorded.
SettingsFlags = BitStruct(
    "flags_high" / BitsInteger(4),
    "ac_input_disable_switch" / BitsInteger(2),
    "outage_alert_switch" / Flag,
    "flags_low" / BitsInteger(1),
)
#: ``a4[26:33]``, appended by v1.2.1.6: reserved zeros around the flags byte.
SettingsTail = Struct(
    "reserved" / Bytes(5), "flags" / SettingsFlags, "reserved_end" / Bytes(1)
)
_RawSettings = Struct(
    "ac_timer_value" / ui(4),
    "ac_recharge_power" / ui(2),
    "ac_frequency" / ui(1),
    "ac_energy_saving_switch" / ui(1),
    "car_charger_timer_value" / ui(4),
    "car_charger_energy_saving_switch" / ui(1),
    "device_standby_time" / ui(2),
    "screen_off_time" / ui(2),
    "lcd_backlight_brightness" / ui(1),
    Padding(1),
    "temperature_unit" / ui(1),
    "super_fast_charge_status" / ui(1),
    "screen_switch" / ui(1),
    "memory_switch" / ui(1),
    "charging_limit_value" / ui(1),
    "discharge_limit_value" / ui(1),
    "device_language" / ui(1),
    "tail" / Optional(SettingsTail),
    "rest" / GreedyBytes,
)
FLAG_ZEROS: Container[Any] = Container(
    flags_high=0, ac_input_disable_switch=0, outage_alert_switch=False, flags_low=0
)
#: The tail a block built from zeros gets: the v1.2.1.6 shape, every bit clear.
TAIL_ZEROS: Container[Any] = Container(
    reserved=bytes(5), flags=FLAG_ZEROS, reserved_end=bytes(1)
)
FLAG_FIELDS = ("ac_input_disable_switch", "outage_alert_switch")


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
#: ``a6`` mainData (9 B): the port totals, the time left and the main pack's
#: SoC. a1783-telemetry.md ``a6`` rows. Each quantity another tag also carries
#: (``a7[4:6]``, ``a8[1:3]``, ``a5[2]``) shares its name, so one value sets both.
MainData = Struct(
    "total_output_power" / ui(2),
    "ac_input_power" / ui(2),
    "pv1_input_power" / ui(2),
    "remaining_time" / ui(2),
    "battery_level" / ui(1),
)
#: ``b2``: 12 V car socket (3 B), its status and output power (u16 LE, W);
#: a1783-telemetry.md ``b2`` rows. Names are the app's.
DcOutput = Struct(
    "car_charge1_output_status" / ui(1), "car_charge1_output_power" / ui(2)
)

#: ``a5``: main-pack battery (5 B). a1783-telemetry.md "a5" table. ``health``
#: is the low byte of the SoH; ``[4]`` is its high byte, always 0 because the
#: firmware truncates the SoH to a byte first.
Battery = Struct(
    "temperature" / ui(1, signed=True),
    "charge_discharge_status" / ui(1),
    "battery_level" / ui(1),
    "health" / ui(1),
    Padding(1),
)

#: ``a3`` StatusData: 10 B on v1.1.2.6 (the A1763 recording), 13 B from v1.2.1.1
#: (the A1785's) on, which appends the silent recharge power and the weak-light
#: lock. a1783-telemetry.md ``a3``
#: rows; names are the app's.
STATUS_TAIL_FIELDS = ("silent_recharge_power", "weak_light_lock_flag")
StatusTail = Struct("silent_recharge_power" / ui(2), "weak_light_lock_flag" / ui(1))
STATUS_TAIL_ZEROS: Container[Any] = Container(
    silent_recharge_power=0, weak_light_lock_flag=0
)
_RawStatus = Struct(
    "work_status" / ui(1),
    "error_code" / ui(1),
    "package_count" / ui(1),
    "package_use_count" / ui(1),
    "max_input_power" / ui(2),
    "upgrade_progress" / ui(1),
    "wifi_signal_strength" / ui(1),
    "mtu_size" / ui(2),
    "tail" / Optional(StatusTail),
)
STATUS_FIELDS = (
    "work_status",
    "error_code",
    "package_count",
    "package_use_count",
    "max_input_power",
    "upgrade_progress",
    "wifi_signal_strength",
    "mtu_size",
    *STATUS_TAIL_FIELDS,
)


def _status_decode(obj: Container[Any], _: object) -> Container[Any]:
    """Lift the v1.2.1.6 tail's fields; 0 on the short block."""
    out = Container(obj)
    tail: Container[Any] = out["tail"] or STATUS_TAIL_ZEROS
    out.update({name: tail[name] for name in STATUS_TAIL_FIELDS})
    return out


def _status_encode(obj: Container[Any], _: object) -> Container[Any]:
    """Put the tail's fields back; a short block has nowhere for them."""
    out = Container(obj)
    values = {name: out.pop(name) for name in STATUS_TAIL_FIELDS}
    if out.get("tail", STATUS_TAIL_ZEROS) is not None:
        out["tail"] = Container(values)
    return out


Status = ExprAdapter(_RawStatus, decoder=_status_decode, encoder=_status_encode)
#: ``fd``: the ``fd`` string of the last request that carried one, at most 15 B.
TRANSACTION_ID_SIZE = 15
TransactionId = Struct("transaction_id" / GreedyBytes)

#: ``d9`` touData (6 + 3n + 19 B): the TOU plan ``4090`` stores, the SoC
#: limits (``[3]``/``[4]`` read the same settings bytes as ``a4[23]``/``[24]``)
#: and the backup-plan tail. a1783-control.md "d9 (touData)"; names are the
#: app's, which reads ``[3]``/``[4]`` under the ``a4`` limits' own names, so
#: one value sets both tags. ``tou_periods`` is the count byte plus 3 B per
#: period ``{type, start hour, end hour}``.
TOU_RECORD_SIZE = 3
#: The most periods a plan holds: the app's limit ("A maximum of 6 segments
#: can be set") and the display's. A ``4090`` counting more is refused.
TOU_PERIODS_MAX = 6
#: The plan's record buffer: six periods. A longer ``4090 a7`` overruns the
#: handler's stack buffer, so it is refused.
TOU_RECORDS_SIZE = TOU_PERIODS_MAX * TOU_RECORD_SIZE
#: The schedule's wire form: the count, then exactly that many periods.
TouRecords = Struct(
    "count" / ui(1),
    "records" / Bytes(lambda this: this.count * TOU_RECORD_SIZE),
    Terminated,
)
#: The plan ``4090`` stores (T at ``0x20003efe``, from T[1]): the raw count,
#: then the record buffer ``a7`` is copied over, zero past what was written.
#: The app has no name for it; ``d9``'s ``tou_periods`` is read from it.
TOU_TABLE = "tou_period_table"
TouTable = Struct(
    "count" / ui(1),
    "records" / fitted(TOU_RECORDS_SIZE),
    Terminated,
)
#: The record buffer as ``4090 a7`` overwrites it: the bytes ``a7`` covers,
#: then the stored rest.
TouOverwrite = Struct("covered" / Bytes(this._.covered_size), "kept" / GreedyBytes)


def _tou_schedule_decode(obj: Container[Any], _: object) -> bytes:
    return TouRecords.build(obj)


def _tou_schedule_encode(obj: Value, _: object) -> Container[Any]:
    msg = "tou_periods is a count byte then 3 bytes per period"
    if not isinstance(obj, bytes):
        raise TypeError(msg)
    try:
        return TouRecords.parse(obj)
    except ConstructError as error:
        raise TypeError(msg) from error


#: ``tou_periods``'s value is its wire bytes: the count and the periods.
TouSchedule = ExprAdapter(
    Struct(
        "count" / ui(1),
        "records" / Bytes(lambda this: this.count * TOU_RECORD_SIZE),
    ),
    decoder=_tou_schedule_decode,
    encoder=_tou_schedule_encode,
)
Tou = Struct(
    "tou_system_status" / ui(1),
    "backup_mode" / ui(1),
    "tou_reserve_power" / ui(1),
    "charging_limit_value" / ui(1),
    "discharge_limit_value" / ui(1),
    "tou_periods" / TouSchedule,
    "tou_setting_system_status" / ui(1),
    "manual_backup_switch" / ui(1),
    "auto_backup_switch" / ui(1),
    "manual_backup_start_time" / ui(4),
    "manual_backup_end_time" / ui(4),
    "backup_start_time" / ui(4),
    "backup_end_time" / ui(4),
)
TOU_ZEROS = leaf_zeros(
    "tou_system_status",
    "backup_mode",
    "tou_reserve_power",
    "charging_limit_value",
    "discharge_limit_value",
    "tou_setting_system_status",
    "manual_backup_switch",
    "auto_backup_switch",
    "manual_backup_start_time",
    "manual_backup_end_time",
    "backup_start_time",
    "backup_end_time",
    tou_periods=b"\x00",
)
#: ``da`` clock screen (23 B), the read side of ``4091``; its first 16 bytes
#: are also ``4092``'s ``a2``. a1783-control.md "Clock screen (4091 -> da)";
#: names are the app's. ``[1]`` is the theme download state, written only by
#: the firmware.
_CLOCK_HEAD = (
    "theme" / BitStruct("screen_saver_switch" / Flag, "lcd_theme" / BitsInteger(7)),
    Padding(1),
    "network_theme_id" / ui(4),
    "network_theme_crc" / ui(4),
    "lcd_time_format" / ui(1),
    "lcd_start_time" / ui(2),
    "lcd_end_time" / ui(2),
    "lcd_repeat_cycle" / ui(1),
)
_CLOCK_TAIL = (
    "day_night_switch" / ui(1),
    "day_brightness_level" / ui(1),
    "night_brightness_level" / ui(1),
    "night_start_time" / ui(2),
    "night_end_time" / ui(2),
)
_THEME_BITS = {"theme": ("screen_saver_switch", "lcd_theme")}
ClockScreen = flat(Struct(*_CLOCK_HEAD, *_CLOCK_TAIL), _THEME_BITS)
ClockScreenHead = flat(Struct(*_CLOCK_HEAD), _THEME_BITS)
CLOCK_HEAD_ZEROS = leaf_zeros(
    "screen_saver_switch",
    "lcd_theme",
    "network_theme_id",
    "network_theme_crc",
    "lcd_time_format",
    "lcd_start_time",
    "lcd_end_time",
    "lcd_repeat_cycle",
)
CLOCK_ZEROS = leaf_zeros(
    *CLOCK_HEAD_ZEROS,
    "day_night_switch",
    "day_brightness_level",
    "night_brightness_level",
    "night_start_time",
    "night_end_time",
)

#: ``dc`` memorySwitchData (5 B): the BMS fault blocking the output-memory
#: restore and its recovery epoch; no setter. a1783-telemetry.md ``dc`` row.
MemorySwitch = Struct(
    "memory_switch_error_code" / ui(1),
    "memory_switch_error_timestamp" / ui(4),
)

#: ``f9`` DeviceVer (28 B): seven 4-byte versions, one decimal digit per
#: byte, last digit first (``06 01 02 01`` = v1.2.1.6). ``[8:12]`` is never
#: written. a1783-telemetry.md "Versions (f9)"; names are the app's, but for
#: ``[20:24]``, which it doesn't name.
VERSION_SIZE = 4
DeviceVersions = Struct(
    "mcu_version" / Bytes(VERSION_SIZE),
    "sub_mcu_version" / Bytes(VERSION_SIZE),
    Padding(VERSION_SIZE),
    "inv_version" / Bytes(VERSION_SIZE),
    "bms_version" / Bytes(VERSION_SIZE),
    "sub_package_version" / Bytes(VERSION_SIZE),
    "module_version" / Bytes(VERSION_SIZE),
)
VERSION_NAMES = (
    "mcu_version",
    "sub_mcu_version",
    "inv_version",
    "bms_version",
    "sub_package_version",
    "module_version",
)
VERSION_ZEROS = leaf_zeros(**dict.fromkeys(VERSION_NAMES, bytes(VERSION_SIZE)))
#: One ``f9`` version as the integer its digits spell (``06 01 02 01`` -> 1216).
VersionNumber: Construct[int, int] = ExprAdapter(
    Array(VERSION_SIZE, Int8ul),
    decoder=lambda digits, _: sum(d * 10**i for i, d in enumerate(digits)),
    encoder=lambda number, _: [number // 10**i % 10 for i in range(VERSION_SIZE)],
)

#: ``fa`` configNegotiation (20 B): the capability flags, constants per
#: build; bits LSB first. a1783-telemetry.md "Capability flags (fa)"; names
#: are the app's, but for ``[6]`` bit 5, which it doesn't name.
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
        "unknown_capability_6_5" / Flag,
        "support_ac_input_disable" / Flag,
        "support_outage_alert" / Flag,
        "support_silent_recharge" / Flag,
        "memory_switch_two" / Flag,
        "support_ppstou" / Flag,
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
        "unknown_capability_6_5",
        "support_ac_input_disable",
        "support_outage_alert",
        "support_silent_recharge",
        "memory_switch_two",
        "support_ppstou",
    ),
}
Capabilities = flat(_RawCapabilities, _CAPABILITY_BITS)
CAPABILITY_ZEROS = leaf_zeros(
    "support_ac_energy_mode",
    "support_car_charge_mode",
    "support_parallel_upgrade",
    "support_get_upgrade_info",
    "station_protocol_version",
    *_CAPABILITY_BITS["flags_5"],
    *_CAPABILITY_BITS["flags_6"],
)

#: ``aa``/``ab``/``ac``/``ae``: one output port each, its status and power
#: (u16 LE). a1783-telemetry.md "c421/c900/0421 tags".
OUTPUT_FIELDS = ("output_status", "output_power")
OutputPort = Struct("output_status" / ui(1), "output_power" / ui(2))
#: The output ports, by the app's port name: port -> its tag.
OUTPUT_PORTS = FieldGroup(
    OutputPort,
    leaf_zeros(*OUTPUT_FIELDS),
    {"pd1": "aa", "pd2": "ab", "pd3": "ac", "usb1": "ae"},
)
#: ``a8``: the one DC input (solar, car socket or alternator), the app's ``pv1``.
DcInput = Struct("pv1_charge_status" / ui(1), "pv1_input_power" / ui(2))

#: ``a2`` basicData (32 B after type ``06``): the protocol version, the
#: length-prefixed SN, then the PN, MainMcu firmware and model variant, carried
#: as recorded. a1783-telemetry.md ``a2`` row; the SN is the app's ``deviceSn``.
BasicData = Struct(
    "_protocol_version" / Bytes(1),
    "device_sn" / Prefixed(Int8ul, GreedyBytes),
    "_rest" / GreedyBytes,
)

#: ``0421``'s typed tags on every model: tag -> (codec, type byte, leaf zeros).
SINGLE_FIELDS: dict[str, FieldSpec] = {
    "a2": (
        BasicData,
        0x06,
        leaf_zeros(device_sn=b"", _protocol_version=bytes(1), _rest=b""),
    ),
    "a3": (Status, 0x04, leaf_zeros(*STATUS_FIELDS)),
    "a4": (Settings, 0x04, leaf_zeros(*SETTINGS_FIELDS)),
    "a5": (
        Battery,
        0x04,
        leaf_zeros("temperature", "charge_discharge_status", "battery_level", "health"),
    ),
    "a6": (MainData, 0x04, struct_zeros(MainData)),
    "a7": (
        AcOutput,
        0x04,
        leaf_zeros(
            "ac_output_status", "ac_output_power", "ac_input_status", "ac_input_power"
        ),
    ),
    "a8": (DcInput, 0x04, leaf_zeros("pv1_charge_status", "pv1_input_power")),
    "b2": (DcOutput, 0x04, struct_zeros(DcOutput)),
    "d9": (Tou, 0x04, TOU_ZEROS),
    "da": (ClockScreen, 0x04, CLOCK_ZEROS),
    "dc": (
        MemorySwitch,
        0x04,
        leaf_zeros("memory_switch_error_code", "memory_switch_error_timestamp"),
    ),
    "f9": (DeviceVersions, 0x04, VERSION_ZEROS),
    "fa": (Capabilities, 0x04, CAPABILITY_ZEROS),
    "fd": (TransactionId, 0x00, leaf_zeros(transaction_id=b"")),
}


# --- Telemetry: the expansion port (A1783, A1785) -----------------------------

#: ``c0`` expansion block (35 B with no pack, 44 B with one): both length
#: prefixes move the later offsets. a1783-telemetry.md "Expansion block
#: (c0)"; names are the app's ``subPackageInfo`` entry's.
Expansion = Struct(
    "sub_package_sn" / Prefixed(Int8ul, GreedyBytes),
    "sub_package_number" / ui(1),
    "sub_package_version" / Bytes(VERSION_SIZE),
    "sub_package_temperature" / ui(1),
    "sub_package_status" / ui(1),
    "sub_package_battery_level" / ui(1),
    "sub_package_health" / ui(1),
    "sub_package_blink_status" / ui(1),
    "sub_package_switch_status" / ui(1),
    "sub_package_error_code" / ui(1),
    "sub_package_connection_status" / ui(1),
    "sub_package_balance_status" / ui(1),
    "sub_package_type" / Prefixed(Int8ul, GreedyBytes),
)
EXPANSION_ZEROS = leaf_zeros(
    "sub_package_number",
    "sub_package_temperature",
    "sub_package_status",
    "sub_package_battery_level",
    "sub_package_health",
    "sub_package_blink_status",
    "sub_package_switch_status",
    "sub_package_error_code",
    "sub_package_connection_status",
    "sub_package_balance_status",
    sub_package_sn=b"",
    sub_package_version=bytes(VERSION_SIZE),
    sub_package_type=b"",
)


class ExternalDeviceMode(IntEnum):
    """``ce[40]``, ``external_device_mode``: the Alternator Charger's CAN-reported mode.

    PiField firmware/bus-can.md "Car alternator charger (ALT) reports".
    """

    #: No charger attached: the record is zeroed.
    NONE = 0
    #: Reverse Mode: the power station's power goes out to the car battery.
    REVERSE_CHARGING = 1
    #: Charging Mode: the alternator charges the power station.
    CHARGING = 2
    #: Attached and doing neither.
    STANDBY = 3


#: ``ce`` altSampData (43 B): the car-alternator charger accessory.
#: a1783-control.md "Other local-state tags" ``ce``; names follow the app's
#: ``externalDeviceInfoList`` entry (the mode and power have no app name).
PN_SIZE = 16
SN_SIZE = 17
Alternator = Struct(
    "external_device_product_code" / Prefixed(Int8ul, Bytes(PN_SIZE)),
    Const(b"\x01"),
    "external_device_sn" / Prefixed(Int8ul, Bytes(SN_SIZE)),
    Padding(4),
    "external_device_mode" / ui(1),
    "external_device_output_power" / ui(2),
)
EXPANSION_FIELDS: dict[str, FieldSpec] = {
    "c0": (Expansion, 0x04, EXPANSION_ZEROS),
    "ce": (
        Alternator,
        0x04,
        leaf_zeros(
            "external_device_mode",
            "external_device_output_power",
            external_device_product_code=bytes(PN_SIZE),
            external_device_sn=bytes(SN_SIZE),
        ),
    ),
}


# --- Telemetry: the other messages --------------------------------------------

#: ``0425`` event report (push). a1783-control.md "0425 event report": ``a2``
#: the event, ``a3``/``a4`` the window's u32 epochs; a grid loss (``0x59``)
#: carries the SoC as a 1-byte ``a3`` and no ``a4``. The app names none of
#: these, so the names are descriptive.
MSGTYPE_425 = 0x425
EVENT_GRID_LOSS = 0x59
EVENT_SOC = "event_soc"
EVENT_FIELDS: dict[str, FieldSpec] = {
    "a2": (Struct("event_code" / ui(1)), 0x01, leaf_zeros("event_code")),
    "a3": (
        Struct("event_start_timestamp" / ui(4)),
        0x04,
        leaf_zeros("event_start_timestamp"),
    ),
    "a4": (
        Struct("event_end_timestamp" / ui(4)),
        0x04,
        leaf_zeros("event_end_timestamp"),
    ),
}
#: ``0489`` push and ``4089`` reply: OTA readiness, ``a2`` allowed and ``a3``
#: the block reason (0 / 2 low SoC / 4 "connect AC"). a1783-control.md
#: "4089"; ``a2`` is the app's ``otaEnable``, ``a3`` has no app name.
#: ``a4``-``a6`` are the recordings' constants.
MSGTYPE_489 = 0x489
MSGTYPE_889 = 0x889
OTA_FIELDS: dict[str, FieldSpec] = {
    "a2": (Struct("ota_enable" / ui(1)), 0x01, leaf_zeros("ota_enable")),
    "a3": (Struct("ota_block_reason" / ui(1)), 0x01, leaf_zeros("ota_block_reason")),
}
#: ``4092`` reply: ``a2`` = ``da[0:16]``, ``a3`` = the theme name ``4091 a5``
#: stored (empty on every recording). a1783-control.md "4092".
MSGTYPE_892 = 0x892
LCD_THEME_NAME = "lcd_theme_name"
CLOCK_READ_FIELDS: dict[str, FieldSpec] = {
    "a2": (ClockScreenHead, 0x04, CLOCK_HEAD_ZEROS),
    "a3": (Struct(LCD_THEME_NAME / GreedyBytes), 0x04, {LCD_THEME_NAME: b""}),
}
#: ``0503`` energy and TOU-time totals, a push only to the cloud
#: (``td_stats_send``): ``a2`` the main pack's lifetime Wh and the seconds in
#: each active TOU type, ``a3`` the Time Save epoch. a1783-telemetry.md
#: "0503"; the app logs none, so the names are the firmware's ``[touWh]`` /
#: ``[touTs]`` labels, the periods under the app's type names.
MSGTYPE_503 = 0x503
TouTotals = Struct(
    "solar_to_battery_energy" / ui(4),
    "grid_to_battery_energy" / ui(4),
    "battery_to_load_energy" / ui(4),
    "peak_time" / ui(4),
    "mid_peak_time" / ui(4),
    "off_peak_time" / ui(4),
)
TOU_TOTALS_FIELDS: dict[str, FieldSpec] = {
    "a2": (TouTotals, TYPE_BIN, struct_zeros(TouTotals)),
    "a3": one_field("time_save", ui(4), TYPE_VAR),
}
#: Typed tags of every message but ``0421``, whose table is per layout.
MESSAGE_FIELDS: dict[int, Mapping[str, FieldSpec]] = {
    MSGTYPE_425: EVENT_FIELDS,
    MSGTYPE_489: OTA_FIELDS,
    MSGTYPE_889: OTA_FIELDS,
    MSGTYPE_892: CLOCK_READ_FIELDS,
    MSGTYPE_503: TOU_TOTALS_FIELDS,
}


# --- Commands (fid 0x0f) ------------------------------------------------------
# PiField/docs/re/a1783-control.md "Command map", "Groups 4101-4103", "TOU
# (4090)", "Clock screen (4091 -> da)" and "4104": every command below has a
# captured or firmware-documented payload shape. 4057 sets only what its stop
# is seen to do, the device-point switch off; its streaming countdown is a
# module timer, not a telemetry field. 405e sets the backup plans; their windows
# run on the clock (below). 4063 (BLE OTA start) and 4072 (daily update window)
# answer only with their recorded replies and change nothing; 4064 (BLE OTA
# data) gets no reply.


#: ``4101`` AC group: tag -> (the field it sets, its codec).
AC_GROUP = {
    "a2": ("ac_output_status", ui(1)),
    "a3": ("ac_timer_value", ui(4)),
    "a4": ("ac_recharge_power", ui(2)),
    "a5": ("ac_frequency", ui(1)),
    "a6": ("ac_energy_saving_switch", ui(1)),
    "a7": ("super_fast_charge_status", ui(1)),
    "ab": ("ac_input_disable_switch", ui(2)),
}
#: ``4102`` DC/car group.
DC_GROUP = {
    "a2": ("car_charge1_output_status", ui(1)),
    "a3": ("car_charger_timer_value", ui(4)),
    "a4": ("car_charger_energy_saving_switch", ui(1)),
}
#: ``4103`` system group, but for the SoC limits and the outage alert.
SYSTEM_GROUP = {
    "a2": ("screen_switch", ui(1)),
    "a3": ("lcd_backlight_brightness", ui(1)),
    "a4": ("screen_off_time", ui(2)),
    "a5": ("temperature_unit", ui(1)),
    "a6": ("device_standby_time", ui(2)),
    "a8": ("memory_switch", ui(1)),
    "a9": ("device_language", ui(1)),
}
SOC_LIMITS = {
    "aa": ("charging_limit_value", ui(1)),
    "ab": ("discharge_limit_value", ui(1)),
}
#: ``4090`` TOU plan: ``a6`` (the period count) and ``a7`` (the periods) are read apart.
TOU_PLAN = {"a2": ("backup_mode", ui(1)), "a5": ("tou_reserve_power", ui(1))}
#: ``4104``: ``a4`` the expansion's switch; ``a5`` (locate blink) sets no field.
EXPANSION_GROUP = {"a4": ("sub_package_switch_status", ui(1))}


def _set_ac_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return the AC group's changed fields; any subset of its tags may be present."""
    return decode_request(request, AC_GROUP)


def _set_car_charger_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return the DC/car group's changed fields; any subset may be present."""
    return decode_request(request, DC_GROUP)


def _soc_limits(request: ParameterDict) -> dict[str, Value]:
    """Return the charging/discharge limit clamp (``dev_cmd_15_soc_limits_set?``).

    charging <= discharge resets both to 100/1; else a charging limit outside
    2-100 clamps to 100 and a discharge limit of 0 clamps to 1.
    """
    limits = decode_request(request, SOC_LIMITS)
    charging = limits.get("charging_limit_value")
    discharge = limits.get("discharge_limit_value")
    if charging is not None and discharge is not None and charging <= discharge:
        charging, discharge = 100, 1
    else:
        if charging is not None and not 2 <= charging <= 100:  # noqa: PLR2004  # the firmware's own clamp bounds
            charging = 100
        if discharge == 0:
            discharge = 1
    changes: dict[str, Value] = {}
    if charging is not None:
        changes["charging_limit_value"] = charging
    if discharge is not None:
        changes["discharge_limit_value"] = discharge
    return changes


def _set_system_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return the system group's changed fields; any subset may be present."""
    changes = decode_request(request, SYSTEM_GROUP)
    if "aa" in request or "ab" in request:
        changes.update(_soc_limits(request))
    if "b0" in request and request["b0"]["type"] is not None:
        changes["outage_alert_switch"] = decode_field(request, "b0", Flag)
    return changes


def _set_tou_system_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return the mode and reserve SoC ``4090`` stores (``fid0f_op90_tou_set?``).

    ``a3``/``a4`` are never read. An ``a6`` or ``a7`` over six periods is
    refused; the periods themselves are ``_tou_periods``'s.
    """
    periods = decode_field(request, "a7", GreedyBytes) if "a7" in request else b""
    if len(periods) > TOU_RECORDS_SIZE:
        return None
    if "a6" in request and decode_field(request, "a6", ui(1)) > TOU_PERIODS_MAX:
        return None
    return decode_request(request, TOU_PLAN)


def _tou_table_of(periods: bytes) -> bytes:
    """Return the stored plan a ``tou_periods`` value implies, zero past its periods."""
    schedule = TouRecords.parse(periods)
    return TouTable.build({"count": schedule.count, "records": schedule.records})


def _check_tou_table(table: Value) -> None:
    """Check a stored plan is its count byte and record buffer.

    Raises:
        TypeError: If it isn't.

    """
    msg = f"{TOU_TABLE} is a count byte then {TOU_RECORDS_SIZE} record bytes"
    if not isinstance(table, bytes):
        raise TypeError(msg)
    try:
        TouTable.parse(table)
    except ConstructError as error:
        raise TypeError(msg) from error


def _tou_periods_of(table: bytes) -> bytes:
    """Return ``d9``'s ``tou_periods``: the count, then that many stored periods.

    Periods past the record buffer read zero.
    """
    stored = TouTable.parse(table)
    records = fitted(stored.count * TOU_RECORD_SIZE).build(stored.records)
    return TouRecords.build({"count": stored.count, "records": records})


def _tou_periods(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Return the plan after ``4090``: ``a7`` copied raw over the record buffer.

    ``a6`` sets the count raw, else the stored count stays; nothing checks the
    count against the periods sent. Bytes ``a7`` doesn't cover keep their
    stored values, past the count too, so a raised count shows them again
    (a1783-control.md "TOU (4090)").
    """
    if "a6" not in request and "a7" not in request:
        return {}
    stored = TouTable.parse(_bytes_value(state, TOU_TABLE))
    count = decode_field(request, "a6", ui(1)) if "a6" in request else stored.count
    periods = decode_field(request, "a7", GreedyBytes) if "a7" in request else b""
    size = len(periods)
    buffer = TouOverwrite.parse(stored.records, covered_size=size)
    records = TouOverwrite.build(
        {"covered": periods, "kept": buffer.kept}, covered_size=size
    )
    table = TouTable.build({"count": count, "records": records})
    return {TOU_TABLE: table, "tou_periods": _tou_periods_of(table)}


class TouPeriodType(IntEnum):
    """A plan record's type byte, ``d9[0]`` when active; the app's labels."""

    NONE = 0
    PEAK = 1
    MID_PEAK = 2
    OFF_PEAK = 3


#: ``d9[1]`` (T[0]) in Time-of-Use mode; any other value never activates.
TOU_MODE_ON = 1
#: The active periods that hold grid charging above the reserve threshold.
HELD_PERIODS = frozenset({TouPeriodType.PEAK, TouPeriodType.MID_PEAK})
#: The threshold's margin over the discharge limit: max(reserve, min + 5).
TOU_MIN_MARGIN = 5
#: The main pack's displayed SoC, the hold compares (c490 ``.14.18``, 0.1 %).
PACK_SOC = "pack_soc"
#: ``pack_soc`` steps per whole percent, and its full scale (100.0 %).
SOC_STEPS_PER_PERCENT = 10
SOC_FULL = 1000
#: The MainMcu build (v1.2.1.6) from which Battery-Only Power blocks TOU.
AC_INPUT_DISABLE_GATE_VERSION = 1216
#: ``a7[3]`` ``ac_input_status``: no mains at the inlet; 1 mains, 2 mains with
#: Battery-Only Power on (a1783-telemetry.md "Presence is not current").
AC_INPUT_NONE = 0
AC_INPUT_MAINS = 1
AC_INPUT_BATTERY_ONLY = 2
#: One plan record: ``{type, start hour, end hour}``, whole hours.
TouRecord = Struct("type" / ui(1), "start_hour" / ui(1), "end_hour" / ui(1))
#: The module's cloud device-point switch (``device_get_device_point_switch``),
#: which the ``0490`` push needs on; no field carries it, and the app has no
#: name for it. Unset, it reads on. a1783-telemetry.md "When the display board
#: posts c490".
DEVICE_POINT_SWITCH = "device_point_switch"
#: ``4057 a2``: 0 stops the realtime stream, 1 starts it.
REALTIME_STOP = 0
#: The data-read replies and telemetry pushes that ride ``param_info``:
#: a1783-telemetry.md "MQTT channel routing".
PARAM_INFO_MESSAGES = frozenset(
    {MSGTYPE_421, MSGTYPE_900, MSGTYPE_489, MSGTYPE_889, MSGTYPE_892}
)
#: The ``a1`` MainMcu gives what it publishes to MQTT, bound and on WiFi:
#: ``34`` on ``param_info``, ``12`` on the ``state_info`` events (``0425``,
#: ``0503``). a1783-telemetry.md "MQTT channel routing".
PARAM_INFO_ROUTE = ROUTE_LAYOUT.build(
    Container(destination=ROUTE_APP, source=ROUTE_PARAM_INFO)
)
STATE_INFO_ROUTE = ROUTE_LAYOUT.build(
    Container(destination=ROUTE_BLE, source=ROUTE_STATE_INFO)
)
MQTT_ROUTES = MappingProxyType(
    {
        **dict.fromkeys(PARAM_INFO_MESSAGES, PARAM_INFO_ROUTE),
        MSGTYPE_425: STATE_INFO_ROUTE,
        MSGTYPE_503: STATE_INFO_ROUTE,
    }
)
#: The pushes MainMcu sends only to the cloud.
CLOUD_PUSHES = (MSGTYPE_503,)


def _check_switch(name: str, value: Value) -> None:
    """Check a hidden on/off input is 0 or 1.

    Raises:
        TypeError: If it isn't.

    """
    if not isinstance(value, int) or value not in {0, 1}:
        msg = f"{name} is 0 or 1"
        raise TypeError(msg)


def _tou_inputs(params: ParameterDict, values: Mapping[str, Value]) -> dict[str, Value]:
    """Return what the TOU conditions read, by name: a frame's fields on ``values``."""
    tou = decode_field(params, "d9", Tou)
    inputs: dict[str, Value] = {
        **values,
        "backup_mode": tou.backup_mode,
        "tou_periods": tou.tou_periods,
        "ac_input_status": decode_field(params, "a7", AcOutput).ac_input_status,
        "mcu_version": decode_field(params, "f9", DeviceVersions).mcu_version,
        DISABLE: decode_field(params, "a4", Settings)[DISABLE],
    }
    if "ce" in params:
        alternator = decode_field(params, "ce", Alternator)
        inputs["external_device_product_code"] = alternator.external_device_product_code
    return inputs


def _alternator_attached(state: Mapping[str, Value]) -> bool:
    """Whether a car-alternator charger is attached (TOU condition 4).

    Assumed: attached when ``ce`` names a product code; the firmware's present
    byte (block 0xe +0x30) isn't in ``ce``.
    """
    code = state.get("external_device_product_code")
    return code is not None and code != bytes(PN_SIZE)


def _ac_input_disabled(state: Mapping[str, Value]) -> bool:
    """Whether Battery-Only Power blocks TOU (condition 5, MainMcu v1.2.1.6 on).

    Assumed: both ``a4[31]`` input states, 1 (``4101 ab``) and 2 (auto-off).
    """
    version = VersionNumber.parse(_bytes_value(state, "mcu_version"))
    if version < AC_INPUT_DISABLE_GATE_VERSION:
        return False
    return bool(state[DISABLE])


def _active_period(params: ParameterDict, values: Mapping[str, Value]) -> int:
    """Return a frame's ``d9[0]``: its active record's type, else 0."""
    return _period_of(_tou_inputs(params, values))


def _period_of(state: Mapping[str, Value]) -> int:
    """Return the active record's type, else 0 (``tou_active_period_get?``).

    a1783-control.md "TOU (4090)" conditions 1-6, in order; the hour is the
    RTC's. Assumed: the first record in order wins where two match, and
    condition 3 (the power stage's AC input, ``0x2000054a`` bit 0) is mains at
    the inlet, ``a7[3]`` != 0: the two differ only in a power-stage fault.
    """
    if not _tou_can_run(state):
        return TouPeriodType.NONE
    hour = int(state[RTC]) // 3600 % 24
    for record in _tou_records(state):
        if record.start_hour <= hour < record.end_hour:
            return int(record.type)
    return TouPeriodType.NONE


def _tou_can_run(state: Mapping[str, Value]) -> bool:
    """Whether TOU conditions 1-5 hold: the mode on, online, no ALT, input allowed."""
    on = state["backup_mode"] == TOU_MODE_ON
    online = state.get(CLOUD) and state["ac_input_status"] != AC_INPUT_NONE
    if not on or not online or _alternator_attached(state):
        return False
    return not _ac_input_disabled(state)


def _tou_records(state: Mapping[str, Value]) -> list[Container[Any]]:
    """Return the plan's records: type, start hour, end hour."""
    records = TouRecords.parse(_bytes_value(state, "tou_periods")).records
    return list(GreedyRange(TouRecord).parse(records))


#: AC input after a holding period ends, as (seconds after the hour, W at the
#: 1290 W the charge came to): the one recorded end, the cloud ``0421`` of
#: 2026-10-21 16:00:07-16:00:13.
#: Before the first sample it reads 0 W; from the last, the full charge.
TOU_RAMP = ((7, 86), (8, 57), (9, 178), (11, 770), (13, 1290))
TOU_RAMP_FULL = 1290


def _hold_ended(params: ParameterDict, values: Mapping[str, Value]) -> int | None:
    """Return the seconds since a holding record ended, while AC input ramps back.

    None outside the ramp, or when the record that ended wasn't holding.
    """
    state = _tou_inputs(params, values)
    if not _tou_can_run(state):
        return None
    seconds = int(values[RTC]) % 86400
    for record in _tou_records(state):
        elapsed = seconds - int(record.end_hour) * 3600
        last = TOU_RAMP[-1][0]
        if 0 <= elapsed < last and _charge_held(params, values, int(record.type)):
            return elapsed
    return None


def _ramped(power: int, elapsed: int) -> int:
    """Return the AC input ``elapsed`` s after the hold ended, ramping to ``power``."""
    reached = [watts for at, watts in TOU_RAMP if at <= elapsed]
    return round(power * reached[-1] / TOU_RAMP_FULL) if reached else 0


def _charge_held(
    params: ParameterDict, values: Mapping[str, Value], period: int
) -> bool:
    """Whether ``period`` holds grid charging off at the main pack's SoC.

    Peak and Mid-Peak hold while c490 ``.14.18`` (the main pack, 0.1 %) >=
    min(100.0 %, max(reserve, min + 5)) (a1783-control.md "TOU" effects).
    Assumed: the main pack only, and no +0.3 % after a pass with AC current.
    """
    if period not in HELD_PERIODS:
        return False
    tou = decode_field(params, "d9", Tou)
    floor = max(tou.tou_reserve_power, tou.discharge_limit_value + TOU_MIN_MARGIN)
    threshold = min(SOC_FULL, floor * SOC_STEPS_PER_PERCENT)
    return bool(int(values[PACK_SOC]) >= threshold)


#: ``a4[31]`` bits 2-3 (``ac_input_disable_switch``): Battery-Only Power off, on
#: (``4101 ab``), and cleared by the firmware (the mains auto-off, or the
#: low-battery exit), which accepts AC input (a1783-operation.md 05:45:40).
DISABLE = "ac_input_disable_switch"
BATTERY_ONLY_OFF = 0
BATTERY_ONLY_ON = 1
BATTERY_ONLY_AUTO_OFF = 2
#: ``a3[0]``, ``a5[1]`` and ``c0[24]``: no flow, output on, charging.
FLOW_IDLE = 0
FLOW_OUT = 1
FLOW_CHARGE = 2
#: ``a3[0]`` follows ``a5[1]`` after its 20-sample pack-current test, 100 ms a
#: sample: 2 s (on the wire 2.0-3.0 s; a1783-telemetry.md "``a5[1]`` vs ``a3[0]``").
FLOW_LAG = 2.0
#: The mains sequence, a1783-control.md "``a4[31]`` witnesses": the 2026-10-26
#: run's timings, with the range both bench runs measured.
#: The auto-off after mains goes: 0 s (0-1 s).
AUTO_OFF_DELAY = 0.0
#: The 0x59 off-grid alert after mains goes: 4 s (4-5 s).
GRID_LOSS_ALERT_DELAY = 4.0
#: The auto-off clearing after mains returns: 3 s (2-3 s).
AUTO_OFF_CLEAR_DELAY = 3.0
#: AC charging after mains returns: 11 s (10-16 s).
MAINS_CHARGE_DELAY = 11.0
#: AC charging after Battery-Only Power goes off with mains in: the median of
#: 6 s (2026-10-06 19:40:42) and 9 s (2026-10-26 16:34:10).
BATTERY_ONLY_CHARGE_DELAY = 7.5
#: The AC charge power: 1208 W (1208-1225 W).
AC_CHARGE_POWER = 1208


def _without(changes: dict[str, Value], own: frozenset[str]) -> dict[str, Value]:
    """Return ``changes`` less the names the triggering change set itself."""
    return {name: value for name, value in changes.items() if name not in own}


def _auto_off(
    own: frozenset[str], _values: Mapping[str, Value], _state: Mapping[str, Value]
) -> Step:
    """Clear Battery-Only Power with mains out, the screen woken (06:02:35)."""
    wake = Timed(0.0, SCREEN_WAKE, partial(_screen_wake, own))
    return Step(values=_without({DISABLE: BATTERY_ONLY_AUTO_OFF}, own), then=(wake,))


def _grid_loss_alert(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Push the 0x59 off-grid alert with the SoC, if mains is still out.

    Assumed: mains back before it is due cancels it, the loss being debounced.
    """
    if state["ac_input_status"] != AC_INPUT_NONE:
        return Step()
    alert = {"event_code": EVENT_GRID_LOSS, EVENT_SOC: state["battery_level"]}
    return Step(values=alert, pushes=(MSGTYPE_425,))


def _auto_off_clear(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Accept AC input again once mains is back (06:02:52, 15:28:54)."""
    if (
        state["ac_input_status"] == AC_INPUT_NONE
        or state[DISABLE] != BATTERY_ONLY_AUTO_OFF
    ):
        return Step()
    return Step(values={DISABLE: BATTERY_ONLY_OFF})


def _ac_charge(
    own: frozenset[str], _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Start AC charging into the pack below its cap (a1783-telemetry.md "Flow state").

    The main below its cap charges (``a5[1]`` 2, ``a3[0]`` following); at it,
    an attached expansion below its own takes the charge (``c0[24]`` 2,
    06:03:06); with every pack at its cap no AC input comes.
    """
    if state["ac_input_status"] == AC_INPUT_NONE or state[DISABLE] == BATTERY_ONLY_ON:
        return Step()
    cap = int(state["charging_limit_value"])
    if int(state["battery_level"]) < cap:
        changes: dict[str, Value] = {
            "ac_input_power": AC_CHARGE_POWER,
            "charge_discharge_status": FLOW_CHARGE,
        }
    elif state.get("sub_package_connection_status") and (
        int(state["sub_package_battery_level"]) < cap
    ):
        changes = {"ac_input_power": AC_CHARGE_POWER, "sub_package_status": FLOW_CHARGE}
    else:
        return Step()
    return _flow_step(_without(changes, own))


def _mains_back(after: Mapping[str, Value], own: frozenset[str]) -> list[Timed]:
    """Return the auto-off's clearing and the AC charge start mains returning draws."""
    events = []
    if after[DISABLE] == BATTERY_ONLY_AUTO_OFF and DISABLE not in own:
        events.append(Timed(AUTO_OFF_CLEAR_DELAY, "auto_off_clear", _auto_off_clear))
    if "ac_input_power" not in own:
        events.append(Timed(MAINS_CHARGE_DELAY, "ac_charge", partial(_ac_charge, own)))
    return events


def _battery_only_switched(
    before: Mapping[str, Value], after: Mapping[str, Value], own: frozenset[str]
) -> list[Timed]:
    """Return what switching Battery-Only Power with mains in does.

    On, ``a7[3]`` reads 2 and AC input stops (19:39:18), and a charge only AC
    fed leaves ``a5[1]`` / ``c0[24]`` at once for the port flow, ``a3[0]``
    following; off, it reads 1 and AC charging restarts (19:40:42, 6-9 s later).
    """
    switched_on = after[DISABLE] == BATTERY_ONLY_ON
    changes: dict[str, Value] = {
        "ac_input_status": AC_INPUT_BATTERY_ONLY if switched_on else AC_INPUT_MAINS
    }
    if switched_on:
        changes["ac_input_power"] = 0
        changes |= _ac_charge_left(after)
    events = [Timed(0.0, "battery_only", partial(_flow_change, _without(changes, own)))]
    if before[DISABLE] == BATTERY_ONLY_ON and "ac_input_power" not in own:
        charge = partial(_ac_charge, own)
        events.append(Timed(BATTERY_ONLY_CHARGE_DELAY, "ac_charge", charge))
    return events


def _step(
    changes: Mapping[str, Value],
    _values: Mapping[str, Value],
    _state: Mapping[str, Value],
) -> Step:
    """Return a step that sets ``changes``."""
    return Step(values=changes)


def _flow_change(
    changes: dict[str, Value],
    _values: Mapping[str, Value],
    _state: Mapping[str, Value],
) -> Step:
    """Return a step that sets ``changes``, ``a3[0]`` following a moved ``a5[1]``."""
    return _flow_step(changes)


def _flow_step(changes: dict[str, Value], then: tuple[Timed, ...] = ()) -> Step:
    """Return a step setting ``changes``; a moved ``a5[1]`` draws ``a3[0]``'s follow."""
    if "charge_discharge_status" in changes:
        then = (*then, FLOW_FOLLOW)
    return Step(values=changes, then=then)


def _flow_follows(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Set ``a3[0]`` to ``a5[1]``'s state, its 20 samples passed."""
    return Step(values={"work_status": int(state["charge_discharge_status"])})


FLOW_FOLLOW = Timed(FLOW_LAG, "work_status_follow", _flow_follows)


def _ac_charge_left(state: Mapping[str, Value]) -> dict[str, Value]:
    """Return the port flow a charge only AC fed leaves ``a5[1]`` / ``c0[24]`` at."""
    if state["pv1_input_power"] or state.get("external_device_mode") == (
        ExternalDeviceMode.CHARGING
    ):
        return {}
    flow = FLOW_OUT if state["total_output_power"] else FLOW_IDLE
    return {
        name: flow
        for name in ("charge_discharge_status", "sub_package_status")
        if state.get(name) == FLOW_CHARGE
    }


#: ``0490`` ``.15.5`` / ``.15.6`` (main, then expansion): the pack holding the bus
#: reads (8, 9), or (3, 9) when quiet at or above its cap; the other (3, 3)
#: (a1783-operation.md "``.15.5`` against the limits", "Observed handovers").
#: Set at each switch; the expansion's quiet 13 at its cap isn't modeled.
MAIN_SLOTS = ("stage_a_state", "stage_b_state")
EXPANSION_SLOTS = ("sub_package_stage_a_state", "sub_package_stage_b_state")
ON_BUS = (8, 9)
QUIET_AT_CAP = (3, 9)
OFF_BUS = (3, 3)
#: Pack selection (MainBMS ``pack_select_eval``, firmware/mainbms-control-fsms.md
#: "Pack selection", 10 ms steps), re-checked on every change:
#: - the charge-cap latch (:193, ``0x20000b64``): sets after 100 steps with the
#:   on-bus pack charging at or above its cap and a charge source in
#:   (``flag_00c7``), whatever the expansion's state; clears in discharge and
#:   at once when the source goes; setting it clears the discharge latch, whose
#:   arming clears it (:194). Its idle 60 s / 3 s paths, the no-SoC count and
#:   the fault path aren't modeled;
#: - main -> expansion charge (:165): the charge-cap latch set, the expansion
#:   below its cap, switched on, its path hold clear;
#: - main -> expansion discharge (:164): the discharge latch (:194 (a): the
#:   main's own SoC above ``min_soc`` + 0.9 % and a >= 1.0 % drop in it since it
#:   last charged); the expansion's discharge allowance non-zero (the stand-in
#:   below; the path doesn't re-check the expansion's SoC), its path hold clear;
#:   more than 200 steps since the last switch;
#: - expansion -> main discharge (:170): 300 steps on the expansion, the
#:   charge-cap latch clear, its SoC under ``min_soc`` + 0.9 %, checked every
#:   step; the main's charge allowance as :170 reads it is taken as non-zero
#:   (which record holds it, and its value at the cap, are open).
#: The emulator's allowance stand-ins: a pack may charge below its cap; the
#: expansion's discharge allowance reads 0 under ``min_soc`` + 0.9 %, unconfirmed
#: (on 2026-09-03 03:29-08:00 the allowance or the path hold held :164 off over
#: the band, which one unread). :172 (that allowance at 0 for 300 steps) would
#: move the bus only once :170 has. The path hold is the expansion BMS's own
#: ``raw+0x95`` (CAN ``0x045a``, a contactor / active-path hold): hidden, clear
#: unless set by name. Not modeled: the once-per-boot switch (:163), the
#: pack-switch code (:167, :173), the allowance-only rows (:168, :169, :171),
#: the expansion's share of the discharge latch and the latches' other paths.
#: Hidden, tracked with an expansion attached: the pack on the bus (1 main, 2
#: expansion; unset, the expansion when one is attached, as on the A1783
#: recording, else the main), when it took it (UTC s), the charge-cap latch,
#: and the discharge latch (``0x20000b65``) and the main's 0.1 % SoC it counts
#: its drop from: re-taken only while the latch is clear, as the main charges
#: (its SoC when charging stops; the 1 s settle isn't modeled) and when the
#: latch arms or the charge-cap latch changes, never on a switch or a rise
#: without charging (firmware/mainbms-control-fsms.md, the list after
#: "Latches"); first seen, the main's SoC before the change.
BUS_PACK = "pack_on_bus"
BUS_MAIN = 1
BUS_EXPANSION = 2
BUS_TAKEN = "bus_taken"
LATCH_FROM = "discharge_latch_from"
LATCH_SET = "discharge_latch"
CAP_LATCH = "charge_cap_latch"
CAP_LATCH_EVENT = "charge_cap_latch_count"
CAP_LATCH_REFERENCE = "charge_cap_latch_clear"
EXPANSION_PACK_SOC = "sub_package_pack_soc"
PATH_HOLD = "sub_package_path_hold"
BUS_DWELL = 3.01
SWITCH_GAP = 2.01
CHARGE_CAP_LATCH = 1.0
DISCHARGE_LATCH_DROP = 10
PACK_HANDOVER = "pack_handover"
LATCH_REFERENCE = "discharge_latch_reference"


def _bus_pack(state: Mapping[str, Value]) -> int:
    """Return the pack holding the bus."""
    held = state.get(BUS_PACK)
    if held is not None:
        return int(held)
    return BUS_EXPANSION if state.get("sub_package_connection_status") else BUS_MAIN


def _main_soc(state: Mapping[str, Value]) -> int:
    """Return the main pack's SoC in 0.1 % (``pack_soc``)."""
    return int(state[PACK_SOC])


def _expansion_soc(state: Mapping[str, Value]) -> int:
    """Return the expansion's SoC in 0.1 % (``sub_package_pack_soc``)."""
    return int(state[EXPANSION_PACK_SOC])


def _cap(state: Mapping[str, Value]) -> int:
    """Return ``max_soc`` in 0.1 %."""
    return int(state["charging_limit_value"]) * SOC_STEPS_PER_PERCENT


def _floor(state: Mapping[str, Value]) -> int:
    """Return ``min_soc`` in 0.1 %."""
    return int(state["discharge_limit_value"]) * SOC_STEPS_PER_PERCENT


def _expansion_floor_line(state: Mapping[str, Value]) -> int:
    """Return the handover line, ``min_soc`` + 0.9 %, in 0.1 %."""
    return _floor(state) + LOW_BATTERY_MARGIN


def _held_for(state: Mapping[str, Value], at_least: float) -> float:
    """Return how long the bus has been held; ``at_least`` when not known."""
    taken = state.get(BUS_TAKEN)
    return at_least if taken is None else float(int(state[EPOCH]) - int(taken))


def _bus_to(
    holder: int, state: Mapping[str, Value], *, flowing: bool
) -> dict[str, Value]:
    """Return the values of ``holder`` taking the bus: its slots and hidden marks."""
    soc = _main_soc(state) if holder == BUS_MAIN else _expansion_soc(state)
    held = ON_BUS if flowing or soc < _cap(state) else QUIET_AT_CAP
    main, expansion = (held, OFF_BUS) if holder == BUS_MAIN else (OFF_BUS, held)
    slots = zip((*MAIN_SLOTS, *EXPANSION_SLOTS), (*main, *expansion), strict=True)
    return {**dict(slots), BUS_PACK: holder, BUS_TAKEN: int(state[EPOCH])}


def _on_bus(state: Mapping[str, Value]) -> tuple[int, int]:
    """Return the on-bus pack's flow and 0.1 % SoC."""
    if _bus_pack(state) == BUS_MAIN:
        return int(state["charge_discharge_status"]), _main_soc(state)
    return int(state["sub_package_status"]), _expansion_soc(state)


def _cap_latch_counts(state: Mapping[str, Value]) -> bool:
    """Return whether the 1 s count that sets the charge-cap latch runs (:193).

    The latch clear, the on-bus pack charging at or above its cap, a source in.
    """
    flow, soc = _on_bus(state)
    return (
        not state.get(CAP_LATCH)
        and flow == FLOW_CHARGE
        and soc >= _cap(state)
        and _charge_source(state)
    )


def _cap_latch_clears(state: Mapping[str, Value]) -> bool:
    """Return whether the charge-cap latch clears: in discharge, or no source in."""
    return _on_bus(state)[0] == FLOW_OUT or not _charge_source(state)


def _cap_latched(state: Mapping[str, Value]) -> bool:
    """Return whether the charge-cap latch (``0x20000b64``) is set."""
    return bool(state.get(CAP_LATCH)) and not _cap_latch_clears(state)


def _charge_handover_due(state: Mapping[str, Value]) -> bool:
    """Return whether :165 holds: the main on the bus and the cap latch set.

    The expansion below its cap, switched on and its path hold clear.
    """
    return (
        _bus_pack(state) == BUS_MAIN
        and _cap_latched(state)
        and _expansion_soc(state) < _cap(state)
        and _expansion_on(state)
        and not state.get(PATH_HOLD)
    )


def _expansion_may_discharge(state: Mapping[str, Value]) -> bool:
    """Return the stand-in for the expansion's discharge allowance being non-zero.

    Zero under ``min_soc`` + 0.9 % (unconfirmed: the allowance or the path hold).
    """
    return _expansion_soc(state) >= _expansion_floor_line(state)


def _expansion_takes_discharge(state: Mapping[str, Value]) -> bool:
    """Return whether :164's expansion gates pass: allowance, switch, path hold."""
    return (
        _expansion_may_discharge(state)
        and _expansion_on(state)
        and not state.get(PATH_HOLD)
    )


def _main_due(state: Mapping[str, Value]) -> bool:
    """Return whether :170 holds: the cap latch clear, the expansion under its line."""
    return not _cap_latched(state) and _expansion_soc(state) < _expansion_floor_line(
        state
    )


def _handovers(
    before: Mapping[str, Value], after: Mapping[str, Value], own: frozenset[str]
) -> list[Timed]:
    """Return the pack switch, or the latches' new state, a change draws."""
    if not after.get("sub_package_connection_status"):
        return []
    events = _cap_latch_events(before, after, own)
    if _bus_pack(after) == BUS_MAIN:
        return events + _main_handovers(before, after, own)
    taken = _bus_pack(before) != BUS_EXPANSION
    if _main_due(after) and (taken or not _main_due(before)):
        dwell = max(0.0, BUS_DWELL - _held_for(after, BUS_DWELL))
        events.append(Timed(dwell, PACK_HANDOVER, _bus_to_main))
    return events


def _cap_latch_events(
    before: Mapping[str, Value], after: Mapping[str, Value], own: frozenset[str]
) -> list[Timed]:
    """Return the charge-cap latch's clearing, or its 1 s count, a change draws.

    Each latch setting clears the other, so a clearing re-takes the baseline.
    """
    if after.get(CAP_LATCH) and _cap_latch_clears(after):
        marks = {CAP_LATCH: 0, LATCH_FROM: _main_soc(after)}
        return [Timed(0.0, CAP_LATCH_REFERENCE, partial(_latch_marks, marks))]
    if _cap_latch_counts(after) and not _cap_latch_counts(before):
        latch = partial(_cap_latch_set, own)
        return [Timed(CHARGE_CAP_LATCH, CAP_LATCH_EVENT, latch)]
    return []


def _main_handovers(
    before: Mapping[str, Value], after: Mapping[str, Value], own: frozenset[str]
) -> list[Timed]:
    """Return the switch to the expansion, or the latch's new start, the main draws."""
    stored = after.get(LATCH_FROM)
    base = _main_soc(before) if stored is None else int(stored)
    latched = bool(after.get(LATCH_SET))
    if not latched and after["charge_discharge_status"] == FLOW_CHARGE:
        base = _main_soc(after)
    elif not latched and _latch_arms(after, base):
        latched, base = True, _main_soc(after)
    marks: dict[str, Value] = {}
    if stored is None or base != int(stored):
        marks[LATCH_FROM] = base
    if latched and not after.get(LATCH_SET):
        marks |= {LATCH_SET: 1, CAP_LATCH: 0}
    events = []
    if marks:
        events.append(Timed(0.0, LATCH_REFERENCE, partial(_latch_marks, marks)))
    if latched and _expansion_takes_discharge(after) and not _latch_hands_over(before):
        gap = max(0.0, SWITCH_GAP - _held_for(after, SWITCH_GAP))
        events.append(Timed(gap, PACK_HANDOVER, _discharge_to_expansion))
    elif (
        not latched and _charge_handover_due(after) and not _charge_handover_due(before)
    ):
        charge = partial(_charge_to_expansion, own)
        events.append(Timed(0.0, PACK_HANDOVER, charge))
    return events


def _latch_arms(state: Mapping[str, Value], base: int) -> bool:
    """Return whether the main's >= 1.0 % drop from ``base`` arms the latch (:194 (a)).

    The main must not be charging (current >= -0.1 A) and its own SoC must be
    above ``min_soc`` + 0.9 %.
    """
    soc = _main_soc(state)
    return (
        state["charge_discharge_status"] != FLOW_CHARGE
        and soc > _expansion_floor_line(state)
        and soc <= base - DISCHARGE_LATCH_DROP
    )


def _latch_hands_over(state: Mapping[str, Value]) -> bool:
    """Return whether :164 holds: the latch set, the expansion able to discharge."""
    return bool(state.get(LATCH_SET)) and _expansion_takes_discharge(state)


def _latch_marks(
    marks: Mapping[str, Value],
    _values: Mapping[str, Value],
    _state: Mapping[str, Value],
) -> Step:
    """Store a latch's new state, or the discharge latch's new baseline."""
    return Step(values=dict(marks))


def _cap_latch_set(
    own: frozenset[str], _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Set the charge-cap latch after 1 s counting, and hand the charge over (:165).

    Setting it clears the discharge latch and re-takes its baseline (:194).
    """
    if not _cap_latch_counts(state):
        return Step()
    changes: dict[str, Value] = {
        CAP_LATCH: 1,
        LATCH_SET: 0,
        LATCH_FROM: _main_soc(state),
    }
    if _charge_handover_due({**state, **changes}):
        changes |= _without(_charge_handover(state), own)
    return Step(values=changes)


def _charge_handover(state: Mapping[str, Value]) -> dict[str, Value]:
    """Return the main's charge moved to the expansion (2026-10-06 20:20:03).

    ``a3[0]`` and ``a5[1]`` read 0 at once, the expansion holding the bus.
    """
    return {
        "work_status": FLOW_IDLE,
        "charge_discharge_status": FLOW_IDLE,
        "sub_package_status": FLOW_CHARGE,
        **_bus_to(BUS_EXPANSION, state, flowing=True),
    }


def _charge_to_expansion(
    own: frozenset[str], _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Hand the main's charge to the expansion, the cap latch already set."""
    return Step(values=_without(_charge_handover(state), own))


def _discharge_to_expansion(
    _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Hand discharge to the expansion, the discharge latch set.

    ``a3[0]`` and ``a5[1]`` read 0 at once and ``c0[24]`` the port flow.
    """
    if not _latch_hands_over(state):
        return Step()
    output = bool(state["total_output_power"])
    changes: dict[str, Value] = {
        "work_status": FLOW_IDLE,
        "charge_discharge_status": FLOW_IDLE,
        "sub_package_status": FLOW_OUT if output else FLOW_IDLE,
        **_bus_to(BUS_EXPANSION, state, flowing=output),
    }
    return Step(values=changes)


def _bus_to_main(values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Hand discharge to the main, the expansion under its line (03:29:25)."""
    if not _main_due(state):
        return Step()
    return _switched_off(values, state)


def _switched_off(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Hand discharge to the main: the selection's ``0x21``.

    ``c0[24]`` reads 0 and ``a5[1]`` the port flow at once, ``a3[0]`` following.
    """
    output = bool(state["total_output_power"])
    changes: dict[str, Value] = {
        "sub_package_status": FLOW_IDLE,
        "charge_discharge_status": FLOW_OUT if output else FLOW_IDLE,
        **_bus_to(BUS_MAIN, state, flowing=output),
    }
    return _flow_step(changes)


#: The AC top-off (a1783-telemetry.md "AC top-off burst", "Top-off hysteresis"):
#: the on-bus pack's charge reopens once its 0.1 % SoC (``pack_soc``) falls
#: below the clear threshold, and AC charges ~1 min until it is past the set
#: one (2026-10-20 04:57:40, off by 04:59). ``soc_status_thresholds_load``:
#: set / clear = ``max_soc`` + 0.7 % / ``max_soc``; 98.9 / 98.5 % at a 99 %
#: cap, 100.0 / 99.9 % at 100 %.
TOP_OFF_MARGIN = 7
TOP_OFF_BURST = 60.0


def _top_off_band(cap: int) -> tuple[int, int]:
    """Return the top-off (set, clear) thresholds for ``cap``, in 0.1 %."""
    if cap * SOC_STEPS_PER_PERCENT >= SOC_FULL:
        return SOC_FULL, SOC_FULL - 1
    if cap == 99:  # noqa: PLR2004  # the firmware's own 99 % case
        return 989, 985
    return cap * SOC_STEPS_PER_PERCENT + TOP_OFF_MARGIN, cap * SOC_STEPS_PER_PERCENT


def _top_offs(
    before: Mapping[str, Value], after: Mapping[str, Value], own: frozenset[str]
) -> list[Timed]:
    """Return the top-off burst the main's SoC falling below the clear threshold draws.

    Only AC into the main on the bus: mains in, Battery-Only Power off, and the
    expansion not on the bus (``c0[24]`` 0, the closest the frames show).
    """
    _, clear = _top_off_band(int(after["charging_limit_value"]))
    if not int(before[PACK_SOC]) >= clear > int(after[PACK_SOC]):
        return []
    if after["ac_input_status"] != AC_INPUT_MAINS or after[DISABLE] == BATTERY_ONLY_ON:
        return []
    if after.get("sub_package_status", FLOW_IDLE) != FLOW_IDLE:
        return []
    return [Timed(0.0, "top_off", partial(_top_off, own))]


def _top_off(
    own: frozenset[str], _values: Mapping[str, Value], _state: Mapping[str, Value]
) -> Step:
    """Start the top-off burst: AC in, ``a5[1]`` 2 with ``a3[0]`` following."""
    changes: dict[str, Value] = {
        "ac_input_power": AC_CHARGE_POWER,
        "charge_discharge_status": FLOW_CHARGE,
    }
    end = Timed(TOP_OFF_BURST, "top_off_end", _top_off_end)
    return _flow_step(_without(changes, own), (end,))


def _top_off_end(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """End the burst at the set threshold: AC in 0 W, ``a5[1]`` the port flow.

    The burst refills a dip just under the clear threshold; from deeper the
    charge runs on to the set threshold, which no value models.
    """
    top, clear = _top_off_band(int(state["charging_limit_value"]))
    if int(state[PACK_SOC]) < clear - 1:
        return Step()
    flow = FLOW_OUT if state["total_output_power"] else FLOW_IDLE
    changes: dict[str, Value] = {
        "ac_input_power": 0,
        "charge_discharge_status": flow,
        PACK_SOC: top,
    }
    return _flow_step(changes)


#: The OTA precheck (``ota_precheck_update``, a1783-control.md ``4089``): 0 2
#: while the main's SoC, or an attached expansion's, is under 6 % (whole
#: percent), else 1 0. A changed pair pushes ``0489`` 0.15 s after the frame
#: (MQTT 2026-10-06 20:21:28, BLE 2026-10-15 07:45:55). Upgrade and BMS boot
#: mode aren't modeled.
OTA_SOC_MIN = 6
OTA_ALLOWED = (1, 0)
OTA_LOW_SOC = (0, 2)
OTA_PRECHECK_DELAY = 0.15
OTA_PAIR = ("ota_enable", "ota_block_reason")


def _ota_pair(state: Mapping[str, Value]) -> tuple[int, int]:
    """Return the precheck pair the SoCs in ``state`` give."""
    levels = [int(state["battery_level"])]
    if state.get("sub_package_connection_status"):
        levels.append(int(state["sub_package_battery_level"]))
    return OTA_LOW_SOC if min(levels) < OTA_SOC_MIN else OTA_ALLOWED


def _ota_prechecks(
    before: Mapping[str, Value], after: Mapping[str, Value], own: frozenset[str]
) -> list[Timed]:
    """Return the ``0489`` a SoC change that changes the pair draws.

    The pair compared is the one the SoCs give, not the recorded one; a call
    that sets the pair itself keeps it.
    """
    if own & set(OTA_PAIR) or _ota_pair(before) == _ota_pair(after):
        return []
    return [Timed(OTA_PRECHECK_DELAY, "ota_precheck", _ota_precheck)]


def _ota_precheck(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Set the precheck pair and push ``0489`` with it."""
    return Step(
        values=dict(zip(OTA_PAIR, _ota_pair(state), strict=True)), pushes=(MSGTYPE_489,)
    )


#: Battery-Only Power at the main's floor (a1783-operation.md "Battery-Only Power
#: at the floor", 2026-10-15): the main pack's BMS raises its low-battery bit
#: under ``min_soc`` + 0.9 % (a1783-control.md ``4101 ab`` clearing), which drops
#: the mode to state 2 with mains accepted (05:45:36); AC charging starts 4 s
#: later (05:45:40); back over it, state 2 clears 1 s later with the screen woken
#: (05:46:20 -> :21). The ``dc`` low-battery code, the outputs' dip and the
#: min-cell path aren't modeled.
LOW_BATTERY_MARGIN = 9
FLOOR_CHARGE_DELAY = 4.0
FLOOR_CLEAR_DELAY = 1.0


def _floor_exits(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> list[Timed]:
    """Return the drop or clear the main crossing its low-battery line draws."""
    line = int(after["discharge_limit_value"]) * SOC_STEPS_PER_PERCENT
    line += LOW_BATTERY_MARGIN
    was, now = int(before[PACK_SOC]), int(after[PACK_SOC])
    held = after[DISABLE] == BATTERY_ONLY_ON
    if was >= line > now and held and after["ac_input_status"] == AC_INPUT_BATTERY_ONLY:
        return [Timed(0.0, "floor_exit", _floor_exit)]
    if was < line <= now and after[DISABLE] == BATTERY_ONLY_AUTO_OFF:
        return [Timed(FLOOR_CLEAR_DELAY, "floor_exit_clear", _floor_exit_clear)]
    return []


def _floor_exit(_values: Mapping[str, Value], _state: Mapping[str, Value]) -> Step:
    """Drop Battery-Only Power to state 2, mains accepted, AC charging following."""
    charge = Timed(FLOOR_CHARGE_DELAY, "ac_charge", partial(_ac_charge, frozenset()))
    changes = {DISABLE: BATTERY_ONLY_AUTO_OFF, "ac_input_status": AC_INPUT_MAINS}
    return Step(values=changes, then=(charge,))


def _floor_exit_clear(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Clear state 2 and wake the screen (05:46:21)."""
    if state[DISABLE] != BATTERY_ONLY_AUTO_OFF:
        return Step()
    wake = Timed(0.0, SCREEN_WAKE, partial(_screen_wake, frozenset()))
    return Step(values={DISABLE: BATTERY_ONLY_OFF}, then=(wake,))


def _other_input_charging(params: ParameterDict) -> bool:
    """Whether a DC input or the alternator charger feeds the unit."""
    if decode_field(params, "a8", DcInput).pv1_input_power:
        return True
    if "ce" not in params:
        return False
    mode = decode_field(params, "ce", Alternator).external_device_mode
    return bool(mode == ExternalDeviceMode.CHARGING)


def _battery_only_on(params: ParameterDict) -> bool:
    """Whether ``a4[31]`` holds Battery-Only Power on (state 1, AC input disabled)."""
    return bool(decode_field(params, "a4", Settings)[DISABLE] == BATTERY_ONLY_ON)


#: ``b2[0]``: the 12 V car socket's output status, whose edges renegotiate the
#: USB-C ports (a1783-control.md "``4102 a2`` and the USB-C ports").
CAR_SOCKET = "car_charge1_output_status"
#: The USB-C PD ports (``aa``/``ab``/``ac``) a car-socket edge re-attaches.
USB_C_PORTS = ("pd1", "pd2", "pd3")
#: Hidden per-port inputs no field carries and the app has no name for: a device
#: in the port (0/1) and the W it draws once attached. Unset, PD1 holds the A1783
#: recording's 86 W load (86 W x34, 87 W x17 in the collector logs); PD2 and PD3
#: hold none.
DEVICE_PLUGGED = "device_plugged"
DEVICE_POWER = "device_power"
USB_C_DEVICES = MappingProxyType(
    {
        "pd1_device_plugged": 1,
        "pd1_device_power": 86,
        "pd2_device_plugged": 0,
        "pd2_device_power": 0,
        "pd3_device_plugged": 0,
        "pd3_device_power": 0,
    }
)
#: A detached port's status 1 after the edge: 1 s (0-3 s; the median of the 55
#: collector clusters with PD1 detached, 5 s once on an off edge alone).
PORT_ATTACH_DELAY = 1.0
#: Its device's power after the status: 3 s (+3-8 s from the edge, median +4 s).
PORT_POWER_DELAY = 3.0


def _usb_c_device(state: Mapping[str, Value], port: str, field: str) -> int:
    """Return a port's hidden device input, its default unless set."""
    name = f"{port}_{field}"
    return int(state.get(name, USB_C_DEVICES[name]))


def _socket_edge(after: Mapping[str, Value], own: frozenset[str]) -> list[Timed]:
    """Return the re-attach of each detached USB-C port with a device in it.

    A port reading 1 holds through the edge (2026-10-07 21:05:16).
    """
    return [
        Timed(PORT_ATTACH_DELAY, f"{port}_attach", partial(_port_attach, port, own))
        for port in USB_C_PORTS
        if after[f"{port}_output_status"] == 0
        and _usb_c_device(after, port, DEVICE_PLUGGED)
    ]


def _port_attach(
    port: str,
    own: frozenset[str],
    _values: Mapping[str, Value],
    _state: Mapping[str, Value],
) -> Step:
    """Re-attach ``port`` (status 1), its device's power following.

    A power the edge's own change set stands, so no power step follows it.
    """
    power = f"{port}_output_power"
    then = (
        ()
        if power in own
        else (
            Timed(PORT_POWER_DELAY, f"{port}_power", partial(_port_power, port, own)),
        )
    )
    return Step(values=_without({f"{port}_output_status": 1}, own), then=then)


def _port_power(
    port: str,
    own: frozenset[str],
    _values: Mapping[str, Value],
    state: Mapping[str, Value],
) -> Step:
    """Draw the device's power on an attached ``port``, into the output total.

    ``a5[1]`` goes 0 -> 1 when that makes the unit discharge (2026-09-01 02:26:46).
    A port detached again first draws nothing (2026-09-24 05:18:22).
    """
    if state[f"{port}_output_status"] != 1:
        return Step()
    power = _usb_c_device(state, port, DEVICE_POWER)
    total = int(state["total_output_power"]) + power
    total -= int(state[f"{port}_output_power"])
    changes: dict[str, Value] = {
        f"{port}_output_power": power,
        "total_output_power": total,
    }
    if state["charge_discharge_status"] == FLOW_IDLE and total:
        changes["charge_discharge_status"] = FLOW_OUT
    return _flow_step(_without(changes, own))


#: ``0403``: MainMcu's own event push, ``a1 01 31 · a2 02 01 <code>`` with no
#: ``fe``, one frame per code (a1783-control.md "``0403`` push").
MSGTYPE_403 = 0x403
EVENT_ROUTE = ROUTE_LAYOUT.build(Container(destination=ROUTE_APP, source=ROUTE_BLE))
EVENT_SOC_EMPTY = 0x50
EVENT_SOC_LOW = 0x52
EVENT_AC_TIMEOUT = 0x55
EVENT_CAR_TIMEOUT = 0x56
#: Main-pack SoC floor (%) -> its code: 10 % (the 2026-10-15 05:00:23 witness)
#: and 1 % (firmware only, ``soc_floor_event_eval(1, ...)``).
SOC_FLOORS = MappingProxyType({10: EVENT_SOC_LOW, 1: EVENT_SOC_EMPTY})
#: The alert after the frame in which the SoC reached the floor: 3 evaluations
#: at or below it, one per MainBMS block-0 reply (500 ms) and, with an expansion
#: attached (``sub_package_connection_status``), one per block-1 reply too. So
#: 0.49 s with one (the 2026-10-15 witness) and 1 s without.
SOC_ALERT_DELAY = 0.49
SOC_ALERT_DELAY_ALONE = 1.0
#: The output timers step 2 s at a time (a 2000 ms gate subtracts 2): ``a4`` reads
#: only even seconds, and 300 s ran out 300.8 s and 301.7 s after the set
#: (app_log 2026-07-19). With the output off, each 10 ms pass clears the timer.
OUTPUT_TIMER_TICK = 2
OUTPUT_TIMER_CLEAR = 0.01
#: Timer -> (the output it switches off, its timeout code).
OUTPUT_TIMERS = MappingProxyType(
    {
        "ac_timer_value": ("ac_output_status", EVENT_AC_TIMEOUT),
        "car_charger_timer_value": (CAR_SOCKET, EVENT_CAR_TIMEOUT),
    }
)


def _event_403(code: int) -> tuple[int, bytes]:
    """Return a ``0403`` event push carrying ``code``."""
    route = ParameterContainer(key=b"\xa1", type=None, value=EVENT_ROUTE)
    event = container("a2", TYPE_UI, ui(1).build(code))
    return MSGTYPE_403, Parameters.build(ParameterDict({"a1": route, "a2": event}))


def _timers_started(own: frozenset[str], after: Mapping[str, Value]) -> list[Timed]:
    """Return each timer's countdown, or its clearing, a change starts.

    A timer counts only while its output is on; with the output off, the next
    10 ms pass clears it, so one set then reads 0 and switching the output off
    discards the rest (a1783-control.md "Output timers").
    """
    events = []
    for timer, (output, _) in OUTPUT_TIMERS.items():
        if (timer in own or output in own) and after[timer]:
            if after[output]:
                tick = partial(_timer_tick, timer)
                events.append(Timed(OUTPUT_TIMER_TICK, timer, tick))
            else:
                clear = partial(_step, {timer: 0})
                events.append(Timed(OUTPUT_TIMER_CLEAR, timer, clear))
    return events


def _timer_tick(
    timer: str, _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Count ``timer`` down one tick; at 0 switch its output off.

    The ``0403`` with its timeout code follows (2026-07-19 12:53:03.318), and a
    car socket going off is a socket edge. A timer set to 0 meanwhile stops.
    """
    output, code = OUTPUT_TIMERS[timer]
    if not state[timer]:
        return Step()
    left = max(0, int(state[timer]) - OUTPUT_TIMER_TICK)
    if left:
        tick = Timed(OUTPUT_TIMER_TICK, timer, partial(_timer_tick, timer))
        return Step(values={timer: left}, then=(tick,))
    off: dict[str, Value] = {timer: 0, output: 0}
    edge = _socket_edge({**state, **off}, frozenset()) if output == CAR_SOCKET else []
    return Step(values=off, frames=(_event_403(code),), then=tuple(edge))


def _soc_floors_crossed(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> list[Timed]:
    """Return the alert of each SoC floor the main pack's SoC fell to or below.

    It re-arms only once the SoC is above the floor again.
    """
    level_before, level = int(before["battery_level"]), int(after["battery_level"])
    attached = after.get("sub_package_connection_status")
    delay = SOC_ALERT_DELAY if attached else SOC_ALERT_DELAY_ALONE
    return [
        Timed(delay, f"soc_floor_{floor}", partial(_soc_alert, floor))
        for floor in SOC_FLOORS
        if level_before > floor >= level
    ]


def _soc_alert(
    floor: int, _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Push the floor's ``0403`` if the SoC is still at or below it.

    One evaluation above the floor resets the count, so a SoC back above it
    first sends none.
    """
    if int(state["battery_level"]) > floor:
        return Step()
    return Step(frames=(_event_403(SOC_FLOORS[floor]),))


#: ``a4[21]``: the screen on/off, and ``a4[15..16]`` its timeout in seconds (0 =
#: never). a1783-control.md "``4103``".
SCREEN = "screen_switch"
SCREEN_OFF_TIME = "screen_off_time"
SCREEN_WAKE = "screen_wake"
#: The changes that wake the screen: in the app_log, every write sent with the
#: screen off of ``4103 a3`` (brightness), ``a9`` (language) and ``4102 a2`` (the
#: car socket) read it on in the next frame; none of ``4101 a2``/``a4``/``a6``,
#: ``4102 a4``, ``4103 a4``/``a8``/``aa``/``b0``, ``4104`` or the ``40xx`` ops did.
SCREEN_WAKES = frozenset({"lcd_backlight_brightness", "device_language", CAR_SOCKET})


def _screen_wakes(own: frozenset[str], after: Mapping[str, Value]) -> list[Timed]:
    """Return the wake a waking change, or the screen set on, starts."""
    if own & SCREEN_WAKES or (SCREEN in own and after[SCREEN]):
        return [Timed(0.0, SCREEN_WAKE, partial(_screen_wake, own))]
    return []


def _screen_wake(
    own: frozenset[str], _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Turn the screen on and (re-)arm its timeout (``screen_wake_apply``).

    It goes off exactly the timeout later (a1783-control.md "``4103``"; 300.2 s
    and 300.8 s after the 2026-08-27 wakes).
    """
    timeout = int(state[SCREEN_OFF_TIME])
    then = (Timed(timeout, "screen_off", _screen_off),) if timeout else ()
    return Step(values=_without({SCREEN: 1}, own), then=then)


def _screen_off(_values: Mapping[str, Value], _state: Mapping[str, Value]) -> Step:
    """Turn the screen off at its timeout."""
    return Step(values={SCREEN: 0})


#: The ``0490`` post period, on one grid from the display board's start: neither
#: a stop, the switch nor the link resets it (2026-10-07 20:34:08 -> 22:40:07 =
#: 14 x 540 s; 2026-09-19 04:48:33 -> 18:54:34 = 94 x 540 s). a1783-telemetry.md
#: "When the display board posts c490".
C490_PERIOD = 540.0
C490_POST = "c490_post"


def _c490_post(_values: Mapping[str, Value], _state: Mapping[str, Value]) -> Step:
    """Post the ``0490``, the device-point switch and the link allowing, and re-arm.

    A post due with the switch off or no authorized link goes unsent.
    """
    return Step(
        pushes=(MSGTYPE_490,), then=(Timed(C490_PERIOD, C490_POST, _c490_post),)
    )


#: The ``0503`` usage pass: a 10 s timer from the display board's start that,
#: while a TOU period is active, adds 10 s to that period type's time; every 120
#: passes fold into the lifetime totals, and pass 720 (2 h) flushes and starts
#: the count again (a1783-telemetry.md "0503" Trigger). The energy deltas
#: (SolarToBatt / GridToBatt / BattToLoad) need the pack's charge power, which
#: no value models, so those totals change only by name. The 2 h publish isn't
#: built.
USAGE_PASS = 10
USAGE_FOLD_PASSES = 120
USAGE_FLUSH_PASSES = 720
USAGE_EVENT = "tou_usage_pass"
#: Hidden: the passes counted since the last flush; no field carries it.
USAGE_PASSES = "tou_usage_passes"
#: Period type -> its ``0503`` time total (``[12]`` / ``[16]`` / ``[20]``).
USAGE_TOTALS = MappingProxyType(
    {
        TouPeriodType.PEAK: "peak_time",
        TouPeriodType.MID_PEAK: "mid_peak_time",
        TouPeriodType.OFF_PEAK: "off_peak_time",
    }
)
#: Hidden: each total's seconds since the last fold, ``<total>_delta``.
USAGE_DELTA = "{}_delta"


def _usage_pass(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Count one 10 s pass in the active TOU period, folding and flushing on count."""
    then = (Timed(USAGE_PASS, USAGE_EVENT, _usage_pass),)
    plan = _tou_periods_of(_bytes_value(state, TOU_TABLE))
    period = _period_of({**state, "tou_periods": plan})
    if period == TouPeriodType.NONE:
        return Step(then=then)
    delta = USAGE_DELTA.format(USAGE_TOTALS[TouPeriodType(period)])
    count = int(state.get(USAGE_PASSES, 0)) + 1
    changes: dict[str, Value] = {delta: int(state.get(delta, 0)) + USAGE_PASS}
    if count % USAGE_FOLD_PASSES == 0:
        for total in USAGE_TOTALS.values():
            name = USAGE_DELTA.format(total)
            folded = int(changes.get(name, state.get(name, 0)))
            changes[total] = int(state[total]) + folded
            changes[name] = 0
    if count < USAGE_FLUSH_PASSES:
        changes[USAGE_PASSES] = count
        return Step(values=changes, then=then)
    rtc = int(state[RTC])
    changes |= {USAGE_PASSES: 0, "time_save": rtc - rtc % 60}
    flushed = _tou_totals({**state, **changes})
    return Step(values=changes, published=((MSGTYPE_503, flushed),), then=then)


def _tou_totals(state: Mapping[str, Value]) -> bytes:
    """Return the ``0503`` cleartext of ``state``'s totals, at its MQTT ``a1`` 12.

    The flush stamps Time Save with the RTC to the whole minute; an active
    period needs the cloud, so the send is never dropped here.
    """
    route = ParameterContainer(key=b"\xa1", type=None, value=STATE_INFO_ROUTE)
    fields = ParameterDict({"a1": route})
    for tag, (codec, type_byte, zeros) in TOU_TOTALS_FIELDS.items():
        named = {name: state[name] for name in zeros}
        fields[tag] = container(tag, type_byte, codec.build(named))
    return Parameters.build(fields)


# Backup plans (405e) and their windows: a1783-control.md "005e means different
# things across families" (the request and the handler) and "d9" tail / "0425
# event report" (the windows). Table 1 is the manual plan (the app's Fast
# Charging Plan, d9 tail [15..22]); table 2 holds three auto (Storm Guard)
# entries, which no field carries.
BACKUP_GROUP = 3
BACKUP_CLEAR_ALL = 2
WINDOW_NONE = 0
WINDOW_MANUAL = 1
WINDOW_AUTO = 2
MANUAL_SWITCH = "manual_backup_switch"
AUTO_SWITCH = "auto_backup_switch"
MANUAL_START = "manual_backup_start_time"
MANUAL_END = "manual_backup_end_time"
WINDOW_TYPE = "tou_setting_system_status"
WINDOW_START = "backup_start_time"
WINDOW_END = "backup_end_time"
#: Hidden: table 2, three ``{u32 start, u32 end}`` UTC entries; empty unset.
AUTO_PLANS = "auto_backup_plans"
AUTO_PLAN_COUNT = 3
AutoPlans = Array(AUTO_PLAN_COUNT, Struct("start" / ui(4), "end" / ui(4)))
AUTO_PLANS_EMPTY = bytes(AutoPlans.sizeof())
#: ``405e a7``: one plan per 10 B, ``{max SoC, -, u32 start, u32 end}``.
BackupPlan = Struct("max_soc" / ui(1), Padding(1), "start" / ui(4), "end" / ui(4))
#: The record a cancel writes over a plan (flash 0x08035d38): never active.
NEVER = 0xFFFFFFFF
#: The window pass: every 100 ms (``charge_policy_change_report``), and no window
#: before the RTC reads 2025 (``rtc_year_valid?``).
BACKUP_PASS = 0.1
BACKUP_WINDOW_EVENT = "backup_window"
RTC_2025 = 1735689600
#: ``0425`` window events: (start, end) by window type.
WINDOW_EVENTS = MappingProxyType(
    {WINDOW_MANUAL: (0x0A, 0x0B), WINDOW_AUTO: (0x07, 0x08)}
)
BACKUP_NAMES = frozenset(
    {MANUAL_SWITCH, AUTO_SWITCH, MANUAL_START, MANUAL_END, AUTO_PLANS, EPOCH, RTC}
)


def _set_backup_params(request: ParameterDict) -> dict[str, Value] | None:
    """Accept a ``405e`` whose ``a3`` group selector is 3; ``_backup_plan`` acts."""
    if "a3" not in request or decode_field(request, "a3", ui(1)) != BACKUP_GROUP:
        return None
    return {}


def _covers(window: list[int], now: int) -> bool:
    """Whether the ``[start, end]`` UTC ``window`` covers ``now``."""
    return window[0] <= now < window[1]


def _auto_plans(state: Mapping[str, Value]) -> list[list[int]]:
    """Return table 2's entries as [start, end] UTC epochs."""
    plans = _bytes_value(state, AUTO_PLANS) if AUTO_PLANS in state else AUTO_PLANS_EMPTY
    return [[entry.start, entry.end] for entry in AutoPlans.parse(plans)]


def _backup_plan(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Return what a ``405e`` does to the switches and tables, in the firmware's order.

    ``a4`` 2 clears both tables and switches and reads nothing else
    (``disaster_plan_clear_all``). Otherwise ``a5`` (with the plan switched off,
    every Storm Guard entry covering now becomes the never record), ``aa`` 1 (a
    cancel, only while a window runs: the running entry and every Storm Guard
    entry covering now become the never record, and a Storm Guard cancel turns
    the plan off where table 1 covers now), then ``a7``'s plans (the manual one
    into table 1, auto ones into table 2) (a1783-control.md "``405e``" notes).
    """
    mode = decode_field(request, "a4", ui(1))
    if mode == BACKUP_CLEAR_ALL:
        return {
            MANUAL_SWITCH: 0,
            AUTO_SWITCH: 0,
            MANUAL_START: 0,
            MANUAL_END: 0,
            AUTO_PLANS: AUTO_PLANS_EMPTY,
        }
    manual = mode == 0
    now = int(state[EPOCH])
    table_1 = [int(state[MANUAL_START]), int(state[MANUAL_END])]
    table_2 = _auto_plans(state)
    changes: dict[str, Value] = {}

    def wipe_storm_guard() -> None:
        for entry in table_2:
            if _covers(entry, now):
                entry[:] = [NEVER, NEVER]

    if "a5" in request:
        switch = decode_field(request, "a5", ui(1))
        changes[MANUAL_SWITCH if manual else AUTO_SWITCH] = switch
        if manual and not switch:
            wipe_storm_guard()
    running = int(state[WINDOW_TYPE])
    if "aa" in request and decode_field(request, "aa", ui(1)) and running:
        if running == WINDOW_MANUAL:
            table_1 = [NEVER, NEVER]
        if running == WINDOW_AUTO and _covers(table_1, now):
            changes[MANUAL_SWITCH] = 0
        wipe_storm_guard()
    if "a7" in request:
        count = decode_field(request, "a6", ui(1)) if "a6" in request else 1
        plans = GreedyRange(BackupPlan).parse(decode_field(request, "a7", GreedyBytes))
        if manual and plans:
            table_1 = [plans[0].start, plans[0].end]
        for index, plan in enumerate(plans[: min(count, AUTO_PLAN_COUNT)]):
            if not manual:
                table_2[index] = [plan.start, plan.end]
    changes[MANUAL_START], changes[MANUAL_END] = table_1
    entries: list[dict[str, Any] | None] = [
        {"start": start, "end": end} for start, end in table_2
    ]
    changes[AUTO_PLANS] = AutoPlans.build(entries)
    return changes


def _backup_passes(own: frozenset[str], reports: bool) -> list[Timed]:  # noqa: FBT001  # the model's flag
    """Return the next window pass a change of a plan, a switch or the clock draws."""
    if own & BACKUP_NAMES:
        window = partial(_backup_window, reports)
        return [Timed(BACKUP_PASS, BACKUP_WINDOW_EVENT, window)]
    return []


def _backup_window(
    reports: bool,  # noqa: FBT001  # the model's flag
    _values: Mapping[str, Value],
    state: Mapping[str, Value],
) -> Step:
    """Run the window pass: start, end or hand over the backup window that covers now.

    A running window keeps going until it ends or its switch goes off; with
    none running, Storm Guard (table 2) is chosen before the plan (table 1). A
    start posts ``0x0a`` / ``0x07`` with its epochs and moves an expansion's
    charge to a main pack below 100 %; an end posts ``0x0b`` / ``0x08``; a
    hand-over between types changes the type with no ``0425``. No window runs
    before the RTC reads 2025 (a1783-control.md ``d9`` type row, "``0425``").
    """
    now = int(state[EPOCH])
    manual = [int(state[MANUAL_START]), int(state[MANUAL_END])]
    covering = {
        WINDOW_AUTO: next(
            (e for e in _auto_plans(state) if state[AUTO_SWITCH] and _covers(e, now)),
            None,
        ),
        WINDOW_MANUAL: manual
        if state[MANUAL_SWITCH] and _covers(manual, now)
        else None,
    }
    running = int(state[WINDOW_TYPE])
    if int(state[RTC]) < RTC_2025:
        target, window = WINDOW_NONE, None
    elif running and covering[running] is not None:
        target, window = running, covering[running]
    else:
        target = next((t for t in (WINDOW_AUTO, WINDOW_MANUAL) if covering[t]), 0)
        window = covering.get(target)
    start, end = window or (0, 0)
    changes: dict[str, Value] = {
        WINDOW_TYPE: target,
        WINDOW_START: start,
        WINDOW_END: end,
    }
    pushes: tuple[int, ...] = ()
    if running == WINDOW_NONE and target:
        changes |= _window_event(WINDOW_EVENTS[target][0], start, end)
        changes |= _charge_to_main(state)
        pushes = (MSGTYPE_425,)
    elif running and target == WINDOW_NONE:
        changes |= _window_event(WINDOW_EVENTS[running][1], 0, 0)
        pushes = (MSGTYPE_425,)
    edges = [
        t for t in (*manual, *(t for e in _auto_plans(state) for t in e)) if t > now
    ]
    then: tuple[Timed, ...] = ()
    if edges and min(edges) < NEVER:
        # The epoch is whole seconds: within one of the edge, pass every 100 ms.
        delay = max(min(edges) - now - 1, 0) or BACKUP_PASS
        later = partial(_backup_window, reports)
        then = (Timed(delay, BACKUP_WINDOW_EVENT, later),)
    step = _flow_step(changes, then)
    return Step(values=step.values, pushes=pushes if reports else (), then=step.then)


def _window_event(code: int, start: int, end: int) -> dict[str, Value]:
    """Return a window ``0425``'s values: the event and its epochs."""
    return {
        "event_code": code,
        "event_start_timestamp": start,
        "event_end_timestamp": end,
    }


def _charge_to_main(state: Mapping[str, Value]) -> dict[str, Value]:
    """Move an expansion's charge to a main pack below 100 % (21:00:04).

    ``a5[1]`` goes 2 at once and ``a3[0]`` follows.
    """
    if state.get("sub_package_status") != FLOW_CHARGE:
        return {}
    if int(state["battery_level"]) >= SOC_FULL // SOC_STEPS_PER_PERCENT:
        return {}
    return {"sub_package_status": FLOW_IDLE, "charge_discharge_status": FLOW_CHARGE}


#: ``4091`` tag -> (the ``da`` leaf it writes, its width): a1783-control.md
#: "Clock screen (4091 -> da)".
CLOCK_TAGS = {
    "a3": ("network_theme_id", ui(4)),
    "a4": ("network_theme_crc", ui(4)),
    "a5": (LCD_THEME_NAME, GreedyBytes),
    "a7": ("lcd_time_format", ui(1)),
    "a8": ("lcd_start_time", ui(2)),
    "a9": ("lcd_end_time", ui(2)),
    "aa": ("lcd_repeat_cycle", ui(1)),
    "ab": ("day_night_switch", ui(1)),
    "ac": ("day_brightness_level", ui(1)),
    "ad": ("night_brightness_level", ui(1)),
    "ae": ("night_start_time", ui(2)),
    "af": ("night_end_time", ui(2)),
}
#: ``4091 a2``: the screensaver switch (bit 7) over the clock theme.
ClockTheme = BitStruct("screen_saver_switch" / Flag, "lcd_theme" / BitsInteger(7))


def _set_screen_saver_params(request: ParameterDict) -> dict[str, Value] | None:
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


def _set_bms_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return ``c0[28]``'s override, the app's ``a4``; ``a5`` (blink) sets none."""
    return decode_request(request, EXPANSION_GROUP)


#: ``4104 a4`` (a1783-control.md "``4104``"): ``c0[28]`` shows the app's value
#: until the block-0xf reread 2 s later pushes a ``0421`` with the expansion's
#: own switch-on state (``flag_0b56``; hidden, unset = on). An on reaches the
#: expansion; an off only with no external charge source (``flag_00c7``:
#: ``.14.30`` AC in, ``a7[3]`` 1 or 2, PV in, or the alternator charger in its
#: Charging Mode), the main then taking the bus.
EXPANSION_SWITCHED_ON = "expansion_switched_on"
SWITCH_REQUESTS = "expansion_switch_requests"
SWITCH_REREAD = 2.0
SWITCH_REREAD_EVENT = "expansion_switch_reread"


def _charge_source(state: Mapping[str, Value]) -> bool:
    """Return whether an external charge source is in (``flag_00c7``).

    AC in, PV / DC in, or the alternator charger in its Charging Mode.
    """
    return (
        state["ac_input_status"] != AC_INPUT_NONE
        or bool(state["pv1_input_power"])
        or state.get("external_device_mode") == ExternalDeviceMode.CHARGING
    )


def _expansion_on(state: Mapping[str, Value]) -> bool:
    """Return the expansion's own switch-on state (``flag_0b56``)."""
    return bool(state.get(EXPANSION_SWITCHED_ON, 1))


def _expansion_switch(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Return what a ``4104 a4`` does to the expansion: an on, or an unblocked off."""
    if "a4" not in request:
        return {}
    changes: dict[str, Value] = {
        SWITCH_REQUESTS: int(state.get(SWITCH_REQUESTS, 0)) + 1
    }
    if decode_field(request, "a4", ui(1)):
        changes[EXPANSION_SWITCHED_ON] = 1
    elif not _charge_source(state):
        changes[EXPANSION_SWITCHED_ON] = 0
    return changes


def _switch_reread(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Clear the override: ``c0[28]`` reads the expansion's own state, pushed."""
    own = int(_expansion_on(state))
    return Step(values={"sub_package_switch_status": own}, pushes=(MSGTYPE_421,))


def _switch_events(
    before: Mapping[str, Value], after: Mapping[str, Value], own: frozenset[str]
) -> list[Timed]:
    """Return the reread a ``4104 a4`` arms and the bus move an off draws."""
    events = []
    if SWITCH_REQUESTS in own:
        events.append(Timed(SWITCH_REREAD, SWITCH_REREAD_EVENT, _switch_reread))
    if (
        _expansion_on(before)
        and not _expansion_on(after)
        and _bus_pack(after) == BUS_EXPANSION
    ):
        events.append(Timed(0.0, PACK_HANDOVER, _switched_off))
    return events


#: ``4051``'s restored settings (a1783-control.md "``4051`` factory defaults"):
#: the default block (v1.2.1.1 0x08035324) at each ``a4``/``d9`` field it
#: feeds, and Ultrafast off. The region-recomputed recharge power, frequency,
#: temperature unit and language keep the unit's own values.
FACTORY_SETTINGS: dict[str, Value] = {
    "device_standby_time": 720,
    "screen_off_time": 30,
    "lcd_backlight_brightness": 2,
    "ac_energy_saving_switch": 0,
    "car_charger_energy_saving_switch": 0,
    "memory_switch": 1,
    "charging_limit_value": 100,
    "discharge_limit_value": 1,
    "tou_reserve_power": 6,
    "outage_alert_switch": 0,
    "manual_backup_switch": 0,
    "auto_backup_switch": 0,
    "super_fast_charge_status": 0,
}


def _restore_defaults(
    request: ParameterDict,  # noqa: ARG001  # the handler reads no request field
) -> dict[str, Value] | None:
    """Return the settings ``4051`` restores (``settings_restore_defaults?``)."""
    return dict(FACTORY_SETTINGS)


def _get_log_size(
    request: ParameterDict,  # noqa: ARG001  # the handler reads no request field
) -> dict[str, Value] | None:
    """Accept: ``4065`` reads the log's size and sets nothing."""
    return {}


#: ``4065``'s chunk size, a firmware constant (a1783-control.md "Command map").
LOG_CHUNK_SIZE = 250


def _log_size(
    request: ParameterDict,  # noqa: ARG001  # the handler reads no request field
    state: Mapping[str, Value],  # noqa: ARG001  # the emulator keeps no log
) -> ParameterDict:
    """``4865``: total bytes, chunk size and chunk count, each type ``03``.

    The 250-byte header chunk always exists; the emulator keeps no log behind
    it, so the total is one chunk.
    """
    return ParameterDict(
        {
            "a3": container("a3", TYPE_VAR, ui(4).build(LOG_CHUNK_SIZE)),
            "a4": container("a4", TYPE_VAR, ui(4).build(LOG_CHUNK_SIZE)),
            "a5": container("a5", TYPE_VAR, ui(4).build(1)),
        }
    )


#: ``4066 a2``: the chunk index.
LOG_CHUNK_REQUEST = {"a2": ("index", ui(2))}
#: The first MainMcu build whose chunk 0 takes the v1.2.1.6 layout.
LOG_LAYOUT_VERSION = 1216
#: ``version_get`` values no frame reports (a1783-control.md "BLE log upload"):
#: index 1, the bootloader's constant; 2, no case; 7 and 8, the SW2505PD USB-PD
#: controllers; 10, the LCD pack; 11, the LCD boot version, runtime only.
LOG_BOOT_VERSION = 1000
LOG_UNSET_VERSION = 0
LOG_USB_PD_VERSION = 310
LOG_LCD_VERSION = 196
LOG_LCD_BOOT_VERSION = 0
#: The v1.2.1.6 layout's slots after the fw one: indices 0-11.
LOG_INDEX_COUNT = 12
#: The v1.2.1.1 layout's slots after the fw one, as ``version_get`` indices.
LOG_V1211_INDICES = (0, 10, 3, 4, 5, 6, 9, 7)
#: The SN bytes chunk 0 carries: 17 from v1.2.1.6, 16 on v1.2.1.1.
LOG_SN_SIZE = 17
LOG_SN_SIZE_V1211 = 16
#: One character: ``'0'`` plus the value, so 12 is ``<``.
LogDigit: Construct[int, int] = ExprAdapter(
    Int8ul,
    decoder=lambda char, _: char - ord("0"),
    encoder=lambda value, _: value + ord("0"),
)
#: v1.2.1.6's version ``v``: ``,`` then ``d.d.d.d`` (``version_fmt_4field``).
LogVersion: Construct[int, int] = ExprAdapter(
    Struct(
        Const(b","),
        "thousands" / LogDigit,
        Const(b"."),
        "hundreds" / LogDigit,
        Const(b"."),
        "tens" / LogDigit,
        Const(b"."),
        "units" / LogDigit,
    ),
    decoder=lambda text, _: (
        text.thousands * 1000 + text.hundreds * 100 + text.tens * 10 + text.units
    ),
    encoder=lambda v, _: {
        "thousands": v // 1000,
        "hundreds": v % 1000 // 100,
        "tens": v % 100 // 10,
        "units": v % 10,
    },
)
#: v1.2.1.1's version ``v``: ``,`` then ``'0'+v/100 . v%100/10 . v%10``.
LogVersionV1211: Construct[int, int] = ExprAdapter(
    Struct(
        Const(b","),
        "hundreds" / LogDigit,
        Const(b"."),
        "tens" / LogDigit,
        Const(b"."),
        "units" / LogDigit,
    ),
    decoder=lambda text, _: text.hundreds * 100 + text.tens * 10 + text.units,
    encoder=lambda v, _: {"hundreds": v // 100, "tens": v % 100 // 10, "units": v % 10},
)
#: The RTC counter as ``%08X``.
RtcHex: Construct[int, int] = ExprAdapter(
    Bytes(8),
    decoder=lambda text, _: int(text, 16),
    encoder=lambda rtc, _: f"{rtc:08X}".encode(),
)


def _log_header_layout(head: Construct[Any, Any]) -> Construct[Any, Any]:
    """Return a chunk 0 of ``head``: space-padded to 249 bytes, then a newline."""
    return Struct("head" / Padded(LOG_CHUNK_SIZE - 1, head, pattern=b" "), Const(b"\n"))


#: Chunk 0 from v1.2.1.6: ``[``, the RTC and a NUL where ``snprintf`` drops the
#: ``]``; the 17-byte SN; 13 versions (fw, then indices 0-11).
LogHeader = _log_header_layout(
    Struct(
        Const(b"["),
        "rtc" / RtcHex,
        Const(b"\x00"),
        "sn" / fitted(LOG_SN_SIZE),
        "versions" / Array(1 + LOG_INDEX_COUNT, LogVersion),
    )
)
#: Chunk 0 on v1.2.1.1: ``[RTC]``, 16 bytes of SN, ``,``, 9 versions.
LogHeaderV1211 = _log_header_layout(
    Struct(
        Const(b"["),
        "rtc" / RtcHex,
        Const(b"]"),
        "sn" / fitted(LOG_SN_SIZE_V1211),
        Const(b","),
        "versions" / Array(1 + len(LOG_V1211_INDICES), LogVersionV1211),
    )
)


def _get_log_chunk(
    request: ParameterDict,  # noqa: ARG001  # the index is read by the reply
) -> dict[str, Value] | None:
    """Accept: ``4066`` reads one chunk of the log and sets nothing."""
    return {}


def _bytes_value(state: Mapping[str, Value], name: str) -> bytes:
    """Return the bytes value ``name`` in ``state``.

    Raises:
        TypeError: If the value isn't bytes.

    """
    value = state[name]
    if not isinstance(value, bytes):
        msg = f"{name} is a bytes value"
        raise TypeError(msg)
    return value


#: ``f9[12:16]`` INVVersion reads only while the ACDC stage is energised (mains
#: in, or AC output on): it shows ~1 s after mains arrives and clears ~1 s after
#: mains goes, or 47-49 s after AC output switches off; 49 s sits in that
#: recorded bracket (a1783-telemetry.md "Versions (``f9``)").
INV_SHOWN_DELAY = 1.0
INV_MAINS_GONE_DELAY = 1.0
INV_OUTPUT_OFF_DELAY = 49.0
INV_EVENT = "inv_version"
#: ``f9[4:8]`` subMCUVersion reads 0 for ~1 s about once a minute with both AC
#: paths off. The timer free-runs from the MCU's start; its phase is ours.
SUB_MCU_PERIOD = 60.0
SUB_MCU_DIP = 1.0
SUB_MCU_EVENT = "sub_mcu_version"
#: Where a dropped-out version waits, carried in no frame.
HIDDEN_VERSIONS = {
    "inv_version": "_inv_version",
    "sub_mcu_version": "_sub_mcu_version",
}
NO_VERSION = bytes(VERSION_SIZE)


def _energised(state: Mapping[str, Value]) -> bool:
    """Whether the ACDC stage is energised: mains in, or AC output on."""
    return state["ac_input_status"] != AC_INPUT_NONE or bool(state["ac_output_status"])


def _inv_version_events(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return INVVersion's return or dropout when the ACDC stage turns on or off."""
    was, now = _energised(before), _energised(after)
    if now and not was:
        shown = partial(_version_shown, "inv_version")
        return (Timed(INV_SHOWN_DELAY, INV_EVENT, shown),)
    if was and not now:
        mains_went = before["ac_input_status"] != AC_INPUT_NONE
        delay = INV_MAINS_GONE_DELAY if mains_went else INV_OUTPUT_OFF_DELAY
        return (Timed(delay, INV_EVENT, partial(_version_hidden, "inv_version")),)
    return ()


def _hidden(name: str, state: Mapping[str, Value]) -> dict[str, Value]:
    """Return the values that set version ``name`` to 0, keeping it aside."""
    return {HIDDEN_VERSIONS[name]: state[name], name: NO_VERSION}


def _version_hidden(
    name: str,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Version ``name`` reads 0 until its stage comes back."""
    return Step(values=_hidden(name, state))


def _version_shown(
    name: str,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Version ``name`` reads again; nothing if it never dropped out."""
    kept = state.get(HIDDEN_VERSIONS[name])
    if kept is None:
        return Step()
    return Step(values={name: kept}, unset=(HIDDEN_VERSIONS[name],))


def _sub_mcu_tick(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Once a minute: with both AC paths off, subMCUVersion reads 0 for ~1 s."""
    tick = Timed(SUB_MCU_PERIOD, SUB_MCU_EVENT, _sub_mcu_tick)
    if _energised(state):
        return Step(then=(tick,))
    back = partial(_version_shown, "sub_mcu_version")
    return Step(
        values=_hidden("sub_mcu_version", state),
        then=(tick, Timed(SUB_MCU_DIP, "sub_mcu_back", back)),
    )


def _version_number(state: Mapping[str, Value], name: str) -> int:
    """Return ``f9`` version ``name`` as its digits' integer (v1.2.1.6 -> 1216)."""
    return int(VersionNumber.parse(_bytes_value(state, name)))


def _log_header(state: Mapping[str, Value]) -> bytes:
    """Return chunk 0 in the layout of the MainMcu build ``f9`` reports.

    Assumed: v1.2.1.6 is the first new-layout build (only the v1.2.1.1 and
    v1.2.1.6 images are decoded, so the change lies in v1.2.1.2-.6), and the
    LCD / USB-PD versions are v1.2.1.6's on v1.2.1.1 too.
    """
    mcu = _version_number(state, "mcu_version")
    by_index = (
        mcu,
        LOG_BOOT_VERSION,
        LOG_UNSET_VERSION,
        _version_number(state, "inv_version"),
        _version_number(state, "sub_mcu_version"),
        _version_number(state, "bms_version"),
        _version_number(state, "sub_package_version"),
        LOG_USB_PD_VERSION,
        LOG_USB_PD_VERSION,
        _version_number(state, "module_version"),
        LOG_LCD_VERSION,
        LOG_LCD_BOOT_VERSION,
    )
    head = {"rtc": int(state[RTC]), "sn": _bytes_value(state, "device_sn")}
    if mcu >= LOG_LAYOUT_VERSION:
        head["versions"] = (mcu, *by_index)
        return bytes(LogHeader.build({"head": head}))
    head["versions"] = (mcu, *(by_index[index] for index in LOG_V1211_INDICES))
    return bytes(LogHeaderV1211.build({"head": head}))


def _log_chunk(request: ParameterDict, state: Mapping[str, Value]) -> ParameterDict:
    """``4866``: the chunk's index, its length and its bytes, type ``02``/``02``/``04``.

    Only chunk 0 exists; a later index replies with no data.
    """
    index = decode_request(request, LOG_CHUNK_REQUEST).get("index", 0)
    data = _log_header(state) if index == 0 else b""
    return ParameterDict(
        {
            "a2": container("a2", TYPE_SILE, ui(2).build(index)),
            "a3": container("a3", TYPE_SILE, ui(2).build(len(data))),
            "a4": container("a4", TYPE_BIN, data),
        }
    )


#: ``4100 a2``'s first six value bytes; bit 0 of the sixth marks an app session.
SessionDescriptor = Struct(Padding(5), "session" / BitStruct(Padding(7), "app" / Flag))
#: ``a3[10:12]`` in an app session, and in any other.
APP_SILENT_RECHARGE_POWER = 1000
SILENT_RECHARGE_POWER = 600


def _get_all_device_info(request: ParameterDict) -> dict[str, Value] | None:
    """Return the silent recharge power the ``4100`` request selects.

    ``fid0f_op100_handler?``: a missing or shorter ``a2`` selects the non-app
    power.
    """
    descriptor = decode_field(request, "a2", GreedyBytes) if "a2" in request else b""
    session = Optional(SessionDescriptor).parse(descriptor)
    app = session is not None and session.session.app
    return {
        "silent_recharge_power": APP_SILENT_RECHARGE_POWER
        if app
        else SILENT_RECHARGE_POWER
    }


def _realtime_trigger(request: ParameterDict) -> dict[str, Value] | None:
    """Return the device-point switch off for a ``4057`` stop (``a2`` 0).

    A start, or no ``a2``, sets nothing: re-sends don't bring the ``0490`` back
    (a1783-telemetry.md "What stops and restores c490"). ``a3`` is the
    module's countdown.
    """
    if "a2" in request and decode_field(request, "a2", ui(1)) == REALTIME_STOP:
        return {DEVICE_POINT_SWITCH: 0}
    return {}


#: Every command the line answers with real validation, by msgtype.
LINE_COMMANDS: dict[int, Callable[[ParameterDict], dict[str, Value] | None]] = {
    0x051: _restore_defaults,
    0x057: _realtime_trigger,
    0x065: _get_log_size,
    0x066: _get_log_chunk,
    0x05E: _set_backup_params,
    0x090: _set_tou_system_params,
    0x091: _set_screen_saver_params,
    0x100: _get_all_device_info,
    0x101: _set_ac_params,
    0x102: _set_car_charger_params,
    0x103: _set_system_params,
}


def _shape_grid_loss(params: ParameterDict, values: Mapping[str, Value]) -> None:
    """Give a grid-loss report its 1-byte ``a3`` (the SoC) and drop ``a4``."""
    if decode_field(params, "a2", ui(1)) != EVENT_GRID_LOSS:
        return
    params["a3"] = ParameterContainer(
        key=b"\xa3", type=0x04, value=ui(1).build(values.get(EVENT_SOC, 0))
    )
    params.pop("a4", None)


def _fields(
    fields_421: Mapping[str, FieldSpec],
) -> Mapping[int, Mapping[str, FieldSpec]]:
    """Return every message's table, with ``fields_421`` for ``0421``/``0900``."""
    return MappingProxyType(
        {MSGTYPE_421: fields_421, MSGTYPE_900: fields_421, **MESSAGE_FIELDS}
    )


class CGen2Layout(TableLayout):
    """The C Gen 2 line's own command/telemetry code; satisfies ``LayoutProtocol``."""

    COMMANDS = MappingProxyType(LINE_COMMANDS)
    SIDE_EFFECTS = MappingProxyType(
        {0x05E: _backup_plan, 0x090: _tou_periods, 0x104: _expansion_switch}
    )
    READ_REPLIES = MappingProxyType({0x065: _log_size, 0x066: _log_chunk})
    FIELDS = _fields(SINGLE_FIELDS)
    GROUPS = MappingProxyType(
        {MSGTYPE_421: (OUTPUT_PORTS,), MSGTYPE_900: (OUTPUT_PORTS,)}
    )
    #: The model pushes ``0425`` event reports (the 0x59 off-grid alert, the
    #: backup windows): only those whose recordings carry a ``0425`` to build from.
    EVENT_REPORTS = False

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names ``set_values()`` can reach.

        ``0425`` adds its SoC byte, a message carrying ``d9`` the stored plan
        and the main pack's c490 SoC its charge hold reads, with the pack
        selection's hidden inputs, one carrying the output ports each USB-C
        port's device, and ``0490`` the device-point switch.
        """
        names = set(super().names(msgtype))
        if msgtype == MSGTYPE_425:
            names.add(EVENT_SOC)
        if "d9" in self.FIELDS.get(msgtype, {}):
            names |= {TOU_TABLE, PACK_SOC, AUTO_PLANS, BUS_PACK, PATH_HOLD}
        if OUTPUT_PORTS in self.GROUPS.get(msgtype, ()):
            names |= USB_C_DEVICES.keys()
        if msgtype == MSGTYPE_490:
            names.add(DEVICE_POINT_SWITCH)
        return frozenset(names)

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages ``name`` reaches; the device-point switch, ``0490``."""
        if name == DEVICE_POINT_SWITCH:
            return (MSGTYPE_490,)
        return super().locate(name)

    def sends(self, msgtype: int, values: Mapping[str, Value]) -> bool:
        """Whether a push goes out now: ``0490`` only with the device-point switch."""
        if msgtype == MSGTYPE_490:
            return bool(values.get(DEVICE_POINT_SWITCH, 1))
        return super().sends(msgtype, values)

    def read(self, msgtype: int, payload: bytes) -> dict[str, Value]:
        """Return the named values ``payload`` carries and its ``d9``'s stored plan.

        A grid-loss ``0425`` reads as its event and its 1-byte SoC ``a3``.
        """
        if msgtype == MSGTYPE_425:
            params = Parameters.parse(payload)
            if decode_field(params, "a2", ui(1)) == EVENT_GRID_LOSS:
                soc = decode_field(params, "a3", ui(1))
                return {"event_code": EVENT_GRID_LOSS, EVENT_SOC: soc}
        values = super().read(msgtype, payload)
        return values | self.implied(values)

    def implied(self, values: Mapping[str, Value]) -> dict[str, Value]:
        """Return what ``values`` imply that they don't set themselves.

        The stored plan of a ``tou_periods``, and no AC input with no mains.
        """
        implied: dict[str, Value] = {}
        if "tou_periods" in values and TOU_TABLE not in values:
            periods = _bytes_value(values, "tou_periods")
            implied[TOU_TABLE] = _tou_table_of(periods)
        if values.get("ac_input_status") == AC_INPUT_NONE and (
            "ac_input_power" not in values
        ):
            implied["ac_input_power"] = 0
        return implied

    def started(self) -> tuple[Timed, ...]:
        """Return the free-running timers: ``0490``, TOU usage, the subMCU dip."""
        return (
            Timed(C490_PERIOD, C490_POST, _c490_post),
            Timed(USAGE_PASS, USAGE_EVENT, _usage_pass),
            Timed(SUB_MCU_PERIOD, SUB_MCU_EVENT, _sub_mcu_tick),
        )

    def timed(
        self, before: Mapping[str, Value], after: Mapping[str, Value]
    ) -> tuple[Timed, ...]:
        """Return what a change of mains, Battery-Only Power or the car socket starts.

        a1783-control.md "``a4[31]`` witnesses" (the 2026-10-23 and 2026-10-26
        mains runs, the 2026-10-06 Battery-Only steps) and "``4102 a2`` and the
        USB-C ports". A name the change set itself stays as set; each step acts
        only if its state still holds.
        """
        own = frozenset(name for name in after if before.get(name) != after.get(name))
        mains_before, mains = before["ac_input_status"], after["ac_input_status"]
        events: list[Timed] = []
        if mains_before != AC_INPUT_NONE and mains == AC_INPUT_NONE:
            events += self._mains_lost(after, own)
        if mains_before == AC_INPUT_NONE and mains != AC_INPUT_NONE:
            events += _mains_back(after, own)
        if DISABLE in own and mains != AC_INPUT_NONE:
            events += _battery_only_switched(before, after, own)
        if CAR_SOCKET in own:
            events += _socket_edge(after, own)
        events += _timers_started(own, after)
        events += _soc_floors_crossed(before, after)
        events += _screen_wakes(own, after)
        if "charge_discharge_status" in own and "work_status" not in own:
            events.append(FLOW_FOLLOW)
        events += _handovers(before, after, own)
        events += _switch_events(before, after, own)
        if PACK_SOC in own:
            events += _top_offs(before, after, own)
            events += _floor_exits(before, after)
        events += _ota_prechecks(before, after, own)
        events += _backup_passes(own, self.EVENT_REPORTS)
        events += _inv_version_events(before, after)
        return tuple(events)

    def _mains_lost(
        self, after: Mapping[str, Value], own: frozenset[str]
    ) -> list[Timed]:
        """Return the auto-off and the off-grid alert mains going draws."""
        events = []
        if after[DISABLE] == BATTERY_ONLY_ON and DISABLE not in own:
            events.append(Timed(AUTO_OFF_DELAY, "auto_off", partial(_auto_off, own)))
        if self.EVENT_REPORTS and after["outage_alert_switch"]:
            alert = Timed(GRID_LOSS_ALERT_DELAY, "grid_loss_alert", _grid_loss_alert)
            events.append(alert)
        return events

    def state_changes(
        self, msgtype: int, plaintext: bytes, state: Mapping[str, Value]
    ) -> dict[str, Value]:
        """Return the values a request sets; any request's ``fd`` is the new ``fd``.

        ``fid0f_app_id_time_store?`` copies up to 15 B of it from every request,
        whatever the command makes of the rest.
        """
        changes = super().state_changes(msgtype, plaintext, state)
        request = Parameters.parse(plaintext)
        if "fd" in request:
            stored = at_most(TRANSACTION_ID_SIZE)
            changes["transaction_id"] = decode_field(request, "fd", stored)
        return changes

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Check each named value against its field, the grid-loss SoC included.

        Raises:
            ConstructError: If a value doesn't fit its field.
            TypeError: If the stored plan isn't its count and record buffer, or
                the device-point switch, the expansion's path hold or a USB-C
                device's plug isn't 0 or 1.

        """
        given = values or {}
        if msgtype == MSGTYPE_425 and EVENT_SOC in given:
            ui(1).build(given[EVENT_SOC])
        if TOU_TABLE in given:
            _check_tou_table(given[TOU_TABLE])
        if BUS_PACK in given and given[BUS_PACK] not in {BUS_MAIN, BUS_EXPANSION}:
            msg = f"{BUS_PACK} is {BUS_MAIN} (main) or {BUS_EXPANSION} (expansion)"
            raise TypeError(msg)
        if AUTO_PLANS in given and (
            len(_bytes_value(given, AUTO_PLANS)) != len(AUTO_PLANS_EMPTY)
        ):
            msg = f"{AUTO_PLANS} is {len(AUTO_PLANS_EMPTY)} bytes: 3 x (start, end)"
            raise TypeError(msg)
        for name in given.keys() & {DEVICE_POINT_SWITCH, PATH_HOLD}:
            _check_switch(name, given[name])
        for name in given.keys() & USB_C_DEVICES.keys():
            if name.endswith(DEVICE_PLUGGED):
                _check_switch(name, given[name])
            else:
                ui(2).build(given[name])
        return super().build(msgtype, values)

    def publishes(self, msgtype: int, payload_of: Callable[[int], bytes]) -> bool:
        """Whether ``msgtype`` publishes now: ``0503`` only in an active TOU period.

        The period is the cloud ``0421``'s ``d9[0]`` (a1783-telemetry.md "0503").
        """
        if msgtype != MSGTYPE_503:
            return super().publishes(msgtype, payload_of)
        params = Parameters.parse(payload_of(MSGTYPE_421))
        period = decode_field(params, "d9", Tou).tou_system_status
        return bool(period != TouPeriodType.NONE)

    def finish(
        self, msgtype: int, params: ParameterDict, values: Mapping[str, Value]
    ) -> None:
        """Give a grid-loss ``0425`` its 1-byte SoC ``a3`` and no ``a4``.

        A ``d9`` takes its ``tou_periods`` from the stored plan and, with the RTC
        known, its active type (0 off the cloud). Grid charging is held off in
        Peak and Mid-Peak, and while Battery-Only Power is on (19:39:18); once
        a holding record ends, AC input ramps back on the RTC (``TOU_RAMP``).
        """
        if msgtype == MSGTYPE_425:
            _shape_grid_loss(params, values)
        fields = self.FIELDS.get(msgtype, {})
        tou = fields.get("d9")
        if tou is not None and TOU_TABLE in values:
            codec, type_byte, _ = tou
            periods = _tou_periods_of(_bytes_value(values, TOU_TABLE))
            self._encode_field(params, "d9", codec, type_byte, tou_periods=periods)
        held, ended = False, None
        if tou is not None and RTC in values:
            codec, type_byte, _ = tou
            period = _active_period(params, values)
            self._encode_field(params, "d9", codec, type_byte, tou_system_status=period)
            held = _charge_held(params, values, period)
            ended = None if held else _hold_ended(params, values)
        if "a7" not in fields:
            return
        if held or (ended is not None and ended < TOU_RAMP[0][0]):
            self._hold_ac_charge(fields, params)
        elif _battery_only_on(params):
            self._hold_ac_charge(fields, params, work=False)
        elif ended is not None:
            self._ramp_ac_input(fields, params, ended)

    def _ramp_ac_input(
        self, fields: Mapping[str, FieldSpec], params: ParameterDict, elapsed: int
    ) -> None:
        """Show AC input ``elapsed`` s into its ramp back after a hold ended."""
        power = decode_field(params, "a7", AcOutput).ac_input_power
        for tag in ("a6", "a7"):
            codec, type_byte, _ = fields[tag]
            ramped = _ramped(power, elapsed)
            self._encode_field(params, tag, codec, type_byte, ac_input_power=ramped)

    def _hold_ac_charge(
        self,
        fields: Mapping[str, FieldSpec],
        params: ParameterDict,
        *,
        work: bool = True,
    ) -> None:
        """Show grid charging held off: AC input 0 W, and no AC-only charging state.

        With no DC or alternator input, a charging ``a5[1]`` / ``c0[24]`` reads
        the port flow left, output on or none, and with ``work`` a charging
        ``a3[0]`` reads idle: the Peak hold, cloud ``0421`` 2026-10-21 15:49:53
        (a1783-telemetry.md "Flow state"). Battery-Only Power leaves ``a3[0]``
        to its stored lag (19:39:18 -> 19:39:22).
        """
        for tag in ("a6", "a7"):
            codec, type_byte, _ = fields[tag]
            self._encode_field(params, tag, codec, type_byte, ac_input_power=0)
        if _other_input_charging(params):
            return
        output = decode_field(params, "a6", MainData).total_output_power
        flow = FLOW_OUT if output else FLOW_IDLE
        held = (
            ("a3", "work_status", FLOW_IDLE),
            ("a5", "charge_discharge_status", flow),
            ("c0", "sub_package_status", flow),
        )
        for tag, name, idle in held if work else held[1:]:
            spec = fields.get(tag)
            if spec is None:
                continue
            codec, type_byte, _ = spec
            if decode_field(params, tag, codec)[name] == FLOW_CHARGE:
                self._encode_field(params, tag, codec, type_byte, **{name: idle})


class CGen2ExpansionLayout(CGen2Layout):
    """The C2000 Gen 2 / C2000X Gen 2 layout: the line's plus the expansion port's.

    The chassis with the port (A1783, A1785) emits ``c0`` and ``ce`` with or
    without a pack attached, and answers ``4104``; the C1000 Gen 2 has neither.
    """

    COMMANDS = MappingProxyType({**LINE_COMMANDS, 0x104: _set_bms_params})
    FIELDS = _fields({**SINGLE_FIELDS, **EXPANSION_FIELDS})
    EVENT_REPORTS = True
