# Copyright (c) 2026 Shawn Stricker
"""SOLIX S2000 (AS220).

This is based on the recorded telemetry from SolixBLE PR #65: the
S2000's ``c421`` is the A1783's ``0421`` layout, with a longer ``a6`` and
``a7``, one aggregate USB block (``aa``) and its own ``dd``-``df``, ``f0``
and ``f8``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from construct import Bytes, Struct

from anker_ble_emulator.layouts import TYPE_VAR, Value, ui
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
    AC_OUT,
    AC_OVER_CEILING,
    CAPABILITY_ZEROS,
    CHARGE_OVER_LIMIT,
    MSGTYPE_421,
    MSGTYPE_900,
    OUTPUT_FIELDS,
    SETTINGS_FIELDS,
    TOU_ZEROS,
    ULTRAFAST,
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


if TYPE_CHECKING:
    from collections.abc import Mapping


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

#: Anker's spec-sheet ratings in W (PiField s2000-as220.md "Ratings"). AC in
#: is the charge plus the AC out it passes through, as on the A1783. The
#: 1150 W charging figure is the nameplate: the recorded unit is set to charge
#: at 1200 W, so the charge holds to its own limit plus the A1783's +10 W, and
#: AC in to the bypass rating plus the C Gen 2's +33 W overshoot band.
ULTRAFAST_CHARGE_RATING = 1600
AC_BYPASS_RATING = 1800
SOLAR_RATING = 400
AC_OUTPUT_PEAK = 3000
#: ``aa`` totals USB-C 100 W, USB-C 15 W and USB-A 12 W; the last two share 18 W.
USB_RATING = 100 + 18
AC_IN, PV_IN, USB_OUT = "ac_input_power", "pv1_input_power", "pd1_output_power"
AC_LIMIT = "ac_recharge_power"
INPUT_TOTAL, OUTPUT_TOTAL = "total_input_power", "total_output_power"


class AS220Layout(TableLayout):
    """The S2000's own telemetry code; satisfies ``LayoutProtocol``."""

    FIELDS = MappingProxyType(
        {MSGTYPE_421: TELEMETRY_FIELDS, MSGTYPE_900: TELEMETRY_FIELDS}
    )
    GROUPS = MappingProxyType(
        {MSGTYPE_421: (OUTPUT_PORTS,), MSGTYPE_900: (OUTPUT_PORTS,)}
    )

    def resolve(
        self, values: Mapping[str, Value], state: Mapping[str, Value]
    ) -> dict[str, Value]:
        """Return what ``values`` set on ``state``: the values, within the ratings.

        A rating is checked when ``values`` names a power it reads.

        Raises:
            TypeError: If a power would pass its rating; nothing is set then.

        """
        after = {**state, **values}
        ac_in, ac_out = int(after[AC_IN]), int(after[AC_OUT])
        charge = (
            ULTRAFAST_CHARGE_RATING
            if after[ULTRAFAST]
            else int(after[AC_LIMIT]) + CHARGE_OVER_LIMIT
        )
        charge_inputs = {AC_IN, AC_OUT, ULTRAFAST, AC_LIMIT}
        limits = (
            ({AC_IN}, "AC in", ac_in, AC_BYPASS_RATING + AC_OVER_CEILING),
            (charge_inputs, "the AC charge", ac_in - ac_out, charge),
            ({PV_IN}, "solar", int(after[PV_IN]), SOLAR_RATING),
            ({AC_OUT}, "AC out", ac_out, AC_OUTPUT_PEAK),
            ({USB_OUT}, "USB", int(after[USB_OUT]), USB_RATING),
            (
                {INPUT_TOTAL},
                "the input total",
                int(after[INPUT_TOTAL]),
                AC_BYPASS_RATING + AC_OVER_CEILING + SOLAR_RATING,
            ),
            (
                {OUTPUT_TOTAL},
                "the output total",
                int(after[OUTPUT_TOTAL]),
                AC_OUTPUT_PEAK + USB_RATING,
            ),
        )
        for names, what, power, rating in limits:
            if names & values.keys() and power > rating:
                msg = f"{what} reads at most {rating} W on the S2000, not {power} W"
                raise TypeError(msg)
        return dict(values)


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
