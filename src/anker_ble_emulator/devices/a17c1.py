# Copyright (c) 2026 Shawn Stricker
"""Solarbank 2 E1600 Pro (A17C1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="APCGQ80E00000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.ESP32_0_0_0_3,
    auth_mode=AuthMode.CONFIRM,
    # No advertisement record is recorded for the A17C1.
    advert=Advert(local_name=None),
    data=("a17c1.json",),
    replies={},
    # The telemetry arrives in three fragments; 0x409 is a short status push.
    pushes=(0x405, 0x409),
    # No 0830 is recorded, so 0030 goes unanswered.
    device_version=None,
)
register(Product.A17C1, PROFILE)


class A17C1(EmulatedDevice):
    """Solarbank 2 E1600 Pro: plain outer, ECDH, owner confirmation by button."""

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
        """Build a Solarbank 2 E1600 Pro; see ``EmulatedDevice``."""
        super().__init__(
            Product.A17C1,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
