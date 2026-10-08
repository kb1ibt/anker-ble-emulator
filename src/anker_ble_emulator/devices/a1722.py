# Copyright (c) 2026 Shawn Stricker
"""SOLIX C300 (A1722)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

#: anker-solix-api maps the MQTT ``0405``, which the BLE status frames equal.
STATUS = 0x405

PROFILE = Profile(
    serial="AZVSBJ0000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_9_8,
    auth_mode=AuthMode.CONFIRM,
    # No advertisement record is recorded for the A1722.
    advert=Advert(local_name=None),
    data=("a1722.json",),
    replies={0x040: (0x840,)},
    pushes=(0x402,),
    device_version="v1.0.5.7",
    version_names=("A1722_high", "A1722_mcu_high", "A1722_esp32_high"),
    # The requests anker-solix-api#348 recorded, and the map's 0x057.
    known_commands=frozenset(
        {0x040, 0x042, 0x043, 0x044, 0x045, 0x046, 0x04A, 0x04B, 0x04C, 0x04F}
        | {0x050, 0x052, 0x057, 0x076, 0x077, 0x079}
    ),
    layout_aliases={0x402: STATUS, 0x840: STATUS},
)
register(Product.A1722, PROFILE)


class A1722(EmulatedDevice):
    """SOLIX C300: plain outer, ECDH, owner confirmation by button."""

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
    ) -> None:
        """Build a C300; see ``EmulatedDevice``."""
        super().__init__(
            Product.A1722,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
