# Copyright (c) 2026 Shawn Stricker
"""The live-value table builder."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from anker_ble_emulator import A1783
from anker_ble_emulator.devices import PROFILES
from anker_ble_emulator.devices.solix_c_gen2 import DC_INPUT_SOURCE, DcInputSource
from anker_ble_emulator.live import ANY, LiveTable
from anker_ble_emulator.parameters import TableLayout
from anker_ble_emulator.products import Product
from tests.fixtures.live import SURVEY
from tools.build_live_tables import (
    EMITTED_SCHEMA,
    SurveyError,
    identifies,
    main,
    table,
)


if TYPE_CHECKING:
    from pathlib import Path

#: The DC input's group as the frame survey writes it.
PV1 = {
    "name": "pv1",
    "fields": ["pv1_charge_status", "pv1_input_power"],
    "live": ["pv1_input_power"],
    "idle": [0],
    "samples": [[[1, 125], 7]],
}


def test_a_table_keeps_the_groups_active_sets_and_held_totals() -> None:
    built = table(SURVEY)

    (section,) = built["sections"]
    assert built["device"] == "A1783"
    assert section["msgtype"] == 0x421
    assert [group["name"] for group in section["groups"]] == ["ac_out", "usbc"]
    assert section["groups"][0] == {
        "name": "ac_out",
        "fields": ["ac_output_switch", "ac_output_power"],
        "match": ["ac_input_status"],
        "live": ["ac_output_power"],
        "idle": [0],
        "samples": [[[1, 100, 1], 7], [[1, 0, 1], 2]],
    }
    assert section["groups"][1]["match"] == []
    assert section["active_sets"] == [[[], 4], [["ac_out"], 4], [["usbc"], 3]]
    assert section["totals"] == [
        {"field": "total_output_power", "sum_of": ["ac_output_power", "usbc_power"]}
    ]


def test_a_table_keeps_the_slow_paired_and_remaining_time_and_drops_counters() -> None:
    (section,) = table(SURVEY)["sections"]

    assert section["slow"] == [
        {
            "field": "pack_soc",
            "interval": 540,
            "steps": [[-1, 2], [0, 90], [1, 3]],
            "values": [[400, 10], [450, 1]],
        }
    ]
    assert section["paired"] == SURVEY["sections"][0]["paired"]
    assert section["remaining_time"] == SURVEY["sections"][0]["remaining_time"]
    assert "counters" not in section
    assert "charge_cycles" not in json.dumps(section)


def test_a_built_table_loads() -> None:
    names = LiveTable.from_json(table(SURVEY)).names

    assert "pd1_device_vid" not in names
    assert {"ac_input_status", "ac_output_power", "pack_current"} <= names


@pytest.mark.parametrize(
    "name",
    [
        "ac_output_power",
        "pack_current",
        "total_output_power",
        "remaining_time",
    ],
)
def test_a_table_drawing_a_field_the_layout_derives_is_refused(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = TableLayout()
    layout.derived = frozenset({name})
    monkeypatch.setitem(
        PROFILES, Product.A1783, replace(PROFILES[Product.A1783], layout=layout)
    )

    with pytest.raises(SurveyError, match=rf"derives drawn fields \['{name}'\]"):
        table(SURVEY)


def test_a_slow_field_the_layout_derives_is_skipped_with_a_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = TableLayout()
    layout.derived = frozenset({"pack_soc"})
    monkeypatch.setitem(
        PROFILES, Product.A1783, replace(PROFILES[Product.A1783], layout=layout)
    )

    (section,) = table(SURVEY)["sections"]

    assert section["slow"] == []
    assert capsys.readouterr().err == (
        "A1783: skipped the derived slow fields ['pack_soc']\n"
    )


def test_a_mirrors_key_is_ignored() -> None:
    """A mirror is one state value in the emulator: the table carries nothing of it."""
    survey = copy.deepcopy(SURVEY)
    survey["sections"][0]["mirrors"] = [["ac_output_power", "ac_power_mirror"]]
    survey["sections"][0]["groups"][0]["mirrors"] = {"ac_output_power": ["x"]}

    assert table(survey) == table(SURVEY)


def test_a_short_a4_sample_matches_with_battery_only_off() -> None:
    """The emulator draws samples from firmware with no ``a4[31]`` at switch 0."""
    survey = copy.deepcopy(SURVEY)
    ac = survey["sections"][0]["groups"][0]
    ac["match"] = ["ac_input_disable_switch"]
    ac["samples"] = [[[1, 100, 1], 7], [[1, 0, 0], 2]]
    ac["samples_short_a4"] = [[[1, 100], 3], [[1, 50], 1]]
    ac["dropped_short_a4"] = 2
    ac["short_a4"] = {"a4_len": [[26, 6]], "frames": 6, "other_missing": 0}

    (section,) = table(survey)["sections"]

    assert section["groups"][0]["samples"] == [
        [[1, 100, 1], 7],
        [[1, 0, 0], 2],
        [[1, 100, 0], 3],
        [[1, 50, 0], 1],
    ]


def test_short_a4_samples_merge_with_long_ones_they_equal() -> None:
    survey = copy.deepcopy(SURVEY)
    ac = survey["sections"][0]["groups"][0]
    ac["match"] = ["ac_input_disable_switch"]
    ac["samples"] = [[[1, 100, 0], 7]]
    ac["samples_short_a4"] = [[[1, 100], 3]]
    ac["short_a4"] = {"a4_len": [[26, 3]], "frames": 3, "other_missing": 0}

    (section,) = table(survey)["sections"]

    assert section["groups"][0]["samples"] == [[[1, 100, 0], 10]]


def test_a_short_a4_sample_keeps_the_match_keys_it_carries() -> None:
    """A 26 B ``a4`` lacks only byte 31: the limit it carries stays as recorded."""
    survey = copy.deepcopy(SURVEY)
    ac = survey["sections"][0]["groups"][0]
    ac["match"] = ["ac_input_disable_switch", "ac_recharge_power"]
    ac["samples"] = [[[1, 100, 1, 1200], 7]]
    ac["samples_short_a4"] = [[[1, 100, 1000], 3], [[1, 100, 1200], 1]]
    ac["match_short_a4"] = ["ac_recharge_power"]
    ac["short_a4"] = {"a4_len": [[26, 4]], "frames": 4, "other_missing": 0}

    (section,) = table(survey)["sections"]

    assert section["groups"][0]["samples"] == [
        [[1, 100, 1, 1200], 7],
        [[1, 100, 0, 1000], 3],
        [[1, 100, 0, 1200], 1],
    ]


def test_a_sample_with_no_ac_input_matches_any_charge_limit() -> None:
    """The charge limits govern AC input, so an AC-input-0 sample takes ``ANY``."""
    survey = copy.deepcopy(SURVEY)
    ac = survey["sections"][0]["groups"][0]
    ac["fields"] = ["ac_output_power", "ac_input_power"]
    ac["live"] = ["ac_output_power", "ac_input_power"]
    ac["idle"] = [0, 0]
    ac["match"] = ["ac_recharge_power", "ac_input_status"]
    ac["samples"] = [
        [[100, 0, 1200, 1], 3],
        [[100, 0, 1000, 1], 2],
        [[0, 1210, 1200, 1], 4],
    ]

    (section,) = table(survey)["sections"]

    assert section["groups"][0]["samples"] == [
        [[100, 0, ANY, 1], 5],
        [[0, 1210, 1200, 1], 4],
    ]


def test_pv1_samples_take_the_dc_source_of_the_era_their_days_fall_in() -> None:
    """Car socket from 2026-08-08; no input (status 0) matches either source.

    Each frame stands for 2 s, and pv1 draws on its own, out of the active sets.
    """
    survey = copy.deepcopy(SURVEY)
    part = survey["sections"][0]
    part["groups"].append(
        {**PV1, "samples": [[[0, 0], 90], [[1, 125], 7], [[1, 0], 2]], "dropped": 1}
    )
    part["active_sets"].append([["usbc", "pv1"], 2])
    part["a8_by_day_local"] = [
        ["2026-07-21", 0, 0, 40],
        ["2026-08-13", 0, 0, 50],
        ["2026-08-13", 1, 125, 7],
        ["2026-08-14", 1, 0, 2],
        ["2026-08-14", 1, 16, 1],
    ]

    (section,) = table(survey)["sections"]

    pv1 = section["groups"][-1]
    assert (pv1["match"], pv1["independent"]) == ([DC_INPUT_SOURCE], True)
    assert pv1["samples"] == [
        [[0, 0, ANY], 180],
        [[1, 125, DcInputSource.CAR_SOCKET], 14],
        [[1, 0, DcInputSource.CAR_SOCKET], 4],
    ]
    assert section["active_sets"] == [[[], 4], [["ac_out"], 4], [["usbc"], 5]]


@pytest.mark.parametrize(
    ("by_day", "error"),
    [
        pytest.param(None, "pv1: samples with no days", id="no-days"),
        pytest.param(
            [["2026-08-01", 1, 125, 3], ["2026-08-13", 1, 125, 4]],
            r"pv1: \[1, 125\] was recorded either side of the era boundary, 2026-08-08",
            id="both-eras",
        ),
        pytest.param(
            [["2026-08-13", 1, 126, 7]],
            r"pv1: \[1, 125\] has no recorded day",
            id="undated",
        ),
    ],
)
def test_pv1_samples_the_builder_cant_date_are_refused(
    by_day: list[list[object]] | None, error: str
) -> None:
    survey = copy.deepcopy(SURVEY)
    part = survey["sections"][0]
    part["groups"].append(copy.deepcopy(PV1))
    if by_day is not None:
        part["a8_by_day_local"] = by_day

    with pytest.raises(SurveyError, match=error):
        table(survey)


def test_solar_samples_come_from_the_0005_posts_of_days_the_panels_were_in() -> None:
    """A post weighs 540 s, status 1 at 0 W too (night with the panels in).

    A day the frames cover keeps the frames; a day with no post above 0 W had
    nothing plugged in (the device's status-0 frames that week), so no samples.
    """
    survey = copy.deepcopy(SURVEY)
    part = survey["sections"][0]
    part["groups"].append(copy.deepcopy(PV1))
    part["a8_by_day_local"] = [["2026-08-13", 1, 125, 7]]
    survey["sections"].append(
        {
            "msgtype": "490",
            "schema": "_0005",
            "frames": 18,
            "dc_in_by_day_local": [
                ["2026-07-24", 0, 5],
                ["2026-08-03", 0, 2],
                ["2026-08-03", 120, 1],
                ["2026-08-04", 0, 1],
                ["2026-08-04", 120, 2],
                ["2026-08-13", 125, 4],
                ["2026-08-20", 0, 3],
            ],
        }
    )

    pv1 = table(survey)["sections"][0]["groups"][-1]

    assert pv1["samples"] == [
        [[1, 125, DcInputSource.CAR_SOCKET], 14],
        [[1, 0, DcInputSource.SOLAR], 3 * 540],
        [[1, 120, DcInputSource.SOLAR], 3 * 540],
    ]


def test_a_0005_post_on_a_car_socket_day_the_frames_dont_cover_is_refused() -> None:
    survey = copy.deepcopy(SURVEY)
    part = survey["sections"][0]
    part["groups"].append(copy.deepcopy(PV1))
    part["a8_by_day_local"] = [["2026-08-13", 1, 125, 7]]
    survey["sections"].append(
        {
            "msgtype": "490",
            "schema": "_0005",
            "dc_in_by_day_local": [["2026-08-20", 126, 2]],
        }
    )

    with pytest.raises(
        SurveyError, match="pv1: _0005 posts on 2026-08-20, a car-socket day"
    ):
        table(survey)


def test_a_named_section_is_skipped_with_a_note(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ac_charge`` is a joint table over the AC limits, not a live source."""
    survey = copy.deepcopy(SURVEY)
    survey["sections"].append(
        {
            "msgtype": "421/900",
            "section": "ac_charge",
            "columns": ["ac_recharge_power", "ac_input_power"],
            "frames": 3,
            "joint": [[[1200, 1100], 3]],
        }
    )

    assert table(survey) == table(SURVEY)
    assert capsys.readouterr().err == "A1783: skipped the sections ['ac_charge']\n"


def test_a_0490_section_of_a_schema_the_emulator_doesnt_send_is_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    survey = copy.deepcopy(SURVEY)
    slow = survey["sections"][0]["slow"]
    survey["sections"] += [
        {"msgtype": "490", "schema": EMITTED_SCHEMA, "frames": 2, "slow": slow},
        {
            "msgtype": "490",
            "schema": "_0005",
            "frames": 2,
            "slow": [{**slow[0], "field": "dc_input_power"}],
        },
    ]

    built = table(survey)

    assert [part["msgtype"] for part in built["sections"]] == [0x421, 0x490]
    assert [block["field"] for block in built["sections"][1]["slow"]] == ["pack_soc"]
    assert capsys.readouterr().err == "A1783: skipped the sections ['490 _0005']\n"


def test_the_0490_schema_kept_is_the_one_the_emulator_sends() -> None:
    push = A1783().mcu.push(0x490).payload

    assert push.endswith(f"charging_pps_series_c{EMITTED_SCHEMA}\x00".encode())


@pytest.mark.parametrize(
    ("match", "other_missing", "error"),
    [
        (["ac_input_disable_switch"], 4, "other_missing"),
        (["ac_input_status"], 0, "no short-a4 value for ac_input_status"),
    ],
)
def test_short_a4_samples_the_builder_cant_place_are_refused(
    match: list[str], other_missing: int, error: str
) -> None:
    survey = copy.deepcopy(SURVEY)
    ac = survey["sections"][0]["groups"][0]
    ac["match"] = match
    ac["samples_short_a4"] = [[[1, 100], 3]]
    ac["short_a4"] = {"a4_len": [[26, 3]], "frames": 7, "other_missing": other_missing}

    with pytest.raises(SurveyError, match=error):
        table(survey)


def test_a_section_of_two_messages_takes_the_first_as_its_label() -> None:
    survey = copy.deepcopy(SURVEY)
    survey["sections"][0]["msgtype"] = "421/900"

    assert table(survey)["sections"][0]["msgtype"] == 0x421


def test_a_slow_block_with_no_interval_is_refused() -> None:
    survey = copy.deepcopy(SURVEY)
    del survey["sections"][0]["slow"][0]["interval"]

    with pytest.raises(SurveyError, match="pack_soc: a slow block with no interval"):
        table(survey)


def test_a_total_with_no_parts_is_refused() -> None:
    survey = copy.deepcopy(SURVEY)
    survey["sections"][0]["totals"].append(
        {"field": "battery_level", "sum_of": None, "equal": 10, "differ": 0}
    )

    with pytest.raises(SurveyError, match="battery_level: a total with no sum_of"):
        table(survey)


def test_a_total_that_didnt_always_hold_is_left_out() -> None:
    (section,) = table(SURVEY)["sections"]

    assert "total_input_power" not in {total["field"] for total in section["totals"]}
    assert [total["field"] for total in section["totals"]] == ["total_output_power"]


def test_a_derived_field_a_group_only_matches_on_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = TableLayout()
    layout.derived = frozenset({"ac_input_status"})
    monkeypatch.setitem(
        PROFILES, Product.A1783, replace(PROFILES[Product.A1783], layout=layout)
    )

    assert table(SURVEY)["sections"][0]["groups"][0]["match"] == ["ac_input_status"]


def test_a_match_key_recorded_at_one_value_carries_to_its_group_elsewhere() -> None:
    """``usbc`` matches a relay only another message carries, at its one value."""
    survey = copy.deepcopy(SURVEY)
    survey["sections"][1]["groups"] = [
        {
            "name": "usbc",
            "fields": ["usbc_power"],
            "match": ["usbc_relay_switch"],
            "match_values": {"usbc_relay_switch": [[1, 4]]},
            "live": ["usbc_power"],
            "idle": [0],
            "samples": [[[30, 1], 4]],
        }
    ]
    survey["sections"][1]["active_sets"] = [[["usbc"], 4]]

    first, second = table(survey)["sections"]

    usbc = first["groups"][1]
    assert (usbc["match"], usbc["samples"]) == (["usbc_relay_switch"], [[[30, 1], 1]])
    assert first["groups"][0]["match"] == ["ac_input_status"]
    assert second["groups"][0]["samples"] == [[[30, 1], 4]]


def test_a_match_key_of_several_values_its_own_frames_carry_builds() -> None:
    """``ac_input_status`` is in the group's own frames: nothing carries it."""
    survey = copy.deepcopy(SURVEY)
    survey["sections"][0]["groups"][0]["match_values"] = {
        "ac_input_status": [[1, 7], [2, 2]]
    }

    (section,) = table(survey)["sections"]

    assert section["groups"][0]["match"] == ["ac_input_status"]


def test_a_carried_match_key_recorded_at_two_values_is_refused() -> None:
    survey = copy.deepcopy(SURVEY)
    survey["sections"][1]["groups"] = [
        {
            "name": "usbc",
            "fields": ["usbc_power"],
            "match": ["usbc_relay_switch"],
            "match_values": {"usbc_relay_switch": [[1, 3], [0, 1]]},
            "live": ["usbc_power"],
            "idle": [0],
            "samples": [[[30, 1], 3], [[0, 0], 1]],
        }
    ]

    with pytest.raises(SurveyError, match="usbc_relay_switch was recorded at"):
        table(survey)


def test_main_builds_only_the_devices_named(tmp_path: Path) -> None:
    surveys, out = tmp_path / "groups", tmp_path / "data"
    surveys.mkdir()
    out.mkdir()
    (surveys / "A1783.json").write_text(json.dumps(SURVEY))
    (surveys / "A1722.json").write_text(json.dumps({**SURVEY, "device": "A1722"}))

    status = main([str(surveys), "--output", str(out), "--only", "A1722"])

    assert status == 0
    assert [path.name for path in out.iterdir()] == ["a1722_live.json"]


def test_a_device_with_no_layout_derives_nothing() -> None:
    survey = {**SURVEY, "device": "A1722"}

    assert table(survey)["device"] == "A1722"


def test_a_survey_of_a_device_not_emulated_is_refused() -> None:
    with pytest.raises(SurveyError, match="A9999: no emulated device"):
        table({**SURVEY, "device": "A9999"})


@pytest.mark.parametrize(
    ("name", "identifying"),
    [
        ("pd1_device_vid", True),
        ("device_sn", True),
        ("serial", True),
        ("wifi_mac", True),
        ("ac_output_power", False),
        ("screen_saver_theme_id", False),
    ],
)
def test_identifier_fields_are_recognised(name: str, identifying: bool) -> None:  # noqa: FBT001  # the expected answer
    assert identifies(name) is identifying


@pytest.mark.parametrize(
    "where", ["idle", "samples", "match", "steps", "remaining", "paired"]
)
def test_a_value_that_isnt_a_number_is_refused(where: str) -> None:
    survey = copy.deepcopy(SURVEY)
    part = survey["sections"][0]
    bad = {
        "idle": lambda: part["groups"][0].update(idle=["0"]),
        "samples": lambda: part["groups"][0]["samples"].append([[1, True], 1]),
        "match": lambda: part["groups"][0]["samples"].append([[1, 100, None], 1]),
        "steps": lambda: part["slow"][0]["steps"].append([None, 1]),
        "remaining": lambda: part["remaining_time"]["output_idle"].append(["x", 1]),
        "paired": lambda: part["paired"][0]["samples"].append([[["a"], [1]], 1]),
    }
    bad[where]()

    with pytest.raises(SurveyError, match="isn't a number"):
        table(survey)


def test_main_writes_each_devices_table(tmp_path: Path) -> None:
    surveys, out = tmp_path / "groups", tmp_path / "data"
    surveys.mkdir()
    out.mkdir()
    (surveys / "A1783.json").write_text(json.dumps(SURVEY))

    status = main([str(surveys), "--output", str(out)])

    assert status == 0
    assert json.loads((out / "a1783_live.json").read_text()) == table(SURVEY)


def test_main_with_no_surveys_fails(tmp_path: Path) -> None:
    assert main([str(tmp_path)]) == 1
