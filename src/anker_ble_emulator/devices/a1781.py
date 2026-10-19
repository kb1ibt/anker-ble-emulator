# Copyright (c) 2026 Shawn Stricker
"""SOLIX F2600 (A1781)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="AZV3NM0000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.ESP32_0_0_0_3,
    auth_mode=AuthMode.CONFIRM,
    # No advertisement record is recorded for the A1781.
    advert=Advert(local_name=None),
    data=("a1781.json",),
    # The recorded c840 starts at a1; its 00 status byte is the reply rule's.
    replies={0x040: (0x840,)},
    # No push is recorded.
    pushes=(),
    # No 0830 is recorded, so 0030 goes unanswered.
    device_version=None,
    # SolixBLE's F2600 requests; only 0x040 has a recorded answer.
    known_commands=frozenset(
        {0x040, 0x042, 0x043, 0x044, 0x046, 0x04A, 0x04B, 0x04C, 0x04E, 0x04F} | {0x052}
    ),
)
register(Product.A1781, PROFILE)


class A1781(EmulatedDevice):
    """SOLIX F2600: plain outer, ECDH, owner confirmation by button."""

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
        """Build a SOLIX F2600; see ``EmulatedDevice``."""
        super().__init__(
            Product.A1781,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
            mtu=mtu,
        )
