# Copyright (c) 2026 Shawn Stricker
"""SOLIX S2000 (AS220)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

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
        0x100: (0x900, 0x421),
        0x101: (0x901,),
        0x103: (0x903,),
    },
    pushes=(0x421,),
    # No 0830 is recorded, so 0030 goes unanswered.
    device_version=None,
    known_commands=frozenset({0x040, 0x057, 0x05E, 0x090, 0x093, 0x100, 0x101, 0x103}),
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
        )
