# Copyright (c) 2026 Shawn Stricker
"""MagGo 3-in-1 Wireless Charger (A25X7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="A25X700000000001",
    # SolixBLE's MagGo3in1 class; its handshake isn't recorded, so the module
    # build, auth mode, 0829, MTU cap and MCU channel are the defaults.
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=("a25x7.json",),
    # SolixBLE's 0x200 subscribe has no recorded answer.
    replies={},
    pushes=(0x300,),
    device_version=None,
    known_commands=frozenset({0x200}),
)
register(Product.A25X7, PROFILE)


class A25X7(EmulatedDevice):
    """MagGo 3-in-1 Wireless Charger: encrypted outer, ECDH; telemetry on 4300."""

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
        """Build a MagGo 3-in-1 Wireless Charger; see ``EmulatedDevice``."""
        super().__init__(
            Product.A25X7,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
