# Copyright (c) 2026 Shawn Stricker
"""SOLIX C800 (A1753)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="A175300000000001",
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_3_1,
    # The button confirmation isn't recorded; it's the C1000's.
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    # Telemetry borrowed from the C1000 (A1761) less its f9 and fd (the C1000's
    # pack type): only the C800's MQTT 0405 is recorded, and its BLE c402
    # differs from that; the C800 runs SolixBLE's C1000 class. Its frames
    # carry the A1761's serial.
    data=("a1753.json",),
    replies={0x040: (0x840,)},
    pushes=(0x402,),
    device_version="v3.5.6",
    # The requests the app sent to an A1753; no setter ack is recorded.
    known_commands=frozenset(
        {0x040, 0x042, 0x044, 0x045, 0x046, 0x04C, 0x04F, 0x050, 0x057, 0x05E}
        | {0x072, 0x076, 0x077}
    ),
)
register(Product.A1753, PROFILE)


class A1753(EmulatedDevice):
    """SOLIX C800: plain outer, ECDH, owner confirmation by button."""

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
        """Build a C800; see ``EmulatedDevice``."""
        super().__init__(
            Product.A1753,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
            mtu=mtu,
        )
