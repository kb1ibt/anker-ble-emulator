# Copyright (c) 2026 Shawn Stricker
"""Build the live-value tables from the frame survey.

Reads each ``<PN>.json`` survey in the directory given and writes
``devices/data/<pn>_live.json``: per message, the groups, the active sets, the
totals that always held (``differ`` 0), the paired draws, the slow fields and
``remaining_time``'s split. Counters, the totals that didn't hold and every
field that identifies a unit are left out; what's written is numbers only. A
named section (a joint table) and a ``0490`` section of a schema the emulator
doesn't send build nothing. A survey of a device the emulator doesn't have is
refused, as is a table that draws a field the device's layout declares it
derives (``derived``).

Usage::

    uv run python tools/build_live_tables.py SURVEY_DIR [--output DIR] [--only PN ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from anker_ble_emulator.devices import PROFILES
from anker_ble_emulator.devices.solix_c_gen2 import DC_INPUT_SOURCE, DcInputSource
from anker_ble_emulator.live import ANY, REMAINING_TIME
from anker_ble_emulator.products import Product


DATA = Path(__file__).parents[1] / "src" / "anker_ble_emulator" / "devices" / "data"
#: The match values the emulator gives a frame whose ``a4`` has no byte 31,
#: from firmware without Battery-Only Power (a1783-telemetry.md:153, :553): a
#: modelling choice that takes that firmware's AC behaviour as the switch off.
SHORT_A4 = {"ac_input_disable_switch": 0}
#: Match keys that bind a sample only while its field is drawn above idle: the
#: AC charge limits govern the AC input, not the AC output.
INPUT_ONLY = {
    "ac_input_power": (
        "ac_recharge_power",
        "silent_recharge_power",
        "super_fast_charge_status",
    )
}
#: The DC input's group, its fields, and its source by day: the owner's era
#: table for the XT-60i, 2 x PS400 solar before this day, the car-socket
#: adapter from it.
DC_INPUT_GROUP = "pv1"
DC_STATUS = "pv1_charge_status"
DC_POWER = "pv1_input_power"
CAR_SOCKET_FROM = "2026-08-08"
#: Seconds a ``0421``/``0900`` frame stands for: the collector's poll.
FRAME_SECONDS = 2
#: The ``0490`` schema whose ``.23.5`` posts (the DC input's register, as
#: ``a8``) give the solar samples, and the seconds a post stands for: the push
#: interval, longer gaps being missing posts.
DC_POSTS_SCHEMA = "_0005"
POST_SECONDS = 540
#: Groups that draw on their own, from their samples given their match keys
#: (an input plugged in or not), not from the section's active sets.
INDEPENDENT = frozenset({DC_INPUT_GROUP})
#: The ``0490`` schema the emulator's recorded frames carry: another schema's
#: section isn't a source.
EMITTED_SCHEMA = "_0009"
#: Field names that identify a unit, never written.
IDENTIFIER = re.compile(r"(^|_)(sn|serial|mac|vid|pid|token)(_|$)")


class SurveyError(ValueError):
    """The survey holds something a table can't carry."""


def identifies(name: str) -> bool:
    """Whether a field identifies a unit."""
    return IDENTIFIER.search(name) is not None


def _numbers(field: str, values: list[Any]) -> list[Any]:
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            msg = f"{field}: {value!r} isn't a number"
            raise SurveyError(msg)
    return values


def _group(group: dict[str, Any]) -> dict[str, Any]:
    name = group["name"]
    return {
        "name": name,
        "fields": group["fields"],
        "match": group.get("match", []),
        "live": group["live"],
        "idle": _numbers(name, group["idle"]),
        "samples": [
            [_numbers(name, sample), count] for sample, count in group["samples"]
        ],
    }


def _paired(block: dict[str, Any]) -> dict[str, Any]:
    name = ",".join(block["fields"])
    return {
        "given": block["given"],
        "fields": block["fields"],
        "samples": [
            [[_numbers(name, given), _numbers(name, values)], count]
            for (given, values), count in block["samples"]
        ],
        "unpaired": block["unpaired"],
    }


def _slow(block: dict[str, Any]) -> dict[str, Any]:
    """Return a slow block's table: its steps, range and the median interval.

    Raises:
        SurveyError: If the block has no interval.

    """
    field = block["field"]
    interval = block.get("interval")
    if interval is None:
        msg = f"{field}: a slow block with no interval"
        raise SurveyError(msg)
    return {
        "field": field,
        "interval": _numbers(field, [interval["median"]])[0],
        "steps": [
            [_numbers(field, [step])[0], count] for step, count in block["steps"]
        ],
        "values": [
            [_numbers(field, [value])[0], count] for value, count in block["values"]
        ],
    }


def _active_sets(sets: list[Any], kept: set[str]) -> list[list[Any]]:
    """Return the active sets over the kept groups, a set left twice counted once."""
    counts: dict[tuple[str, ...], int] = {}
    for names, count in sets:
        key = tuple(name for name in names if name in kept)
        counts[key] = counts.get(key, 0) + count
    return [[list(names), count] for names, count in counts.items()]


def _msgtype(value: int | str) -> int:
    """Return a section's msgtype: for messages sharing a TLV (``421/900``), the first.

    It only labels the section: draws go by field name.
    """
    return value if isinstance(value, int) else int(value.split("/")[0], 16)


def carried_matches(survey: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return each group's match keys with their recorded values.

    A group of that name in another message, whose frame carries no such key
    (the Prime relay switches, in ``0a00`` only), matches on them too.
    """
    carried: dict[str, dict[str, Any]] = {}
    for part in survey["sections"]:
        for group in part.get("groups", []):
            for name, values in group.get("match_values", {}).items():
                carried.setdefault(group["name"], {})[name] = values
    return carried


def _with_short_a4(group: dict[str, Any]) -> dict[str, Any]:
    """Return ``group`` with its short-``a4`` samples merged into its samples.

    A frame whose ``a4`` has no byte 31 comes from firmware without
    Battery-Only Power. Its samples carry the fields and the match keys in
    ``match_short_a4``; each other match key takes its ``SHORT_A4`` value (a
    modelling choice, not a recorded switch state). A tuple a long-``a4``
    sample also holds adds its count.

    Raises:
        SurveyError: If short frames sit in neither sample list, or a match
            key has no short-``a4`` value.

    """
    short = group.get("samples_short_a4")
    if short is None:
        return group
    name = group["name"]
    missing = group["short_a4"]["other_missing"]
    if missing:
        msg = f"{name}: {missing} short-a4 frames in no samples (other_missing)"
        raise SurveyError(msg)
    match = group.get("match", [])
    carried = group.get("match_short_a4", [])
    unknown = [key for key in match if key not in {*SHORT_A4, *carried}]
    if unknown:
        msg = f"{name}: no short-a4 value for {', '.join(unknown)}"
        raise SurveyError(msg)
    width = len(group["fields"])

    def full(values: list[Any]) -> list[Any]:
        known = {**SHORT_A4, **dict(zip(carried, values[width:], strict=True))}
        return [*values[:width], *(known[key] for key in match)]

    samples = [*group["samples"], *([full(values), c] for values, c in short)]
    return {**group, "samples": _merged(samples)}


def _merged(samples: list[Any]) -> list[list[Any]]:
    """Return ``samples`` with equal tuples merged, their counts added."""
    counts: dict[tuple[Any, ...], int] = {}
    for sample, count in samples:
        counts[tuple(sample)] = counts.get(tuple(sample), 0) + count
    return [[list(sample), count] for sample, count in counts.items()]


def _with_wildcards(group: dict[str, Any]) -> dict[str, Any]:
    """Return ``group`` with each ``INPUT_ONLY`` key at ``ANY`` where its field idles.

    Such samples merge, their counts added.
    """
    columns = [*group["fields"], *group["match"]]
    samples = []
    for sample, count in group["samples"]:
        cells = list(sample)
        for field, keys in INPUT_ONLY.items():
            if field not in group["live"]:
                continue
            idle = group["idle"][group["live"].index(field)]
            if sample[columns.index(field)] == idle:
                for key in set(keys) & set(columns):
                    cells[columns.index(key)] = ANY
        samples.append([cells, count])
    return {**group, "samples": _merged(samples)}


def _with_dc_source(
    group: dict[str, Any], by_day: list[list[Any]] | None, posts: list[list[Any]]
) -> dict[str, Any]:
    """Return the DC input's group matching on its source, a frame ``FRAME_SECONDS``.

    A plugged-in sample (status 1) takes the source of the era its days fall
    in; one with nothing plugged in matches either. The solar posts join it.

    Raises:
        SurveyError: If there are no days, or a plugged-in sample has none or
            has days on both sides of ``CAR_SOCKET_FROM``.

    """
    name = group["name"]
    if by_day is None:
        msg = f"{name}: samples with no days"
        raise SurveyError(msg)
    days: dict[tuple[Any, ...], set[str]] = {}
    for day, status, power, _ in by_day:
        days.setdefault((status, power), set()).add(day)
    status_at = group["fields"].index(DC_STATUS)
    power_at = group["fields"].index(DC_POWER)
    samples = []
    for sample, count in group["samples"]:
        key = (sample[status_at], sample[power_at])
        source: Any = ANY
        if sample[status_at]:
            eras = {day >= CAR_SOCKET_FROM for day in days.get(key, ())}
            if not eras:
                msg = f"{name}: {list(key)} has no recorded day"
                raise SurveyError(msg)
            if len(eras) > 1:
                msg = f"{name}: {list(key)} was recorded either side of the era"
                msg += f" boundary, {CAR_SOCKET_FROM}"
                raise SurveyError(msg)
            source = int(
                DcInputSource.CAR_SOCKET if eras.pop() else DcInputSource.SOLAR
            )
        samples.append([[*sample, source], count * FRAME_SECONDS])
    frame_days = {row[0] for row in by_day}
    samples += _solar_posts(group, posts, frame_days)
    return {
        **group,
        "match": [*group["match"], DC_INPUT_SOURCE],
        "samples": _merged(samples),
    }


def _solar_posts(
    group: dict[str, Any], posts: list[list[Any]], frame_days: set[str]
) -> list[list[Any]]:
    """Return the DC input's solar samples from the posts, each ``POST_SECONDS``.

    A day the frames cover keeps the frames. A day with no post above 0 W had
    nothing plugged in (the device's own status-0 frames that July week), so
    it gives none. Every other post is status 1, 0 W included: night with the
    panels in, a modelling choice.

    Raises:
        SurveyError: If a day left falls in the car-socket era.

    """
    plugged = {day for day, power, _ in posts if power}
    samples = []
    for day, power, count in posts:
        if day in frame_days or day not in plugged:
            continue
        if day >= CAR_SOCKET_FROM:
            msg = f"{group['name']}: {DC_POSTS_SCHEMA} posts on {day}, a car-socket"
            msg += " day the frames don't cover"
            raise SurveyError(msg)
        values = {DC_STATUS: 1, DC_POWER: power}
        sample = [
            *(values[field] for field in group["fields"]),
            int(DcInputSource.SOLAR),
        ]
        samples.append([sample, count * POST_SECONDS])
    return samples


def _with_carried(group: dict[str, Any], carried: dict[str, Any]) -> dict[str, Any]:
    """Return ``group`` matching too on the carried keys its frame lacks.

    Each sample takes the key's one recorded value.

    Raises:
        SurveyError: If a key it takes was recorded at more than one value.

    """
    own = {*group["fields"], *group.get("match", [])}
    keys = [name for name in carried if name not in own]
    tail = []
    for name in keys:
        values = carried[name]
        if len(values) != 1:
            msg = f"{group['name']}: {name} was recorded at {values}"
            raise SurveyError(msg)
        tail.append(values[0][0])
    if not keys:
        return group
    return {
        **group,
        "match": [*group.get("match", []), *keys],
        "samples": [[[*sample, *tail], count] for sample, count in group["samples"]],
    }


def section(
    survey: dict[str, Any],
    carried: dict[str, dict[str, Any]] | None = None,
    derived: frozenset[str] = frozenset(),
    posts: list[list[Any]] | None = None,
) -> dict[str, Any] | None:
    """Return a survey section's table; None if it carries nothing live.

    Args:
        survey: The survey's section.
        carried: Match keys to add to a group of each name that has none.
        derived: Fields the layout derives: their slow entries are left out.
        posts: The DC input's posts by day, ``[day, W, posts]``.

    """
    groups = [
        _with_wildcards(
            _group(
                _with_carried(
                    _with_short_a4(group), (carried or {}).get(group["name"], {})
                )
            )
        )
        for group in survey.get("groups", [])
        if not any(identifies(f) for f in (*group["fields"], *group.get("match", [])))
    ]
    groups = [
        _with_dc_source(group, survey.get("a8_by_day_local"), posts or [])
        if group["name"] == DC_INPUT_GROUP
        else group
        for group in groups
    ]
    groups = [
        {**group, "independent": True} if group["name"] in INDEPENDENT else group
        for group in groups
    ]
    for total in survey.get("totals", []):
        if total["sum_of"] is None:
            msg = f"{total['field']}: a total with no sum_of"
            raise SurveyError(msg)
    totals = [
        {"field": total["field"], "sum_of": total["sum_of"]}
        for total in survey.get("totals", [])
        if total["differ"] == 0
        and not any(identifies(name) for name in (total["field"], *total["sum_of"]))
    ]
    paired = [
        _paired(block)
        for block in survey.get("paired", [])
        if not any(identifies(n) for n in (*block["given"], *block["fields"]))
    ]
    slow = [
        _slow(b)
        for b in survey.get("slow", [])
        if not identifies(b["field"]) and b["field"] not in derived
    ]
    remaining = survey.get("remaining_time")
    if not (groups or totals or paired or slow or remaining):
        return None
    table: dict[str, Any] = {
        "msgtype": _msgtype(survey["msgtype"]),
        "groups": groups,
        "active_sets": _active_sets(
            survey.get("active_sets", []),
            {group["name"] for group in groups if not group.get("independent")},
        ),
        "totals": totals,
        "paired": paired,
        "slow": slow,
    }
    if remaining is not None:
        table["remaining_time"] = {
            split: [
                [_numbers("remaining_time", [v])[0], c] for v, c in remaining[split]
            ]
            for split in ("output_idle", "output_active")
        }
    return table


def table(survey: dict[str, Any]) -> dict[str, Any]:
    """Return a device survey's table.

    A named section (a joint table such as ``ac_charge``) and a section of a
    schema the emulator doesn't send are left out, as is a slow field the
    layout derives (it works it out from the field it follows), each with a
    note on stderr.

    Raises:
        SurveyError: If the device isn't emulated, the table otherwise draws a
            field its layout derives, or a carried match key has more than one
            value.

    """
    device = survey["device"]
    derived = derived_by(device)
    posts = [
        row
        for part in survey["sections"]
        if part.get("schema") == DC_POSTS_SCHEMA
        for row in part.get("dc_in_by_day_local", [])
    ]
    left = [part for part in survey["sections"] if not _source(part)]
    if left:
        labels = [
            part.get("section") or f"{part['msgtype']} {part['schema']}"
            for part in left
        ]
        sys.stderr.write(f"{device}: skipped the sections {labels}\n")
    survey = {**survey, "sections": [p for p in survey["sections"] if _source(p)]}
    skipped = sorted(
        {
            block["field"]
            for part in survey["sections"]
            for block in part.get("slow", [])
            if block["field"] in derived
        }
    )
    if skipped:
        sys.stderr.write(f"{device}: skipped the derived slow fields {skipped}\n")
    carried = carried_matches(survey)
    sections = [section(part, carried, derived, posts) for part in survey["sections"]]
    built = {
        "device": device,
        "sections": [part for part in sections if part is not None],
    }
    refused = sorted(drawn(built) & derived)
    if refused:
        msg = f"{device}: the layout derives drawn fields {refused}"
        raise SurveyError(msg)
    return built


def _source(part: dict[str, Any]) -> bool:
    """Whether a survey section is a live source: unnamed, and of the emitted schema."""
    return part.get("section") is None and part.get("schema", EMITTED_SCHEMA) == (
        EMITTED_SCHEMA
    )


def derived_by(device: str) -> frozenset[str]:
    """Return the fields the device's layout declares it derives.

    Raises:
        SurveyError: If the device isn't emulated.

    """
    profile = PROFILES.get(Product(device)) if device in set(Product) else None
    if profile is None:
        msg = f"{device}: no emulated device"
        raise SurveyError(msg)
    return frozenset() if profile.layout is None else profile.layout.derived


def drawn(built: dict[str, Any]) -> set[str]:
    """Return every field the table's draws set."""
    names: set[str] = set()
    for part in built["sections"]:
        for group in part["groups"]:
            names.update(group["live"])
        for block in part["paired"]:
            names.update(block["fields"])
        names.update(total["field"] for total in part["totals"])
        names.update(block["field"] for block in part["slow"])
        if "remaining_time" in part:
            names.add(REMAINING_TIME)
    return names


def main(argv: list[str] | None = None) -> int:
    """Build a table for each survey; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("survey", type=Path)
    parser.add_argument("--output", type=Path, default=DATA)
    parser.add_argument("--only", nargs="+", metavar="PN")
    args = parser.parse_args(argv)
    surveys = sorted(
        path
        for path in args.survey.glob("*.json")
        if args.only is None or path.stem in args.only
    )
    if not surveys:
        sys.stderr.write(f"no surveys in {args.survey}\n")
        return 1
    for path in surveys:
        built = table(json.loads(path.read_text()))
        out = args.output / f"{built['device'].lower()}_live.json"
        out.write_text(json.dumps(built, separators=(",", ":")) + "\n")
        size = out.stat().st_size
        sys.stderr.write(
            f"wrote {out}: {len(built['sections'])} sections, {size:,} bytes\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
