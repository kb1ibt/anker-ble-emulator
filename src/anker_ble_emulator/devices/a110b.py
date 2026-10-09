# Copyright (c) 2026 Shawn Stricker
"""Prime Power Bank 20K (A110B)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.frame import CHANNEL_APP
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="AJ7DL500000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.CHARGING_0_0_5_1,
    auth_mode=AuthMode.CONFIRM,
    # Only the product type (b406) is known, not the rest of the record.
    advert=Advert(local_name=None),
    data=("a110b.json",),
    replies={0x200: (0xA00,)},
    pushes=(0x300,),
    # No 0830 is recorded, so 0030 goes unanswered.
    device_version=None,
    # The recorded request; SolixBLE's 0x20A got no recorded answer.
    known_commands=frozenset({0x200}),
    chip=b"Charging",
    lib_version=b"v0.0.5.1",
    serial_tail=11,
    fragment_cap=297,
    mcu_channel=CHANNEL_APP,
)
register(Product.A110B, PROFILE)


class A110B(EmulatedDevice):
    """Prime Power Bank 20K: encrypted outer, ECDH, owner confirmation by button."""

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
        """Build a Prime Power Bank 20K; see ``EmulatedDevice``."""
        super().__init__(
            Product.A110B,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
