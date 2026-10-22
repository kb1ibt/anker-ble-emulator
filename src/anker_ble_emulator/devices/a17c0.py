# Copyright (c) 2026 Shawn Stricker
"""Solarbank E1600 (A17C0).

No BLE frame is recorded and PiField has no Solarbank 1 doc. The packaged
frames are anker-solix-api's MQTT recording (``examples/Mqtt_SB1_C1000``,
``CUTUTT17MUSXAJ4R``; identifiers replaced) with the BLE route marker: the
``0405`` telemetry, and the ``0856`` / ``0857`` replies. That the BLE ``c405``
equals the MQTT ``0405`` is assumed (it does on the C1000, not on the A17C1).

The app logs no names for this device, so a field takes the app's name for the
same quantity on the A1783 (reference/app-names.md), or a descriptive one where
it has none; the recorded frame's other tags stay unnamed. Tags and setters:
moag1000/anker-solix-api-exploration ``BLE_COMMAND_MAP.md`` "A17C0 --
Solarbank Gen 1 0405 Status Tags", "A17C0 -- Solarbank Gen 1" and
"CMD_SB_POWER_CUTOFF".
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from anker_ble_emulator.layouts import short, signed_byte, string, ui, unsigned, word
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    CommandDispatchLayout,
    ParameterDict,
    ScalarFieldLayout,
    decode_field,
    decode_request,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product
from anker_ble_emulator.validators import OptionsValidator

from .base import Advert, ProductDevice, Profile, register


if TYPE_CHECKING:
    from anker_ble_emulator.layouts import Value

#: The telemetry: the status request's answer and the push.
MSGTYPE_405 = 0x405

#: ``0405`` fields, typed as the recorded frame carries them.
TELEMETRY_405 = (
    string("a2", "device_sn", 16),
    unsigned("a3", "battery_level"),
    unsigned("a5", "error_code"),
    short("a6", "device_version"),
    short("a7", "sub_mcu_version"),
    short("a8", "hw_version"),
    unsigned("a9", "temperature_unit"),
    signed_byte("aa", "temperature"),
    short("ab", "pv1_input_power"),
    short("ac", "total_output_power"),
    unsigned("ad", "charge_discharge_status"),
    word("b0", "battery_charge_power"),
    word("b1", "pv_yield"),
    word("b2", "charged_energy"),
    word("b3", "output_energy"),
    unsigned("b4", "discharge_limit_value"),
    unsigned("b5", "lowpower_input_data"),
    unsigned("b6", "input_cutoff_data"),
    string("b7", "inverter_brand", 16),
    string("b8", "inverter_model", 16),
    short("b9", "min_load"),
    string("c0", "0w_switch_sn", 16),
    string("c1", "0w_switch_bt_mac", 12),
    word("fe", "timestamp"),
)

#: ``0050 a2``: the temperature unit, 0 (C) or 1 (F).
TEMP_UNIT = OptionsValidator(ui(1), 0, 1)
#: ``0067``/``0068`` cutoff fields, each one of its options.
CUTOFFS = {
    "a2": ("discharge_limit_value", OptionsValidator(ui(1), 5, 10)),
    "a3": ("lowpower_input_data", OptionsValidator(ui(1), 4, 5)),
    "a4": ("input_cutoff_data", OptionsValidator(ui(1), 5, 10)),
}


def _set_system_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return the temperature unit if ``a2`` is 0 (C) or 1 (F)."""
    return {"temperature_unit": decode_field(request, "a2", TEMP_UNIT)}


def _set_cutoffs(request: ParameterDict) -> dict[str, Value] | None:
    """Return the cutoffs the request sets, if each is one of its options."""
    return decode_request(request, CUTOFFS)


class A17C0Layout(ScalarFieldLayout, CommandDispatchLayout):
    """The A17C0's own command/telemetry code; satisfies ``LayoutProtocol``.

    ``0068`` (inverter type) also carries the brand, model and loads; only
    its cutoff triple is modelled, since the request's brand/model encoding
    (type ``04``) isn't matched to the telemetry's strings anywhere.
    """

    TELEMETRY = MappingProxyType({MSGTYPE_405: TELEMETRY_405})
    COMMANDS = MappingProxyType(
        {
            0x050: _set_system_params,
            0x067: _set_cutoffs,
            0x068: _set_cutoffs,
        }
    )


PROFILE = Profile(
    serial="A17C000000000001",
    # Assumed: no Solarbank 1 handshake is recorded; the Solarbank 2 (A17C1)
    # negotiates on the plain outer.
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=("a17c0.json",),
    # The status request draws the 0405 telemetry, not an 0840 reply.
    replies={0x040: (MSGTYPE_405,), 0x056: (0x856,), 0x057: (0x857,)},
    pushes=(MSGTYPE_405,),
    device_version=None,
    # The recorded requests, the setters, and the app's setDischargeTime (005e).
    known_commands=dict.fromkeys({0x040, 0x050, 0x056, 0x057, 0x05E, 0x067, 0x068}),
    layout=A17C0Layout(),
)
register(Product.A17C0, PROFILE)


class A17C0(ProductDevice):
    """Solarbank E1600: plain outer (assumed), ECDH, owner confirmation by button."""

    PRODUCT = Product.A17C0
