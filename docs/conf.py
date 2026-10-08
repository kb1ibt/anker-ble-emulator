# Copyright (c) 2026 Shawn Stricker
"""Sphinx configuration; writes the coverage tables from the package's profiles."""

import json
from pathlib import Path

from anker_ble_emulator.devices import PROFILES
from anker_ble_emulator.layouts import Layout
from anker_ble_emulator.products import Product


project = "anker-ble-emulator"
author = "Shawn Stricker"
extensions = ["myst_parser"]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build", "_generated"]
html_theme = "alabaster"

DOCS = Path(__file__).parent
MAPS = DOCS.parent / "src" / "anker_ble_emulator" / "maps"
GENERATED = DOCS / "_generated"
SOURCES = ("recorded", "map", "name", "untyped")
LAYOUT_HEADER = ("Product", "Recorded", "Map", "Name", "Untyped", "Commands")
#: The emulated products, in the compatibility table's column order.
EMULATED = (
    Product.A1763,
    Product.A1765,
    Product.A1783,
    Product.A1785,
    Product.A2345,
    Product.A91B2,
)
COMMAND_ROWS = (
    "MCU commands known",
    "Answered",
    "Answered from recordings",
    "Answered by a map ack",
    "Values checked",
    "Change telemetry",
    "Telemetry they change",
    "Telemetry fields typed",
    "Module commands answered",
)
#: The module answers its version read on every build.
BUILT_SESSION_OPS = frozenset({0x030})


def map_fields(pn: str) -> list[dict[str, str]]:
    """Return every telemetry field of a product's layout, typed or not."""
    layout = json.loads((MAPS / f"{pn.lower()}.json").read_text())
    return [field for spec in layout["messages"].values() for field in spec["fields"]]


def layout_rows() -> list[tuple[str, ...]]:
    """Return each layout's typed fields by where the type came from, and commands."""
    rows = []
    for path in sorted(MAPS.glob("*.json")):
        layout = json.loads(path.read_text())
        counts = dict.fromkeys(SOURCES, 0)
        for field in map_fields(layout["pn"]):
            counts[field.get("from", "untyped")] += 1
        commands = sum(len(group) for group in layout["commands"].values())
        rows.append((layout["pn"], *map(str, counts.values()), str(commands)))
    return rows


def share(part: int, whole: int) -> str:
    """Return ``part`` of ``whole`` as a percentage with its counts."""
    return f"{round(100 * part / whole)} % ({part}/{whole})" if whole else "N/A"


def shown_in(layout: Layout, msgtype: int) -> set[int]:
    """Return the telemetry messages an accepted ``msgtype`` command changes."""
    return {
        target
        for command in layout.commands[msgtype]
        for spec in command.values()
        if spec.get("state")
        for target in layout.locate(spec["state"])
    }


def command_column(pn: Product) -> list[str]:
    """Return one product's command and telemetry coverage, row by row."""
    profile = PROFILES[pn]
    layout = Layout.load(pn)
    if layout is None:
        msg = f"{pn} has no layout"
        raise ValueError(msg)
    mapped = set(layout.commands)
    recorded = set(profile.replies)
    known = set(profile.known_commands) | mapped | recorded
    changes = {msgtype: shown_in(layout, msgtype) for msgtype in mapped}
    shown = {msgtype for msgtype, targets in changes.items() if targets}
    targets = sorted(set().union(*changes.values()))
    typed = sum(len(fields) for fields in layout.messages.values())
    build = profile.module_build
    module_ops = build.session_ops
    module_answered = set(profile.session_replies(build)) | BUILT_SESSION_OPS
    return [
        str(len(known)),
        share(len(known & (recorded | mapped)), len(known)),
        share(len(known & recorded), len(known)),
        share(len(known & (mapped - recorded)), len(known)),
        share(len(known & mapped), len(known)),
        share(len(known & shown), len(known)),
        ", ".join(f"``{target:04x}``" for target in targets) or "none",
        share(typed, len(map_fields(pn))),
        share(len(module_ops & module_answered), len(module_ops)),
    ]


def grid_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """Return ``rows`` under ``header`` as an rst grid table."""
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]
    rule = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def line(row: tuple[str, ...]) -> str:
        cells = (cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        return "| " + " | ".join(cells) + " |"

    body = [f"{line(row)}\n{rule}" for row in rows]
    return "\n".join([rule, line(header), rule.replace("-", "="), *body, ""])


def command_table() -> str:
    """Return the emulated products' command coverage, products as columns."""
    columns = [command_column(pn) for pn in EMULATED]
    rows = [
        (label, *(column[index] for column in columns))
        for index, label in enumerate(COMMAND_ROWS)
    ]
    return grid_table(("", *map(str, EMULATED)), rows)


GENERATED.mkdir(exist_ok=True)
(GENERATED / "layout_coverage.rst").write_text(grid_table(LAYOUT_HEADER, layout_rows()))
(GENERATED / "command_coverage.rst").write_text(command_table())
