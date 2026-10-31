# Copyright (c) 2026 Shawn Stricker
"""SOLIX S2000 (AS220).

This is based on the recorded telemetry from SolixBLE PR #65: the
S2000's ``c421`` is the A1783's ``0421`` layout, with a longer ``a6`` and
``a7``, one aggregate USB block (``aa``) and its own ``dd``-``df``, ``f0``
and ``f8``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from construct import Bytes, Container, ExprAdapter, Int16ul, Struct

from anker_ble_emulator.layouts import TYPE_VAR, Value, ui
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    FieldGroup,
    FieldSpec,
    ParameterDict,
    TableLayout,
    decode_request,
    leaf_zeros,
    one_field,
    struct_zeros,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product
from anker_ble_emulator.timed import Step, Timed

from .base import Advert, ProductDevice, Profile, register
from .solix_c_gen2 import (
    AC_GROUP,
    AC_OUT,
    AC_OVER_CEILING,
    CAPABILITY_ZEROS,
    CHARGE_OVER_LIMIT,
    LINE_COMMANDS,
    MSGTYPE_421,
    MSGTYPE_900,
    OUTPUT_FIELDS,
    SETTINGS_FIELDS,
    TAIL_ZEROS,
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
    ac_output_off,
    ac_output_switched,
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
#: ``f0`` (4 B): ``[0]`` is 1 while AC passes through, the AC output on with AC
#: coming in (PiField s2000-as220.md "Telemetry"); no app name is known. The rest
#: isn't named.
PASSTHROUGH = "ac_pass_through_status"
Charger = Struct(PASSTHROUGH / ui(1), "_reserved" / Bytes(3))
#: ``a4[28:30]``, in the A1783 tail's reserved bytes: the Smart AC Output
#: timeout in minutes (``4101 aa``); no app name is known. ``a4[26:28]``, the
#: app's ``countryCode``, stays as recorded.
SMART_AC_TIMEOUT = "smart_ac_output_timeout"
TIMEOUT_IN_RESERVED = slice(2, 4)


def _timeout_decode(obj: Container[Any], _: object) -> Container[Any]:
    """Lift the Smart AC Output timeout out of the tail; 0 on the short block."""
    out = Container(obj)
    tail = out["tail"]
    reserved = b"" if tail is None else tail.reserved[TIMEOUT_IN_RESERVED]
    out[SMART_AC_TIMEOUT] = Int16ul.parse(reserved) if reserved else 0
    return out


def _timeout_encode(obj: Container[Any], _: object) -> Container[Any]:
    """Put the timeout back in the tail; a short block has nowhere for it."""
    out = Container(obj)
    timeout = out.pop(SMART_AC_TIMEOUT)
    tail = out.get("tail", TAIL_ZEROS)
    if tail is not None:
        reserved = bytearray(tail.reserved)
        reserved[TIMEOUT_IN_RESERVED] = Int16ul.build(timeout)
        out["tail"] = Container(tail, reserved=bytes(reserved))
    return out


#: ``a4``: the A1783's settings block, with the timeout named.
AS220Settings = ExprAdapter(Settings, decoder=_timeout_decode, encoder=_timeout_encode)


#: The typed tags. ``a4``/``a5``/``a8`` and ``d9``/``dc``/``f9``/``fa`` are
#: the A1783's.
TELEMETRY_FIELDS: dict[str, FieldSpec] = {
    "a4": (AS220Settings, 0x04, leaf_zeros(*SETTINGS_FIELDS, SMART_AC_TIMEOUT)),
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
#: 1150 W charging figure is the nameplate: the charge holds to the charger's
#: 1600 W UltraFast rating whatever the limit and the flag (1520 W recorded at
#: a 600 W limit, 1-3 s after a switch), and AC in to the bypass rating plus
#: the C Gen 2's +33 W overshoot band.
CHARGE_RATING = 1600
AC_IN_TOP = 1800 + AC_OVER_CEILING
SOLAR_RATING = 400
AC_OUTPUT_PEAK = 3000
#: ``aa`` totals USB-C 100 W, USB-C 15 W and USB-A 12 W; the last two share 18 W.
USB_RATING = 100 + 18
AC_IN, PV_IN, USB_OUT = "ac_input_power", "pv1_input_power", "pd1_output_power"
AC_LIMIT = "ac_recharge_power"
INPUT_TOTAL, OUTPUT_TOTAL = "total_input_power", "total_output_power"


def _split_input(
    values: Mapping[str, Value], merged: Mapping[str, Value]
) -> dict[str, Value]:
    """Return the AC in and solar an input total set with at most one of them leaves.

    Alone, AC in takes the charge the limit requests (+10 W; 1600 W with
    UltraFast) plus the AC out, at most the total and 1833 W, and solar the rest; with
    one input, the other is the rest. With both, none.
    """
    total = int(values[INPUT_TOTAL])
    if PV_IN in values:
        if AC_IN in values:
            return {}
        return {AC_IN: total - int(values[PV_IN])}
    if AC_IN in values:
        ac = int(values[AC_IN])
    else:
        limit = int(merged[AC_LIMIT]) + CHARGE_OVER_LIMIT
        request = CHARGE_RATING if merged[ULTRAFAST] else limit
        ac = min(total, request + int(merged[AC_OUT]), AC_IN_TOP)
    return {AC_IN: ac, PV_IN: total - ac}


def _tied_totals(
    values: Mapping[str, Value], state: Mapping[str, Value]
) -> dict[str, Value]:
    """Return ``values`` with ``a6``'s totals tied to their parts on ``state``.

    The input total is AC in + solar and the output total AC out + USB, as in
    every recorded frame: a part change moves its total.

    Raises:
        TypeError: If a total set by name can't be its parts' sum.

    """
    resolved, merged = dict(values), {**state, **values}
    if INPUT_TOTAL in values:
        resolved |= _split_input(values, merged)
    elif values.keys() & {AC_IN, PV_IN}:
        resolved[INPUT_TOTAL] = int(merged[AC_IN]) + int(merged[PV_IN])
    after = {**state, **resolved}
    ac, pv, total = int(after[AC_IN]), int(after[PV_IN]), int(after[INPUT_TOTAL])
    if INPUT_TOTAL in values and min(ac, pv) < 0:
        msg = f"AC in and solar can't sum to {total} W"
        raise TypeError(msg)
    if INPUT_TOTAL in values and ac + pv != total:
        msg = f"the input total is AC in + solar, {ac + pv} W, not {total} W"
        raise TypeError(msg)
    outputs, shown = int(after[AC_OUT]) + int(after[USB_OUT]), int(after[OUTPUT_TOTAL])
    if OUTPUT_TOTAL in values and shown != outputs:
        msg = f"the output total is AC out + USB, {outputs} W, not {shown} W"
        raise TypeError(msg)
    if OUTPUT_TOTAL not in values and values.keys() & {AC_OUT, USB_OUT}:
        resolved[OUTPUT_TOTAL] = outputs
    return resolved


#: ``4101``: the C Gen 2's AC group, and ``aa`` the Smart AC Output timeout
#: (PiField s2000-as220.md "Setters").
AC_PARAMS = {**AC_GROUP, "aa": (SMART_AC_TIMEOUT, ui(2))}
SCREEN = "screen_switch"
#: A ``4103 a2`` screen switch waiting to show.
SCREEN_REQUEST = "screen_switch_request"
#: ``4103 a2`` shows in the ``0421`` after its reply's, under a second on.
SCREEN_DELAY = 0.5
PASSTHROUGH_EVENT = "ac_pass_through"


def _set_ac_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return the AC group's changed fields; any subset of its tags may be present."""
    return decode_request(request, AC_PARAMS)


def _set_system_params(request: ParameterDict) -> dict[str, Value] | None:
    """Return the C Gen 2 system group's changes, the screen switch held back."""
    changes = LINE_COMMANDS[0x103](request)
    if changes is not None and SCREEN in changes:
        changes[SCREEN_REQUEST] = changes.pop(SCREEN)
    return changes


def _screen_shown(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
    """Show the held screen switch and push the ``0421`` that carries it."""
    return Step(
        values={SCREEN: state[SCREEN_REQUEST]},
        unset=(SCREEN_REQUEST,),
        pushes=(MSGTYPE_421,),
    )


def _outputs(state: Mapping[str, Value]) -> int:
    """Return what ``a6[0:2]`` sums: AC out and the USB total."""
    return int(state[AC_OUT]) + int(state[USB_OUT])


def _output_total_follows(
    _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Set ``a6[0:2]`` to AC out and the USB total."""
    return Step(values={OUTPUT_TOTAL: _outputs(state)})


def _passes_through(state: Mapping[str, Value]) -> int:
    """Return ``f0[0]`` for ``state``: 1 with the AC output on and AC coming in."""
    return int(bool(state["ac_output_status"]) and int(state[AC_IN]) > 0)


def _pass_through_follows(
    _values: Mapping[str, Value], state: Mapping[str, Value]
) -> Step:
    """Set ``f0[0]`` from the AC output switch and AC in."""
    return Step(values={PASSTHROUGH: _passes_through(state)})


class AS220Layout(TableLayout):
    """The S2000's own telemetry code; satisfies ``LayoutProtocol``."""

    COMMANDS = MappingProxyType({0x101: _set_ac_params, 0x103: _set_system_params})
    SIDE_EFFECTS = MappingProxyType({0x101: ac_output_switched})
    FIELDS = MappingProxyType(
        {MSGTYPE_421: TELEMETRY_FIELDS, MSGTYPE_900: TELEMETRY_FIELDS}
    )
    GROUPS = MappingProxyType(
        {MSGTYPE_421: (OUTPUT_PORTS,), MSGTYPE_900: (OUTPUT_PORTS,)}
    )

    def resolve(
        self, values: Mapping[str, Value], state: Mapping[str, Value]
    ) -> dict[str, Value]:
        """Return what ``values`` set on ``state``: the totals follow their parts.

        A rating is checked when the change sets a power it reads.

        AC out reads 0 W with the AC output off.

        Raises:
            TypeError: If a total can't be its parts' sum, AC out is set over 0 W
                with the AC output off, or a power would pass its rating;
                nothing is set then.

        """
        resolved = _tied_totals(ac_output_off(values, state), state)
        after = {**state, **resolved}
        ac_in, ac_out = int(after[AC_IN]), int(after[AC_OUT])
        limits = (
            ({AC_IN}, "AC in", ac_in, AC_IN_TOP),
            ({AC_IN, AC_OUT}, "the AC charge", ac_in - ac_out, CHARGE_RATING),
            ({PV_IN}, "solar", int(after[PV_IN]), SOLAR_RATING),
            ({AC_OUT}, "AC out", ac_out, AC_OUTPUT_PEAK),
            ({USB_OUT}, "USB", int(after[USB_OUT]), USB_RATING),
        )
        for names, what, power, rating in limits:
            if names & resolved.keys() and power > rating:
                msg = f"{what} reads at most {rating} W on the S2000, not {power} W"
                raise TypeError(msg)
        passing = _passes_through(after)
        if PASSTHROUGH in values and int(after[PASSTHROUGH]) != passing:
            msg = f"{PASSTHROUGH} follows the AC output and AC in: {passing}"
            raise TypeError(msg)
        return resolved

    def timed(
        self, before: Mapping[str, Value], after: Mapping[str, Value]
    ) -> tuple[Timed, ...]:
        """Return the screen switch a ``4103 a2`` holds and ``f0[0]``'s follow.

        A request a pending one repeats keeps the pending one's time. A
        command's change of AC out moves the output total too.
        """
        events = []
        if before.get(SCREEN_REQUEST) != after.get(SCREEN_REQUEST):
            events.append(Timed(SCREEN_DELAY, SCREEN_REQUEST, _screen_shown))
        if int(after[OUTPUT_TOTAL]) != _outputs(after):
            events.append(Timed(0.0, OUTPUT_TOTAL, _output_total_follows))
        if int(after[PASSTHROUGH]) != _passes_through(after):
            events.append(Timed(0.0, PASSTHROUGH_EVENT, _pass_through_follows))
        return tuple(events)


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
        # A 0421 follows each setter's ack (s2000-as220.md "Setters").
        0x101: (0x901, MSGTYPE_421),
        0x103: (0x903, MSGTYPE_421),
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
