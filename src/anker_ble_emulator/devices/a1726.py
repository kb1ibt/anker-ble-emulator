# Copyright (c) 2026 Shawn Stricker
"""SOLIX C300 DC (A1726)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="AZVUW00000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_1_5_8,
    # The button confirmation isn't recorded; it's the C300's.
    auth_mode=AuthMode.CONFIRM,
    # The name is recorded; the manufacturer record isn't.
    advert=Advert(local_name="Anker SOLIX C300 DC"),
    data=("a1726.json",),
    # The setter acks the A1726 sent to MQTT; no status request is recorded.
    replies={
        0x043: (0x843,),
        0x04B: (0x84B,),
        0x04F: (0x84F,),
        0x052: (0x852,),
        0x057: (0x857,),
    },
    # Recorded on device firmware v0.1.2.8, which sent it in the clear.
    pushes=(0x402,),
    device_version="v0.1.2.8",
    known_commands=frozenset({0x043, 0x04B, 0x04F, 0x052, 0x057}),
)
register(Product.A1726, PROFILE)


class A1726(EmulatedDevice):
    """SOLIX C300 DC: plain outer, ECDH, owner confirmation by button."""

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
        """Build a C300 DC; see ``EmulatedDevice``."""
        super().__init__(
            Product.A1726,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
            mtu=mtu,
        )
