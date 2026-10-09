# Copyright (c) 2026 Shawn Stricker
"""Solarbank E1600 (A17C0), from its anker-solix-api map; nothing is recorded."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="A17C000000000001",
    # Assumed: no Solarbank 1 handshake is recorded; the Solarbank 2 (A17C1)
    # negotiates on the plain outer.
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=(),
    # The status request draws the 0405 telemetry, not an 0840 reply.
    replies={0x040: (0x405,)},
    pushes=(0x405,),
    device_version=None,
    # The map's commands, and the app's setDischargeTime (005e).
    known_commands=frozenset({0x040, 0x050, 0x056, 0x057, 0x05E, 0x067, 0x068}),
    built=(0x405,),
    map_built=True,
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
        )
