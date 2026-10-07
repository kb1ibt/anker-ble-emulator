# Copyright (c) 2026 Shawn Stricker
"""Sphinx configuration; writes the layout coverage table from the packaged layouts."""

import json
from pathlib import Path


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
HEADER = ("Product", "Recorded", "Map", "Name", "Untyped", "Commands")


def coverage_rows() -> list[tuple[str, ...]]:
    """Return each layout's typed fields by where the type came from, and commands."""
    rows = []
    for path in sorted(MAPS.glob("*.json")):
        layout = json.loads(path.read_text())
        counts = dict.fromkeys(SOURCES, 0)
        for message in layout["messages"].values():
            for field in message["fields"]:
                counts[field.get("from", "untyped")] += 1
        commands = sum(len(group) for group in layout["commands"].values())
        rows.append((layout["pn"], *map(str, counts.values()), str(commands)))
    return rows


def simple_table(rows: list[tuple[str, ...]]) -> str:
    """Return ``rows`` under ``HEADER`` as an rst simple table."""
    widths = [max(len(row[i]) for row in (HEADER, *rows)) for i in range(len(HEADER))]
    rule = " ".join("=" * width for width in widths)

    def line(row: tuple[str, ...]) -> str:
        return " ".join(
            cell.ljust(width) for cell, width in zip(row, widths, strict=True)
        )

    return "\n".join([rule, line(HEADER), rule, *map(line, rows), rule, ""])


GENERATED.mkdir(exist_ok=True)
(GENERATED / "layout_coverage.rst").write_text(simple_table(coverage_rows()))
