# Copyright (c) 2026 Shawn Stricker
"""Write profiles for products a map and a SolixBLE class cover; the cross-check.

A product with a SolixBLE device class (``tools/solixble.json``), an
anker-solix-api map and the ``ff09`` transport, but no recorded profile, gets a
generated one in ``devices/generated/``: the class's outer, its status request,
the commands it sends, and telemetry built from the map's typed fields under
each message the class listens for. ``docs/solixble-crosscheck.rst`` gets every
mapped product's SolixBLE cross-check (``tools/solixble_cross.py``).

Usage::

    python -m tools.generate_profiles
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anker_ble_emulator.layouts import Layout
from anker_ble_emulator.products import PRODUCTS, Outer, Product, Transport
from tools.solixble_cross import FALLBACK, Check, Link, cross_check


if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).parent.parent
SOLIXBLE = ROOT / "tools" / "solixble.json"
DEVICES = ROOT / "src" / "anker_ble_emulator" / "devices"
REPORT = ROOT / "docs" / "solixble-crosscheck.rst"
HEADER = "# Copyright (c) 2026 Shawn Stricker\n"


@dataclass(frozen=True)
class Generated:
    """What a generated profile takes from the SolixBLE class and the map."""

    outer: Outer
    replies: dict[int, tuple[int, ...]]
    pushes: tuple[int, ...]
    built: tuple[int, ...]
    aliases: dict[int, int]
    known_commands: frozenset[int]


def generated(facts: Mapping[str, Any], layout: Layout) -> Generated:
    """Return a product's profile facts; messages the map can't type are left out."""

    def typed_by(msgtype: int) -> int | None:
        if msgtype in layout.messages:
            return msgtype
        fallback = FALLBACK.get(msgtype)
        return fallback if fallback in layout.messages else None

    pushes = tuple(
        msgtype
        for msgtype in (int(cmd, 16) for cmd in facts["telemetry"])
        if typed_by(msgtype) is not None
    )
    replies: dict[int, tuple[int, ...]] = {}
    built = list(pushes)
    if facts["status"] is not None:
        request, reply = (int(cmd, 16) for cmd in facts["status"])
        if typed_by(reply) is not None:
            replies[request] = (reply,)
            built.append(reply)
    aliases = {
        msgtype: source
        for msgtype in built
        if (source := typed_by(msgtype)) is not None and source != msgtype
    }
    return Generated(
        outer=Outer(facts["outer"]),
        replies=replies,
        pushes=pushes,
        built=tuple(built),
        aliases=aliases,
        known_commands=frozenset(
            int(command["cmd"], 16)
            for command in facts["commands"]
            if command["cmd"] is not None
        ),
    )


def _hex(msgtype: int) -> str:
    return f"0x{msgtype:03X}"


def _set(msgtypes: frozenset[int]) -> str:
    items = ", ".join(_hex(m) for m in sorted(msgtypes))
    return f"frozenset({{{items}}})" if msgtypes else "frozenset()"


def _tuple(msgtypes: tuple[int, ...]) -> str:
    items = ", ".join(_hex(m) for m in msgtypes)
    return f"({items},)" if len(msgtypes) == 1 else f"({items})"


def render(pn: Product, solixble_class: str, profile: Generated) -> str:
    """Return the source of a product's generated profile module."""
    name = PRODUCTS[pn].name
    replies = ", ".join(
        f"{_hex(request)}: {_tuple(replies)}"
        for request, replies in profile.replies.items()
    )
    aliases = ", ".join(f"{_hex(m)}: {_hex(s)}" for m, s in profile.aliases.items())
    serial = pn + "1".rjust(16 - len(pn), "0")
    fields = [
        f'serial="{serial}"',
        f"outer=Outer.{profile.outer.name}",
        "path=Path.ECDH",
        "module_build=ModuleBuild.UNRECORDED",
        "auth_mode=AuthMode.CONFIRM",
        "advert=Advert(local_name=None)",
        "data=()",
        f"replies={{{replies}}}",
        f"pushes={_tuple(profile.pushes)}",
        "device_version=None",
        f"known_commands={_set(profile.known_commands)}",
        f"layout_aliases={{{aliases}}}",
        f"built={_tuple(profile.built)}",
        "map_built=True",
    ]
    body = "".join(f"    {item},\n" for item in fields)
    return (
        f"{HEADER}"
        f'"""{name} ({pn}), from its map and SolixBLE\'s ``{solixble_class}``.\n\n'
        "Written by ``tools/generate_profiles.py``; nothing about it is recorded.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from anker_ble_emulator.devices.base import Advert, Profile, register\n"
        "from anker_ble_emulator.module import AuthMode\n"
        "from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product\n"
        "\n\n"
        f"PROFILE = Profile(\n{body})\n"
        f"register(Product.{pn.name}, PROFILE)\n"
    )


def _natural(pn: Product) -> list[str | int]:
    return [
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", pn.lower())
        if part
    ]


def render_package(products: list[Product]) -> str:
    """Return the source of the generated profiles' package."""
    # Ruff's isort orders module names naturally: a17c5 before a1728.
    imports = "".join(
        f"from .{pn.lower()} import PROFILE as {pn.name}\n"
        for pn in sorted(products, key=_natural)
    )
    entries = "".join(f"    Product.{pn.name}: {pn.name},\n" for pn in products)
    return (
        f"{HEADER}"
        '"""Profiles generated from anker-solix-api maps and SolixBLE classes.\n\n'
        "Written by ``tools/generate_profiles.py``. A product leaves here when it\n"
        "gets a recorded profile of its own.\n"
        '"""\n\n'
        "from anker_ble_emulator.products import Product\n\n"
        f"{imports}\n\n"
        "#: The generated profiles, by product.\n"
        f"MAP_BUILT = {{\n{entries}}}\n"
    )


def _title(text: str, rule: str) -> str:
    return f"{text}\n{rule * len(text)}\n"


def render_report(sources: list[str], checks: list[Check]) -> str:
    """Return the cross-check page: positions, then command links, per product."""
    parts = [
        _title("SolixBLE cross-check", "="),
        "\nGenerated by ``tools/generate_profiles.py`` from "
        + ", ".join(sources)
        + ", against anker-solix-api's maps. Each property's decode position"
        " is checked against the map's typed field or part there (offsets after"
        " the type byte). Each command is linked to the telemetry it changes: the"
        " map's ``state_name`` on the variant its parameters select, *verified*"
        " when a property reads that field; otherwise by the method's name.\n",
    ]
    for check in checks:
        info = PRODUCTS[Product(check.pn)]
        parts.append(
            "\n" + _title(f"{check.pn} {info.name} (``{check.solixble_class}``)", "-")
        )
        parts.append(
            f"\n{check.agree} positions agree; {len(check.absent)} read a field"
            " the map doesn't type.\n"
        )
        lines = [f"- Conflict: {line}" for line in check.conflicts]
        lines += [f"- Unnamed in the map: {line}" for line in check.gaps]
        lines += [_link_line(link) for link in check.links]
        if lines:
            parts.append("\n" + "\n".join(lines) + "\n")
    return "".join(parts)


def _link_line(link: Link) -> str:
    cmd = "?" if link.cmd is None else f"{link.cmd:04x}"
    head = f"- ``{link.method}`` ``{cmd}``"
    states = ", ".join(f"``{s}``" for s in link.states)
    props = ", ".join(f"``{p}``" for p in link.properties)
    if link.verdict == "position":
        return f"{head}: {states}, verified by {props}"
    if link.verdict == "map":
        return f"{head}: {states}"
    if link.verdict == "name":
        return f"{head}: {states}, by name ({props})"
    if link.verdict == "ambiguous":
        return f"{head}: ambiguous by name, {states}"
    return f"{head}: unlinked"


def generate(data: Mapping[str, Any], devices: Path) -> tuple[dict[Path, str], str]:
    """Return the generated modules by path, and the cross-check page."""
    classes = data["classes"]
    files: dict[Path, str] = {}
    products: list[Product] = []
    checks: list[Check] = []
    for pn, info in sorted(PRODUCTS.items()):
        facts = classes.get(info.solixble_class)
        layout = Layout.load(pn)
        if facts is None or layout is None:
            continue
        checks.append(cross_check(pn, info.solixble_class, facts, layout))
        recorded = (devices / f"{pn.lower()}.py").exists()
        if recorded or info.transport != Transport.NEGOTIATED:
            continue
        files[devices / "generated" / f"{pn.lower()}.py"] = render(
            pn, info.solixble_class, generated(facts, layout)
        )
        products.append(pn)
    files[devices / "generated" / "__init__.py"] = render_package(products)
    return files, render_report(data["sources"], checks)


def main(argv: list[str] | None = None) -> int:
    """Write the generated profiles and the cross-check page; return the status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--solixble", type=Path, default=SOLIXBLE)
    parser.add_argument("--devices", type=Path, default=DEVICES)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)

    files, report = generate(json.loads(args.solixble.read_text()), args.devices)
    (args.devices / "generated").mkdir(exist_ok=True)
    for stale in (args.devices / "generated").glob("*.py"):
        if stale not in files:
            stale.unlink()
    for path, source in files.items():
        path.write_text(source)
    args.report.write_text(report)
    sys.stderr.write(f"wrote {len(files) - 1} profiles\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
