# Copyright (c) 2026 Shawn Stricker
"""Build the live-value tables from the frame survey.

Reads each ``<PN>.json`` survey in the directory given and writes
``devices/data/<pn>_live.json``: per message, the groups, the active sets, the
totals that always held (``differ`` 0), the paired draws, the slow fields and
``remaining_time``'s split. Counters, the totals that didn't hold and every
field that identifies a unit are left out; what's written is numbers only. A
survey of a device the emulator doesn't have is refused, as is a table that
draws a field the device's layout declares it derives (``derived``).

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
from anker_ble_emulator.live import REMAINING_TIME
from anker_ble_emulator.products import Product


DATA = Path(__file__).parents[1] / "src" / "anker_ble_emulator" / "devices" / "data"
#: The match values the emulator gives a frame whose ``a4`` has no byte 31,
#: from firmware without Battery-Only Power (a1783-telemetry.md:153, :553): a
#: modelling choice that takes that firmware's AC behaviour as the switch off.
SHORT_A4 = {"ac_input_disable_switch": 0}
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
    Battery-Only Power; the emulator lets its samples draw only at the
    ``SHORT_A4`` value of each match key (a modelling choice, not a recorded
    switch state). A tuple a long-``a4`` sample also holds adds its count.

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
    unknown = [key for key in group.get("match", []) if key not in SHORT_A4]
    if unknown:
        msg = f"{name}: no short-a4 value for {', '.join(unknown)}"
        raise SurveyError(msg)
    tail = [SHORT_A4[key] for key in group.get("match", [])]
    counts: dict[tuple[Any, ...], int] = {}
    for sample, count in [
        *group["samples"],
        *([[*values, *tail], count] for values, count in short),
    ]:
        counts[tuple(sample)] = counts.get(tuple(sample), 0) + count
    return {
        **group,
        "samples": [[list(sample), count] for sample, count in counts.items()],
    }


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
) -> dict[str, Any] | None:
    """Return a survey section's table; None if it carries nothing live.

    Args:
        survey: The survey's section.
        carried: Match keys to add to a group of each name that has none.
        derived: Fields the layout derives: their slow entries are left out.

    """
    groups = [
        _group(
            _with_carried(_with_short_a4(group), (carried or {}).get(group["name"], {}))
        )
        for group in survey.get("groups", [])
        if not any(identifies(f) for f in (*group["fields"], *group.get("match", [])))
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
            survey.get("active_sets", []), {group["name"] for group in groups}
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

    A slow field the layout derives is left out, with a note on stderr: the
    layout works it out from the field it follows.

    Raises:
        SurveyError: If the device isn't emulated, the table otherwise draws a
            field its layout derives, or a carried match key has more than one
            value.

    """
    device = survey["device"]
    derived = derived_by(device)
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
    sections = [section(part, carried, derived) for part in survey["sections"]]
    built = {
        "device": device,
        "sections": [part for part in sections if part is not None],
    }
    refused = sorted(drawn(built) & derived)
    if refused:
        msg = f"{device}: the layout derives drawn fields {refused}"
        raise SurveyError(msg)
    return built


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
