# Copyright (c) 2026 Shawn Stricker
"""Solarbank E1600 (A17C0).

No BLE frame is recorded and PiField has no Solarbank 1 doc. The packaged
frames are anker-solix-api's MQTT recording (``examples/Mqtt_SB1_C1000``,
``CUTUTT17MUSXAJ4R``; identifiers replaced) with the BLE route marker: the
``0405`` telemetry, and the ``0856`` / ``0857`` replies. That the BLE ``c405``
equals the MQTT ``0405`` is assumed (it does on the C1000, not on the A17C1).

Field names are anker-solix-api's ``A17C0`` names that the app's own parser
confirms (moag1000/anker-solix-api-exploration ``BLE_COMMAND_MAP.md``
"A17C0 -- Solarbank Gen 1 0405 Status Tags"); the recorded frame's other
tags stay unnamed. Setters: ``BLE_COMMAND_MAP.md`` "A17C0 -- Solarbank Gen 1"
and "CMD_SB_POWER_CUTOFF".
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
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport
from anker_ble_emulator.validators import OptionsValidator

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock
    from anker_ble_emulator.layouts import Value

#: The telemetry: the status request's answer and the push.
MSGTYPE_405 = 0x405

#: ``0405`` fields, typed as the recorded frame carries them.
TELEMETRY_405 = (
    string("a2", "device_sn", 16),
    unsigned("a3", "battery_soc"),
    unsigned("a5", "error_code"),
    short("a6", "sw_version"),
    short("a7", "sw_controller"),
    short("a8", "hw_version"),
    unsigned("a9", "temp_unit_fahrenheit"),
    signed_byte("aa", "temperature"),
    short("ab", "photovoltaic_power"),
    short("ac", "output_power"),
    unsigned("ad", "charging_status"),
    word("b0", "bat_charge_power"),
    word("b1", "pv_yield"),
    word("b2", "charged_energy"),
    word("b3", "output_energy"),
    unsigned("b4", "output_cutoff_data"),
    unsigned("b5", "lowpower_input_data"),
    unsigned("b6", "input_cutoff_data"),
    string("b7", "inverter_brand", 16),
    string("b8", "inverter_model", 16),
    short("b9", "min_load"),
    string("c0", "0w_switch_sn", 16),
    string("c1", "0w_switch_bt_mac", 12),
    word("fe", "msg_timestamp"),
)

#: ``0050 a2``: the temperature unit, 0 (C) or 1 (F).
TEMP_UNIT = OptionsValidator(ui(1), 0, 1)
#: ``0067``/``0068`` cutoff fields, each one of its options.
CUTOFFS = {
    "a2": ("output_cutoff_data", OptionsValidator(ui(1), 5, 10)),
    "a3": ("lowpower_input_data", OptionsValidator(ui(1), 4, 5)),
    "a4": ("input_cutoff_data", OptionsValidator(ui(1), 5, 10)),
}


def _validate_4050(request: ParameterDict) -> dict[str, Value] | None:
    """Return the temperature unit if ``a2`` is 0 (C) or 1 (F)."""
    return {"temp_unit_fahrenheit": decode_field(request, "a2", TEMP_UNIT)}


def _validate_cutoffs(request: ParameterDict) -> dict[str, Value] | None:
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
            0x050: _validate_4050,
            0x067: _validate_cutoffs,
            0x068: _validate_cutoffs,
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
    known_commands=frozenset({0x040, 0x050, 0x056, 0x057, 0x05E, 0x067, 0x068}),
    layout=A17C0Layout(),
)
register(Product.A17C0, PROFILE)


class A17C0(EmulatedDevice):
    """Solarbank E1600: plain outer (assumed), ECDH, owner confirmation by button."""

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
        """Build a Solarbank E1600; see ``EmulatedDevice``."""
        super().__init__(
            Product.A17C0,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
            mtu=mtu,
        )
