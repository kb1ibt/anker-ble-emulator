# Copyright (c) 2026 Shawn Stricker
"""The C Gen 2 ``f9`` versions that drop out: INVVersion and subMCUVersion.

a1783-telemetry.md "Versions (``f9``)": the ACDC DSP's version reads only while
that stage is energised, and the DCDC DSP's reads 0 for about a second once a
minute with both AC paths off.
"""

from __future__ import annotations

from anker_ble_emulator import A1783, A1785, ManualClock
from anker_ble_emulator.devices import solix_c_gen2
from anker_ble_emulator.parameters import decode_field
from tests.fixtures.cgen2 import authorized_client, next_0421


TOKEN = b"owner-token"
#: v0.1.2.6, the recorded A1783 / A1785 DSP version.
DSP_VERSION = bytes.fromhex("06020100")
NO_VERSION = bytes(4)


def test_inv_version_follows_mains_a_second_behind() -> None:
    """Mains going clears ``f9[12:16]`` ~1 s on; mains back sets it ~1 s on.

    BLE 2026-09-23 05:08:24 -> 25: ``a7[3]`` 0 -> 1, then ``06020100`` a frame later.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    authorized_client(device, TOKEN)
    module = device.negotiated_module
    seen = []

    device.set_values(ac_input_status=0, ac_output_status=0)
    for seconds in (0.9, 0.2):
        clock.advance(seconds)
        module.check_pushes()
        seen.append(
            decode_field(next_0421(device)[1], "f9", solix_c_gen2.DeviceVersions)
        )
    device.set_values(ac_input_status=1)
    for seconds in (0.9, 0.2):
        clock.advance(seconds)
        module.check_pushes()
        seen.append(
            decode_field(next_0421(device)[1], "f9", solix_c_gen2.DeviceVersions)
        )

    assert [versions.inv_version for versions in seen] == [
        DSP_VERSION,
        NO_VERSION,
        NO_VERSION,
        DSP_VERSION,
    ]


def test_inv_version_outlasts_ac_output_off_by_about_49_seconds() -> None:
    """With no mains, AC output on keeps it; off clears it 47-49 s later.

    BLE 2026-08-29: AC off 12:20:30.852, still set 12:21:17.892, 0 at 12:21:19.935.
    """
    clock = ManualClock()
    device = A1785(clock=clock)
    authorized_client(device, TOKEN)
    module = device.negotiated_module
    seen = []

    device.set_values(ac_input_status=0)
    clock.advance(5)
    module.check_pushes()
    seen.append(decode_field(next_0421(device)[1], "f9", solix_c_gen2.DeviceVersions))
    device.set_values(ac_output_status=0)
    for seconds in (47.1, 1.9):
        clock.advance(seconds)
        module.check_pushes()
        seen.append(
            decode_field(next_0421(device)[1], "f9", solix_c_gen2.DeviceVersions)
        )

    assert [versions.inv_version for versions in seen] == [
        DSP_VERSION,
        DSP_VERSION,
        NO_VERSION,
    ]


def test_sub_mcu_version_reads_0_for_a_second_each_minute_with_ac_off() -> None:
    """Both AC paths off: ``f9[4:8]`` 0 for ~1 s about once a minute.

    BLE 2026-08-29 12:21:31.693 c900 and 32.050 c421 (one ``fe``) read 0; the
    12:21:32.775 c421 is back. With mains in, no dip.
    """
    clock = ManualClock()
    off = A1783(clock=clock)
    on = A1783(clock=clock)
    for device in (off, on):
        authorized_client(device, TOKEN)
    off.set_values(ac_input_status=0, ac_output_status=0)
    seen = []

    for seconds in (59.9, 0.2, 1.0, 59.0):
        clock.advance(seconds)
        for device in (off, on):
            device.negotiated_module.check_pushes()
        seen.append(
            tuple(
                decode_field(
                    next_0421(device)[1], "f9", solix_c_gen2.DeviceVersions
                ).sub_mcu_version
                for device in (off, on)
            )
        )

    assert seen == [
        (DSP_VERSION, DSP_VERSION),
        (NO_VERSION, DSP_VERSION),
        (DSP_VERSION, DSP_VERSION),
        (NO_VERSION, DSP_VERSION),
    ]


def test_ac_output_back_on_within_49_s_keeps_inv_version() -> None:
    """The stage energised again before the dropout: ``f9[12:16]`` never clears."""
    clock = ManualClock()
    device = A1785(clock=clock)
    authorized_client(device, TOKEN)
    module = device.negotiated_module
    device.set_values(ac_input_status=0)

    device.set_values(ac_output_status=0)
    clock.advance(10)
    module.check_pushes()
    device.set_values(ac_output_status=1)
    clock.advance(60)
    module.check_pushes()

    versions = decode_field(next_0421(device)[1], "f9", solix_c_gen2.DeviceVersions)
    assert versions.inv_version == DSP_VERSION
