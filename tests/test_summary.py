# Copyright (c) 2026 Shawn Stricker
"""The C Gen 2 ``c490`` summary: read and set by field name."""

from __future__ import annotations

import pytest

from anker_ble_emulator import A1763, A1765, A1783, A1785, A2345
from anker_ble_emulator.layouts import LayoutError


#: Cell voltages are 16 x u16 LE, so 32 bytes.
CELL_BYTES = 32


def test_the_recorded_summary_reads_back_unchanged() -> None:
    script = A1783().mcu
    summary = script.summary
    assert summary is not None
    recorded = script.push(0x490).payload

    values = summary.read(recorded)

    assert set(values) == set(summary.fields)
    assert summary.update(recorded, values) == recorded
    assert summary.update(recorded, {"ac_input_power_total": 5}) == recorded
    assert values["battery_soc"] == 100
    assert values["exp_1_soc"] == 90


def test_named_values_reach_the_summary() -> None:
    device = A1783()
    summary = device.mcu.summary
    assert summary is not None

    device.set_values(
        pack_soc=555, pack_current_a=-3, cell_voltage=b"\x01\x02", output_power_total=86
    )

    post = summary.read(device.mcu.push(0x490, values=device.module.values).payload)
    assert post["pack_soc"] == 555
    assert post["pack_current_a"] == -3
    assert post["cell_voltage"] == b"\x01\x02".ljust(CELL_BYTES, b"\x00")
    assert post["output_power_total"] == 86


@pytest.mark.parametrize(
    ("name", "value"),
    [("cell_voltage", 5), ("pack_soc", b"\x01"), ("pack_soc", 1.5)],
)
def test_a_value_the_summary_field_cant_hold_is_refused(
    name: str, value: float | bytes
) -> None:
    device = A1783()

    with pytest.raises(TypeError):
        device.set_values(**{name: value})


def test_the_summary_refuses_names_and_values_it_doesnt_hold() -> None:
    script = A1783().mcu
    summary = script.summary
    assert summary is not None

    with pytest.raises(LayoutError):
        summary.check("no_such_field", 1)
    with pytest.raises(TypeError):
        summary.update(script.push(0x490).payload, {"pack_soc": b"\x01"})


def test_a_device_without_a_summary_refuses_its_names() -> None:
    with pytest.raises(LayoutError):
        A2345().set_values(pack_soc=1)


@pytest.mark.parametrize("model", [A1763, A1765, A1785])
def test_a_model_without_an_expansion_zeroes_the_expansion_entries(
    model: type[A1763 | A1765 | A1785],
) -> None:
    device = model()
    summary = device.mcu.summary
    assert summary is not None
    recorded = summary.read(A1783().mcu.push(0x490).payload)

    post = summary.read(device.mcu.push(0x490, values=device.module.values).payload)

    zeroed = {
        name: bytes(len(value)) if isinstance(value, bytes) else 0
        for name, value in recorded.items()
        if name.startswith("exp_1_")
    }
    assert post == {**recorded, **zeroed}
    assert recorded["exp_1_soc"] != 0


def test_the_a1783_keeps_its_recorded_expansion() -> None:
    device = A1783()

    assert device.module.values == {}
