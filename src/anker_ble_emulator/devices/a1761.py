# Copyright (c) 2026 Shawn Stricker
"""SOLIX C1000 (A1761), the first generation."""

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
    serial="APC9FE0000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_3_1,
    auth_mode=AuthMode.CONFIRM,
    # No advertisement record is recorded for the A1761.
    advert=Advert(local_name=None),
    data=("a1761.json",),
    replies={0x040: (0x840,)},
    pushes=(0x402,),
    device_version="v1.5.9",
    known_commands=frozenset(
        {0x040, 0x042, 0x043, 0x044, 0x045, 0x046, 0x04A, 0x04B, 0x04C, 0x04F}
        | {0x050, 0x052, 0x057, 0x05E, 0x076, 0x077}
    ),
    layout_aliases={0x402: STATUS, 0x840: STATUS},
)
register(Product.A1761, PROFILE)


class A1761(EmulatedDevice):
    """SOLIX C1000: plain outer, ECDH, owner confirmation by button."""

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
        """Build a C1000; see ``EmulatedDevice``."""
        super().__init__(
            Product.A1761,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
