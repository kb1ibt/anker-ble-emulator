# Copyright (c) 2026 Shawn Stricker
"""Solarbank 2 E1600 Plus (A17C3), from its map and the A17C1's recording.

Nothing about the A17C3 is recorded. The app reads the Solarbank 2 models with
one parser, and anker-solix-api maps the A17C3's ``0405`` with the A17C1's
fields, so its telemetry is typed from the A17C1's recorded BLE ``c405``
(``a17c1.json``): on BLE, ``a5 a7 b6 c2 c9 ca cb`` differ in type or length
from the map's MQTT ``0405``. Tags the map doesn't name are left out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.layouts import TYPE_SILE, TYPE_STR, TYPE_UI, TYPE_VAR, Field
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product, Transport

from .base import DEFAULT_MAC, UNSET, Advert, EmulatedDevice, Profile, Unset, register


if TYPE_CHECKING:
    from anker_ble_emulator.clock import Clock

#: The ``0405`` fields the map names, typed as the A17C1's ``c405`` carries them.
TELEMETRY = (
    Field(0xA2, "device_sn", TYPE_STR, 17),
    Field(0xA3, "main_battery_soc", TYPE_UI, 2),
    Field(0xA5, "error_code", TYPE_SILE, 3),
    Field(0xA6, "sw_version", TYPE_VAR, 5),
    Field(0xA7, "sw_controller?", TYPE_VAR, 5),
    Field(0xA8, "sw_expansion", TYPE_VAR, 5),
    Field(0xA9, "temp_unit_fahrenheit", TYPE_UI, 2),
    Field(0xAA, "temperature", TYPE_UI, 2),
    Field(0xAB, "photovoltaic_power", TYPE_VAR, 5),
    Field(0xAC, "ac_output_power", TYPE_VAR, 5),
    Field(0xAD, "battery_soc", TYPE_UI, 2),
    Field(0xB0, "bat_charge_power", TYPE_VAR, 5),
    Field(0xB1, "pv_yield", TYPE_VAR, 5),
    Field(0xB2, "charged_energy", TYPE_VAR, 5),
    Field(0xB3, "output_energy", TYPE_VAR, 5),
    Field(0xB4, "min_soc", TYPE_UI, 2),
    Field(0xB5, "lowpower_input_data", TYPE_UI, 2),
    Field(0xB6, "active_charge_soc", TYPE_UI, 2),
    Field(0xB7, "bat_discharge_power", TYPE_VAR, 5),
    Field(0xBC, "grid_to_home_power", TYPE_VAR, 5),
    Field(0xBD, "pv_to_grid_power", TYPE_VAR, 5),
    Field(0xBE, "grid_import_energy", TYPE_VAR, 5),
    Field(0xBF, "grid_export_energy", TYPE_VAR, 5),
    Field(0xC2, "max_load", TYPE_SILE, 3),
    Field(0xC4, "home_demand", TYPE_VAR, 5),
    Field(0xC6, "usage_mode", TYPE_UI, 2),
    Field(0xC7, "home_load_preset", TYPE_SILE, 3),
    Field(0xC8, "ac_socket_power", TYPE_VAR, 5),
    Field(0xC9, "consumed_energy", TYPE_VAR, 5),
    Field(0xCA, "pv_1_power", TYPE_VAR, 5),
    Field(0xCB, "pv_2_power", TYPE_VAR, 5),
    Field(0xCC, "pv_3_power", TYPE_VAR, 5),
    Field(0xCD, "pv_4_power", TYPE_VAR, 5),
    Field(0xD2, "light_mode", TYPE_UI, 2),
    Field(0xD3, "output_power", TYPE_VAR, 5),
    Field(0xE0, "grid_status", TYPE_UI, 2),
    Field(0xE1, "light_off_switch", TYPE_UI, 2),
    Field(0xE8, "battery_heating", TYPE_UI, 2),
    Field(0xFE, "msg_timestamp", TYPE_VAR, 5),
)

PROFILE = Profile(
    serial="A17C300000000001",
    # Assumed from the A17C1: plain outer, its module's 0829 (ESP32 0.0.0.3).
    outer=Outer.PLAIN,
    path=Path.ECDH,
    module_build=ModuleBuild.UNRECORDED,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(local_name=None),
    data=(),
    replies={},
    pushes=(0x405,),
    device_version=None,
    known_commands=frozenset({0x050, 0x057, 0x05A, 0x067, 0x068, 0x080}),
    built=(0x405,),
    map_built=True,
    retyped={0x405: TELEMETRY},
)
register(Product.A17C3, PROFILE)


class A17C3(EmulatedDevice):
    """Solarbank 2 E1600 Plus: plain outer (assumed), ECDH, button confirmation."""

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
        """Build a Solarbank 2 E1600 Plus; see ``EmulatedDevice``."""
        super().__init__(
            Product.A17C3,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
        )
