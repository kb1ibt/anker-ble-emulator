# Copyright (c) 2026 Shawn Stricker
"""SOLIX S2000 (AS220).

This is based on the recorded telemetry from SolixBLE PR #65: the
S2000's ``c421`` is the A1783's ``0421`` layout, with a longer ``a6`` and
``a7``, one aggregate USB block (``aa``) and its own ``dd``-``df``, ``f0``
and ``f8``.
"""

from __future__ import annotations

from types import MappingProxyType

from construct import Bytes, Struct

from anker_ble_emulator.layouts import TYPE_VAR, ui
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    FieldGroup,
    FieldSpec,
    TableLayout,
    leaf_zeros,
    one_field,
    struct_zeros,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product

from .base import Advert, ProductDevice, Profile, register
from .solix_c_gen2 import (
    CAPABILITY_ZEROS,
    MSGTYPE_421,
    MSGTYPE_900,
    OUTPUT_FIELDS,
    SETTINGS_FIELDS,
    TOU_ZEROS,
    VERSION_ZEROS,
    Battery,
    Capabilities,
    DcInput,
    DeviceVersions,
    MemorySwitch,
    OutputPort,
    Settings,
    Tou,
)


#: ``a6`` (13 B): the A1783's 9 B, then ``[9]`` (unnamed), the charger plug
#: state and the total input (AC + DC). Names are the A1783 app's; where two
#: tags carry one quantity (``a5[2]``/``[8]``, ``a7[4:6]``/``[2:4]``,
#: ``a8[1:3]``/``[4:6]``) they share its name, so one value sets both.
Power = Struct(
    "total_output_power" / ui(2),
    "ac_input_power" / ui(2),
    "pv1_input_power" / ui(2),
    "remaining_time" / ui(2),
    "battery_level" / ui(1),
    "_reserved" / Bytes(1),
    "ac_input_plug_status" / ui(1),
    "total_input_power" / ui(2),
)
#: ``a7`` (11 B): the A1783's 6 B; the trailing 5 bytes aren't named.
AcOutput = Struct(
    "ac_output_status" / ui(1),
    "ac_output_power" / ui(2),
    "ac_input_status" / ui(1),
    "ac_input_power" / ui(2),
    "_reserved" / Bytes(5),
)
#: ``f0`` (4 B): ``[0]`` is 1 while the AC charger is plugged in (PR #65's
#: unplug/replug test), the app's ``acInputStatus``; the rest isn't named.
Charger = Struct("ac_input_status" / ui(1), "_reserved" / Bytes(3))


#: The typed tags. ``a4``/``a5``/``a8`` and ``d9``/``dc``/``f9``/``fa`` are
#: the A1783's.
TELEMETRY_FIELDS: dict[str, FieldSpec] = {
    "a4": (Settings, 0x04, leaf_zeros(*SETTINGS_FIELDS)),
    "a5": (
        Battery,
        0x04,
        leaf_zeros("temperature", "charge_discharge_status", "battery_level", "health"),
    ),
    "a6": (Power, 0x04, struct_zeros(Power)),
    "a7": (AcOutput, 0x04, struct_zeros(AcOutput)),
    "a8": (DcInput, 0x04, leaf_zeros("pv1_charge_status", "pv1_input_power")),
    "d9": (Tou, 0x04, TOU_ZEROS),
    "dc": (
        MemorySwitch,
        0x04,
        leaf_zeros("memory_switch_error_code", "memory_switch_error_timestamp"),
    ),
    "f0": (Charger, 0x04, struct_zeros(Charger)),
    "f9": (DeviceVersions, 0x04, VERSION_ZEROS),
    "fa": (Capabilities, 0x04, CAPABILITY_ZEROS),
    "fe": one_field("timestamp", ui(4), TYPE_VAR),
}
#: ``aa``: one aggregate USB total, at the tag the A1783 app calls ``pd1``.
OUTPUT_PORTS = FieldGroup(OutputPort, leaf_zeros(*OUTPUT_FIELDS), {"pd1": "aa"})


class AS220Layout(TableLayout):
    """The S2000's own telemetry code; satisfies ``LayoutProtocol``."""

    FIELDS = MappingProxyType(
        {MSGTYPE_421: TELEMETRY_FIELDS, MSGTYPE_900: TELEMETRY_FIELDS}
    )
    GROUPS = MappingProxyType(
        {MSGTYPE_421: (OUTPUT_PORTS,), MSGTYPE_900: (OUTPUT_PORTS,)}
    )


PROFILE = Profile(
    serial="APCDPTC0000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.ESP32_0_0_0_3,
    auth_mode=AuthMode.CONFIRM,
    # No advertisement record is recorded for the AS220.
    advert=Advert(local_name=None),
    data=("as220.json",),
    replies={
        0x057: (0x857,),
        0x05E: (0x85E,),
        0x090: (0x890,),
        0x093: (0x893,),
        0x100: (MSGTYPE_900, MSGTYPE_421),
        0x101: (0x901,),
        0x103: (0x903,),
    },
    pushes=(MSGTYPE_421,),
    # No 0830 is recorded, so 0030 goes unanswered.
    device_version=None,
    known_commands=dict.fromkeys(
        {0x040, 0x057, 0x05E, 0x090, 0x093, 0x100, 0x101, 0x103}
    ),
    layout=AS220Layout(),
)
register(Product.AS220, PROFILE)


class AS220(ProductDevice):
    """SOLIX S2000: encrypted outer, ECDH, owner confirmation by button."""

    PRODUCT = Product.AS220
