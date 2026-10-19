# Copyright (c) 2026 Shawn Stricker
"""SOLIX S2000 (AS220).

This is based on the recorded telemetry from SolixBLE PR #65: the
S2000's ``c421`` is the A1783's ``0421`` layout, with a longer ``a6`` and
``a7`` and one aggregate USB block (``aa``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from construct import Bytes, Construct, Container, Struct

from anker_ble_emulator.layouts import TYPE_VAR, ui
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    CommandDispatchLayout,
    ParameterContainer,
    Parameters,
    StructFieldLayout,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register
from .solix_c_gen2 import POWER_FIELDS, SETTINGS_FIELDS, PowerStatus, Settings


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator.clock import Clock
    from anker_ble_emulator.layouts import Value

#: The telemetry push, and the ``4100`` reply (a status byte, then the same TLV).
MSGTYPE_421 = 0x421
MSGTYPE_900 = 0x900

#: ``a5`` (5 B): the doc's offsets; the last byte isn't named.
Battery = Struct(
    "temperature" / ui(1, signed=True),
    "working_status" / ui(1),
    "battery_soc" / ui(1),
    "battery_soh" / ui(1),
    "_reserved" / Bytes(1),
)
#: ``a6`` (13 B, the A1783's 9): byte 9 isn't named.
Power = Struct(
    "output_power_total" / ui(2),
    "ac_input_power_total" / ui(2),
    "dc_input_power_total" / ui(2),
    "remaining_time_hours" / ui(2),
    "main_battery_soc" / ui(1),
    "_reserved" / Bytes(1),
    "ac_input_plug_status" / ui(1),
    "input_power_total" / ui(2),
)
#: ``a7`` (11 B, the A1783's 6): the trailing 5 bytes aren't named.
AcOutput = Struct(
    "ac_output_status" / ui(1),
    "ac_output_power" / ui(2),
    "ac_input_status" / ui(1),
    "ac_input_power" / ui(2),
    "_reserved" / Bytes(5),
)


def _leaves(codec: Construct[Container[Any], Any]) -> tuple[str, ...]:
    """Return a struct's named leaves, without its unnamed bytes."""
    return tuple(
        sc.name
        for sc in codec.subcons  # type: ignore[attr-defined]  # Struct's own subcons
        if sc.name and not sc.name.startswith("_")
    )


def _zeroed(codec: Construct[Container[Any], Any]) -> dict[str, Any]:
    """Return a value for every field of ``codec``: 0, or zero bytes."""
    return {
        sc.name: bytes(sc.sizeof()) if sc.name.startswith("_") else 0
        for sc in codec.subcons  # type: ignore[attr-defined]  # Struct's own subcons
        if sc.name
    }


#: Single-instance blocks: tag -> (codec, its type byte, its leaf names).
SINGLE_FIELDS: dict[
    str, tuple[Construct[Container[Any], Any], int, tuple[str, ...]]
] = {
    "a4": (Settings, 0x04, SETTINGS_FIELDS),
    "a5": (Battery, 0x04, _leaves(Battery)),
    "a6": (Power, 0x04, _leaves(Power)),
    "a7": (AcOutput, 0x04, _leaves(AcOutput)),
}
#: Each block's all-zero value, for checking one field against its codec.
ZEROS = {
    "a4": dict.fromkeys(SETTINGS_FIELDS, 0),
    "a5": _zeroed(Battery),
    "a6": _zeroed(Power),
    "a7": _zeroed(AcOutput),
}
#: Status + power blocks: tag -> name. ``aa`` is one aggregate USB total.
POWER_PORTS = {"a8": "dc_input", "aa": "usb"}


class AS220Layout(StructFieldLayout, CommandDispatchLayout):
    """The S2000's own telemetry code; satisfies ``LayoutProtocol``."""

    TELEMETRY_MSGTYPES = (MSGTYPE_421, MSGTYPE_900)

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names ``set_values()`` can reach in ``msgtype``."""
        if msgtype not in self.TELEMETRY_MSGTYPES:
            return frozenset()
        leaves = {name for _, _, names in SINGLE_FIELDS.values() for name in names}
        leaves |= {f"{p}_{sub}" for p in POWER_PORTS.values() for sub in POWER_FIELDS}
        return frozenset({*leaves, "msg_timestamp"})

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages with a named field matching ``name``."""
        return tuple(m for m in self.TELEMETRY_MSGTYPES if name in self.names(m))

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Validate that each named value fits its field; the bytes aren't used.

        The telemetry is always recorded; this lets ``set_values()`` check a
        value against its field before storing it.

        Raises:
            ConstructError: If a value doesn't fit its field.

        """
        if msgtype not in self.TELEMETRY_MSGTYPES:
            return b""
        for name, value in (values or {}).items():
            for tag, (codec, _, field_names) in SINGLE_FIELDS.items():
                if name in field_names:
                    codec.build(ZEROS[tag] | {name: value})
            for port in POWER_PORTS.values():
                if name.startswith(f"{port}_"):
                    PowerStatus.build(
                        dict.fromkeys(POWER_FIELDS, 0) | {name[len(port) + 1 :]: value}
                    )
            if name == "msg_timestamp":
                ui(4).build(value)
        return b""

    def update(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> bytes:
        """Return ``payload`` with the given named fields set."""
        if msgtype not in self.TELEMETRY_MSGTYPES:
            return payload
        params = Parameters.parse(payload)
        for tag, (codec, type_byte, field_names) in SINGLE_FIELDS.items():
            changed = {n: values[n] for n in field_names if n in values}
            if changed:
                self._encode_field(params, tag, codec, type_byte, **changed)
        for tag, port in POWER_PORTS.items():
            changed = {
                s: values[f"{port}_{s}"]
                for s in POWER_FIELDS
                if f"{port}_{s}" in values
            }
            if changed:
                self._encode_field(params, tag, PowerStatus, 0x04, **changed)
        if "msg_timestamp" in values:
            params["fe"] = ParameterContainer(
                key=b"\xfe", type=TYPE_VAR, value=ui(4).build(values["msg_timestamp"])
            )
        return Parameters.build(params)


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
    known_commands=frozenset({0x040, 0x057, 0x05E, 0x090, 0x093, 0x100, 0x101, 0x103}),
    layout=AS220Layout(),
)
register(Product.AS220, PROFILE)


class AS220(EmulatedDevice):
    """SOLIX S2000: encrypted outer, ECDH, owner confirmation by button."""

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
        """Build an S2000; see ``EmulatedDevice``."""
        super().__init__(
            Product.AS220,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
            mtu=mtu,
        )
