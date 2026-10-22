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
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from construct import (
    BitsInteger,
    BitStruct,
    Bytes,
    Const,
    ConstructError,
    Container,
    ExprAdapter,
    Flag,
    GreedyBytes,
    GreedyRange,
    Int8ul,
    Optional,
    Padding,
    Prefixed,
    Struct,
    Terminated,
)

from anker_ble_emulator.layouts import (
    STATUS_ACCEPTED,
    TYPE_BIN,
    TYPE_SILE,
    TYPE_VAR,
    Value,
    ui,
)
from anker_ble_emulator.mcu import RTC
from anker_ble_emulator.messages import ROUTE_APP, ROUTE_LAYOUT, ROUTE_PARAM_INFO
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
    struct_zeros,
)


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
#: (4101-4103) writes into; 26 B on MainMcu v1.2.1.1 (the A1763 recording),
#: 33 B on v1.2.1.6, which appends reserved zeros and the ``[31]`` flags
#: byte. PiField/docs/re/a1783-control.md "Telemetry from local state
#: (display board)" for the offsets; names are the app's
#: (reference/app-names.md).
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

#: ``a3`` StatusData: 10 B on v1.2.1.1, 13 B on v1.2.1.6, which appends the
#: silent recharge power and the weak-light lock. a1783-telemetry.md ``a3``
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
#: then the record buffer ``a7`` is copied over. The app has no name for it;
#: ``d9``'s ``tou_periods`` is read from it.
TOU_TABLE = "tou_period_table"
TouTable = Struct(
    "count" / ui(1),
    "records" / Bytes(TOU_RECORDS_SIZE),
    Terminated,
)


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
    Const(b"\x10"),
    "external_device_product_code" / Bytes(PN_SIZE),
    Const(b"\x01"),
    Const(b"\x11"),
    "external_device_sn" / Bytes(SN_SIZE),
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
    if "b0" in request and request["b0"]["length"] > 1:
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
    records = schedule.records[:TOU_RECORDS_SIZE].ljust(TOU_RECORDS_SIZE, b"\0")
    return TouTable.build({"count": schedule.count, "records": records})


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
    size = stored.count * TOU_RECORD_SIZE
    records = stored.records[:size].ljust(size, b"\0")
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
    buffer = bytearray(stored.records)
    if "a7" in request:
        periods = decode_field(request, "a7", GreedyBytes)
        buffer[: len(periods)] = periods
    table = TouTable.build({"count": count, "records": bytes(buffer)})
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
#: The MainMcu build (v1.2.1.6) from which Battery-Only Power blocks TOU.
AC_INPUT_DISABLE_GATE_VERSION = 1216
#: One plan record: ``{type, start hour, end hour}``, whole hours.
TouRecord = Struct("type" / ui(1), "start_hour" / ui(1), "end_hour" / ui(1))
#: The app's name for the device being on WiFi to the cloud; set, it stands
#: for TOU condition 2 (bound and on WiFi) and routes ``param_info``.
WIFI_ONLINE = "wifi_online"
#: TOU condition 3, the power stage's AC input report (``0x2000054a`` bit 0),
#: which no ``0421`` field carries; unset, it reads reported. Descriptive name.
POWER_STAGE_AC_INPUT = "power_stage_ac_input"
#: The data-read replies and pushes whose ``a1`` source follows WiFi:
#: a1783-telemetry.md "Data-read replies and telemetry pushes ride param_info".
PARAM_INFO_MESSAGES = frozenset(
    {MSGTYPE_421, MSGTYPE_900, MSGTYPE_489, MSGTYPE_889, MSGTYPE_892}
)


def _check_switch(name: str, value: Value) -> None:
    """Check a hidden on/off input is 0 or 1.

    Raises:
        TypeError: If it isn't.

    """
    if not isinstance(value, int) or value not in {0, 1}:
        msg = f"{name} is 0 or 1"
        raise TypeError(msg)


def _alternator_attached(params: ParameterDict) -> bool:
    """Whether a car-alternator charger is attached (TOU condition 4).

    Assumed: attached when ``ce`` names a product code; the firmware's present
    byte (block 0xe +0x30) isn't in ``ce``.
    """
    if "ce" not in params:
        return False
    alternator = decode_field(params, "ce", Alternator)
    return bool(alternator.external_device_product_code != bytes(PN_SIZE))


def _ac_input_disabled(params: ParameterDict) -> bool:
    """Whether Battery-Only Power blocks TOU (condition 5, MainMcu v1.2.1.6 on).

    Assumed: both ``a4[31]`` input states, 1 (``4101 ab``) and 2 (auto-off).
    """
    versions = decode_field(params, "f9", DeviceVersions)
    version = _version_number({"mcu_version": versions.mcu_version}, "mcu_version")
    if version < AC_INPUT_DISABLE_GATE_VERSION:
        return False
    return bool(decode_field(params, "a4", Settings).ac_input_disable_switch)


def _active_period(params: ParameterDict, values: Mapping[str, Value]) -> int:
    """Return ``d9[0]``: the active record's type, else 0 (``tou_active_period_get?``).

    a1783-control.md "TOU (4090)" conditions 1-6, in order; the hour is the
    RTC's. Assumed: the first record in order wins where two match.
    """
    tou = decode_field(params, "d9", Tou)
    on = tou.backup_mode == TOU_MODE_ON
    online = values.get(WIFI_ONLINE, 0) and values.get(POWER_STAGE_AC_INPUT, 1)
    if not on or not online or _alternator_attached(params):
        return TouPeriodType.NONE
    if _ac_input_disabled(params):
        return TouPeriodType.NONE
    hour = int(values[RTC]) // 3600 % 24
    records = TouRecords.parse(tou.tou_periods).records
    for record in GreedyRange(TouRecord).parse(records):
        if record.start_hour <= hour < record.end_hour:
            return int(record.type)
    return TouPeriodType.NONE


def _charge_held(params: ParameterDict, period: int) -> bool:
    """Whether ``period`` holds grid charging off at the battery's SoC.

    Peak and Mid-Peak hold while SoC >= max(reserve, min + 5). Assumed: the SoC
    is ``a5``'s whole-percent ``battery_level``, with no +0.3 % hysteresis.
    """
    if period not in HELD_PERIODS:
        return False
    tou = decode_field(params, "d9", Tou)
    threshold = max(tou.tou_reserve_power, tou.discharge_limit_value + TOU_MIN_MARGIN)
    return bool(decode_field(params, "a5", Battery).battery_level >= threshold)


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
    """Return the expansion's switch state; ``a5`` (locate blink) changes no field.

    The real pack re-closes an "off" within ~2 s (``contactor_fsm?``); the
    emulator reports the commanded state.
    """
    return decode_request(request, EXPANSION_GROUP)


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
#: Chunk 0's auxiliary (LCD board) and USB-PD controller versions, which no
#: frame reports.
LOG_AUX_VERSION = 0
LOG_USB_PD_VERSION = 0
#: Chunk 0 carries the SN's first 16 bytes.
LOG_SN_SIZE = 16


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


def _version_number(state: Mapping[str, Value], name: str) -> int:
    """Return ``f9`` version ``name`` as its digits' integer (v1.2.1.6 -> 1216)."""
    return int("".join(str(digit) for digit in reversed(_bytes_value(state, name))))


def _log_header(state: Mapping[str, Value]) -> bytes:
    """Return chunk 0: ``[<RTC as 8 hex>]``, the SN's first 16 bytes, nine versions.

    Each version integer ``v`` prints as ``,(v/100).(v%100/10).(v%10)``; the
    rest is spaces, with a newline last. a1783-control.md "BLE log upload".
    """
    mcu = _version_number(state, "mcu_version")
    slots = (
        mcu,
        mcu,
        LOG_AUX_VERSION,
        _version_number(state, "inv_version"),
        _version_number(state, "sub_mcu_version"),
        _version_number(state, "bms_version"),
        _version_number(state, "sub_package_version"),
        _version_number(state, "module_version"),
        LOG_USB_PD_VERSION,
    )
    versions = "".join(f",{v // 100}.{v % 100 // 10}.{v % 10}" for v in slots)
    sn = _bytes_value(state, "device_sn")[:LOG_SN_SIZE]
    head = f"[{int(state[RTC]):08X}]".encode() + sn + versions.encode()
    return head.ljust(LOG_CHUNK_SIZE - 1, b" ") + b"\n"


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
SESSION_DESCRIPTOR_SIZE = 6
#: ``a3[10:12]`` in an app session, and in any other.
APP_SILENT_RECHARGE_POWER = 1000
SILENT_RECHARGE_POWER = 600


def _get_all_device_info(request: ParameterDict) -> dict[str, Value] | None:
    """Return the silent recharge power the ``4100`` request selects.

    ``fid0f_op100_handler?``: a missing or shorter ``a2`` selects the non-app
    power.
    """
    descriptor = decode_field(request, "a2", GreedyBytes) if "a2" in request else b""
    padded = descriptor[:SESSION_DESCRIPTOR_SIZE].ljust(SESSION_DESCRIPTOR_SIZE, b"\0")
    app = SessionDescriptor.parse(padded).session.app
    return {
        "silent_recharge_power": APP_SILENT_RECHARGE_POWER
        if app
        else SILENT_RECHARGE_POWER
    }


#: Every command the line answers with real validation, by msgtype.
LINE_COMMANDS: dict[int, Callable[[ParameterDict], dict[str, Value] | None]] = {
    0x051: _restore_defaults,
    0x065: _get_log_size,
    0x066: _get_log_chunk,
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
    SIDE_EFFECTS = MappingProxyType({0x090: _tou_periods})
    READ_REPLIES = MappingProxyType({0x065: _log_size, 0x066: _log_chunk})
    FIELDS = _fields(SINGLE_FIELDS)
    GROUPS = MappingProxyType(
        {MSGTYPE_421: (OUTPUT_PORTS,), MSGTYPE_900: (OUTPUT_PORTS,)}
    )

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names ``set_values()`` can reach.

        ``0425`` adds its SoC byte, a message carrying ``d9`` the stored plan and
        the power stage's AC input, and a ``param_info`` message the WiFi state.
        """
        names = set(super().names(msgtype))
        if msgtype == MSGTYPE_425:
            names.add(EVENT_SOC)
        if "d9" in self.FIELDS.get(msgtype, {}):
            names |= {TOU_TABLE, POWER_STAGE_AC_INPUT}
        if msgtype in PARAM_INFO_MESSAGES:
            names.add(WIFI_ONLINE)
        return frozenset(names)

    def read(self, msgtype: int, payload: bytes) -> dict[str, Value]:
        """Return the named values ``payload`` carries and its ``d9``'s stored plan."""
        values = super().read(msgtype, payload)
        return values | self.implied(values)

    def implied(self, values: Mapping[str, Value]) -> dict[str, Value]:
        """Return the stored plan a ``tou_periods`` set without one implies."""
        if "tou_periods" not in values or TOU_TABLE in values:
            return {}
        return {TOU_TABLE: _tou_table_of(_bytes_value(values, "tou_periods"))}

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
            transaction_id = decode_field(request, "fd", GreedyBytes)
            changes["transaction_id"] = transaction_id[:TRANSACTION_ID_SIZE]
        return changes

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Check each named value against its field, the grid-loss SoC included.

        Raises:
            ConstructError: If a value doesn't fit its field.
            TypeError: If the stored plan isn't its count and record buffer, or
                a hidden on/off input isn't 0 or 1.

        """
        given = values or {}
        if msgtype == MSGTYPE_425 and EVENT_SOC in given:
            ui(1).build(given[EVENT_SOC])
        if TOU_TABLE in given:
            _check_tou_table(given[TOU_TABLE])
        for name in (WIFI_ONLINE, POWER_STAGE_AC_INPUT):
            if name in given:
                _check_switch(name, given[name])
        return super().build(msgtype, values)

    def finish(
        self, msgtype: int, params: ParameterDict, values: Mapping[str, Value]
    ) -> None:
        """Give a grid-loss ``0425`` its 1-byte SoC ``a3`` and no ``a4``.

        A ``d9`` takes its ``tou_periods`` from the stored plan and, with the RTC
        known, its active type, which holds grid charging (``a7`` AC input) at 0
        W in Peak and Mid-Peak; on WiFi a ``param_info`` message routes ``a1``
        ``0x34``, else as recorded (``0x31``).
        """
        if msgtype == MSGTYPE_425:
            _shape_grid_loss(params, values)
        fields = self.FIELDS.get(msgtype, {})
        tou = fields.get("d9")
        if tou is not None and TOU_TABLE in values:
            codec, type_byte, _ = tou
            periods = _tou_periods_of(_bytes_value(values, TOU_TABLE))
            self._encode_field(params, "d9", codec, type_byte, tou_periods=periods)
        if tou is not None and RTC in values:
            codec, type_byte, _ = tou
            period = _active_period(params, values)
            self._encode_field(params, "d9", codec, type_byte, tou_system_status=period)
            if _charge_held(params, period):
                ac, ac_type, _ = fields["a7"]
                self._encode_field(params, "a7", ac, ac_type, ac_input_power=0)
        if msgtype in PARAM_INFO_MESSAGES and values.get(WIFI_ONLINE):
            route = ROUTE_LAYOUT.build(
                Container(destination=ROUTE_APP, source=ROUTE_PARAM_INFO)
            )
            params["a1"] = ParameterContainer(key=b"\xa1", type=None, value=route)


class CGen2ExpansionLayout(CGen2Layout):
    """The C2000 Gen 2 / C2000X Gen 2 layout: the line's plus the expansion port's.

    The chassis with the port (A1783, A1785) emits ``c0`` and ``ce`` with or
    without a pack attached, and answers ``4104``; the C1000 Gen 2 has neither.
    """

    COMMANDS = MappingProxyType({**LINE_COMMANDS, 0x104: _set_bms_params})
    FIELDS = _fields({**SINGLE_FIELDS, **EXPANSION_FIELDS})
