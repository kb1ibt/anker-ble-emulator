# Copyright (c) 2026 Shawn Stricker
"""Prime Charger 250W (A2345)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

PROFILE = Profile(
    serial="AQLQJB0000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_9_7,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(
        local_name=None,
        version_code=0x01,
        bind_type=0x00,
        product_type=bytes.fromhex("b402"),
        sku=b"QJB",
        capability=0x04,
        prime_name=True,
    ),
    data=("a2345.json",),
    replies={
        0x200: (0xA00,),
        0x20A: (0xA0A,),
        0x20B: (0xA0B, 0x303),
    },
    pushes=(0x303,),
    device_version="v2.1.1.6",
    version_names=("A2345", "A2345_mcu", "A2345_esp32"),
    # The MCU's fid 0x0f handlers; 0x201 is a factory reset.
    known_commands=frozenset(range(0x200, 0x215)) | {0x218, 0x220, 0x221, 0x222, 0x223},
)
register(Product.A2345, PROFILE)


class A2345(EmulatedDevice):
    """Prime Charger 250W: encrypted outer, ECDH, owner confirmation by button.

    Its module build (v0.2.9.7) also accepts the plain outer.
    """

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
        """Build a Prime Charger 250W; see ``EmulatedDevice``."""
        super().__init__(
            Product.A2345,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
