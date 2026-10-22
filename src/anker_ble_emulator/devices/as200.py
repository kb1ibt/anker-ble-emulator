# Copyright (c) 2026 Shawn Stricker
"""SOLIX Alternator Charger (AS200).

No BLE frame is recorded. The packaged frames are MQTT recordings from
anker-solix-api discussion #290 (identifiers replaced) with the BLE route
marker: a ``0900`` status reply and ``0421`` from firmware v1.1.1.0 while it
charged a C2000 Gen 2 through the expansion cable, the ``0857``/``0903``
acks, and a v1.1.0.5 ``0489``. That the BLE frames equal the MQTT ones is
assumed. Its firmware is the C Gen 2 display board's family: the same
messages (``0100`` status request, ``0057`` realtime trigger, ``0103``
``action_set_system_params``, ``0489`` OTA push), ``f9`` and ``0489`` layouts.

Settings take the app's names for the AS200's own ``action_set_system_params``
parameters (moag1000/anker-solix-api-exploration ``VALIDATION_MODEL.md``,
unverified against a log); readings take the A1783's names for the same
quantity (reference/app-names.md); the attached power station's fields take
the A1783's ``externalDevice`` prefix. Offsets from the thread's traces.
"""

from __future__ import annotations

from enum import IntEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from construct import Bytes, Int8ul, Prefixed, Struct

from anker_ble_emulator.layouts import TYPE_BIN, ui
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    FieldSpec,
    ParameterDict,
    TableLayout,
    decode_request,
    one_field,
    struct_zeros,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from .base import Advert, ProductDevice, Profile, register
from .solix_c_gen2 import (
    MSGTYPE_421,
    MSGTYPE_489,
    MSGTYPE_900,
    OTA_FIELDS,
    VERSION_ZEROS,
    DeviceVersions,
)


if TYPE_CHECKING:
    from anker_ble_emulator.layouts import Value


class WorkMode(IntEnum):
    """``a4[1]``, ``current_work_mode``: which way power moves once switched on."""

    #: Charging Mode: the alternator charges the power station.
    CHARGING = 0
    #: Reverse Mode: the power station charges the car battery.
    REVERSE_CHARGING = 1


class WorkStatus(IntEnum):
    """``a6[0]`` and ``a7[3]``: the A1783's ``workStatus`` codes."""

    IDLE = 0
    #: The power station discharges into the car battery (Reverse Mode).
    REVERSE_CHARGING = 1
    #: The power station charges from the alternator (Charging Mode).
    CHARGING = 2


#: ``a3`` (10 B): ``[4:6]`` the power moved, in either mode.
Output = Struct(
    "_reserved_0" / Bytes(4),
    "total_output_power" / ui(2),
    "_reserved_6" / Bytes(4),
)
#: ``a4`` (22 B on v1.1.1.0; v1.1.0.5 sends the first 20): the settings
#: ``0103`` writes. ``power_out_voltage`` is in 0.1 V. Which of the two
#: timeouts is the app's ``shutdownTimeout`` and which ``shutdownTimeoutV2``
#: is assumed: ``[11:13]`` is the one the device acts on (0 with
#: ``never_shutdown``), ``[16:18]`` the app's slider.
Settings = Struct(
    "power_switch" / ui(1),
    "current_work_mode" / ui(1),
    "battery_type" / ui(1),
    "battery_voltage_type" / ui(1),
    "_reserved_4" / Bytes(1),
    "power_out_voltage" / ui(2),
    "charging_mode_power" / ui(2),
    "reverse_mode_power" / ui(2),
    "shutdown_timeout" / ui(2),
    "temperature_unit" / ui(1),
    "_reserved_14" / Bytes(2),
    "shutdown_timeout_v2" / ui(2),
    "never_shutdown" / ui(1),
    "_reserved_19" / Bytes(3),
)
#: ``a6`` (30 B): the state, the limits the app's sliders take (which follow
#: the battery type and voltage), the car battery's voltage (0.1 V) and the
#: cables. ``[4]`` is 1 while a cable to a power station is plugged, ``[27]``
#: 1 while that cable is the XT-60i (2 for a moment as the expansion cable
#: comes out). ``[25:27]`` repeats ``a3[4:6]``.
Status = Struct(
    "work_status" / ui(1),
    "_reserved_1" / Bytes(1),
    "temperature" / ui(1, signed=True),
    "_reserved_3" / Bytes(1),
    "output_cable_status" / ui(1),
    "_reserved_5" / Bytes(1),
    "charging_mode_power_min" / ui(2),
    "charging_mode_power_max" / ui(2),
    "reverse_mode_power_min" / ui(2),
    "reverse_mode_power_max" / ui(2),
    "power_out_voltage_min" / ui(2),
    "power_out_voltage_max" / ui(2),
    "battery_voltage" / ui(2),
    "_reserved_20" / Bytes(5),
    "total_output_power" / ui(2),
    "xt60i_cable_status" / ui(1),
    "_reserved_28" / Bytes(2),
)
#: ``a7`` (43 B): the power station on the expansion cable, zero on the
#: XT-60i. Times are 0.1 h, ``ffff`` when not charging / discharging.
STATION_SN_SIZE = 17
STATION_PN_SIZE = 12
Station = Struct(
    "_reserved_0" / Bytes(2),
    "external_device_temperature" / ui(1, signed=True),
    "external_device_work_status" / ui(1),
    "external_device_remaining_charge_time" / ui(2),
    "external_device_remaining_discharge_time" / ui(2),
    "external_device_battery_level" / ui(1),
    "_reserved_9" / Bytes(2),
    "external_device_sn" / Prefixed(Int8ul, Bytes(STATION_SN_SIZE)),
    "external_device_product_code" / Prefixed(Int8ul, Bytes(STATION_PN_SIZE)),
    "_reserved_42" / Bytes(1),
)

TELEMETRY_FIELDS: dict[str, FieldSpec] = {
    "a3": (Output, TYPE_BIN, struct_zeros(Output)),
    "a4": (Settings, TYPE_BIN, struct_zeros(Settings)),
    "a6": (Status, TYPE_BIN, struct_zeros(Status)),
    "a7": (
        Station,
        TYPE_BIN,
        {
            **struct_zeros(Station),
            "external_device_sn": bytes(STATION_SN_SIZE),
            "external_device_product_code": bytes(STATION_PN_SIZE),
        },
    ),
    "f9": (DeviceVersions, TYPE_BIN, VERSION_ZEROS),
    "fe": one_field("timestamp", ui(4), TYPE_BIN),
}

#: ``0103`` tag -> (the ``a4`` field it sets, its codec). ``b8`` (power the
#: device off; only its button brings it back) sets nothing.
SYSTEM_PARAMS = {
    "a2": ("current_work_mode", ui(1)),
    "a3": ("battery_type", ui(1)),
    "a5": ("charging_mode_power", ui(2)),
    "a6": ("reverse_mode_power", ui(2)),
    "aa": ("battery_voltage_type", ui(1)),
    "ac": ("power_switch", ui(1)),
    "ae": ("shutdown_timeout", ui(2)),
    "b2": ("temperature_unit", ui(1)),
    "b4": ("power_out_voltage", ui(2)),
    "bb": ("shutdown_timeout_v2", ui(2)),
    "bc": ("never_shutdown", ui(1)),
}


def _set_system_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return the settings ``0103`` changes; the app sends only the group it edits."""
    return decode_request(request, SYSTEM_PARAMS)


class AS200Layout(TableLayout):
    """The Alternator Charger's command/telemetry code; satisfies ``LayoutProtocol``."""

    COMMANDS = MappingProxyType({0x103: _set_system_params})
    FIELDS = MappingProxyType(
        {
            MSGTYPE_421: TELEMETRY_FIELDS,
            MSGTYPE_900: TELEMETRY_FIELDS,
            MSGTYPE_489: OTA_FIELDS,
        }
    )


PROFILE = Profile(
    serial="ALFDMT30000000001",
    # Assumed, as the S2000's (AS220): no handshake is recorded.
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_3_2_7,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=("as200.json",),
    replies={
        0x057: (0x857,),
        0x100: (MSGTYPE_900, MSGTYPE_421),
        0x103: (0x903, MSGTYPE_421),
    },
    pushes=(MSGTYPE_421, MSGTYPE_489),
    device_version=None,
    known_commands=dict.fromkeys({0x057, 0x100, 0x103}),
    # The display board's family acks every setter 00.
    rejects=False,
    layout=AS200Layout(),
)
register(Product.AS200, PROFILE)


class AS200(ProductDevice):
    """SOLIX Alternator Charger: encrypted outer, button (both assumed), ECDH."""

    PRODUCT = Product.AS200
