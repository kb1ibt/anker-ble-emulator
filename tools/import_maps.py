# Copyright (c) 2026 Shawn Stricker
"""Build emulator layouts from anker-solix-api's MQTT maps, typed from recordings.

The maps name each message's fields, but most leave the field type to the frame.
The tool walks recorded frames (collector logs, MQTT records, or
``pn<TAB>kind<TAB>msgtype<TAB>payload hex`` lines), learns each field's type
byte and length, and carries it to every product that shares the map and to
same-named fields where every recording agrees. It writes one ``<pn>.json``
per product: message layouts (no values) and the command fields with their
ranges.

Usage::

    python -m tools.import_maps --solix-api ../anker-solix-api-pr-naming/src
        --output src/anker_ble_emulator/maps FRAMES [FRAMES ...]
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

from anker_ble_emulator.frame import FrameError, decode
from anker_ble_emulator.tlv import FIELD_HEADER_LEN, FIELDS_LAYOUT
from tools.sanitize_frames import LINE, MQTT_DATA, split_status


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from types import ModuleType

    #: One anker-solix-api map entry: a message, a field or a command.
    Desc: TypeAlias = dict[str, Any]

#: A collector line's product, when the line carries it.
DEVICE_PN = re.compile(rb'"device_pn": "(\w+)"')
#: The product of the MQTT records that follow.
TOPIC_PN = re.compile(rb"\b(?:dt|cmd)/anker_power/(\w+)/")
RECORD_PN = re.compile(rb'\\?"(?:pn|device_pn)\\?"\s*:\s*\\?"(A\w{4,5})')
#: Byte sizes of the fixed-width value types, by type byte.
FIXED_SIZES = {0x01: 1, 0x02: 2, 0x03: 4, 0x05: 4}
TAG = re.compile(r"^[a-f0-9]{2}$")
#: A frame without a msgtype must have this share of its tags in a map.
MATCH_SHARE = 0.9
MATCH_MIN_TAGS = 5
FIRST_TAG = 0xA1
#: A typed field value: its type byte, then at least one value byte.
MIN_TYPED_VALUE = 2
#: A listed frame: product, kind, msgtype and payload, tab separated.
LISTED_TABS = 3


@dataclass(frozen=True)
class Recorded:
    """One recorded frame: product and msgtype where known, and its payload."""

    pn: str | None
    msgtype: int | None
    payload: bytes


@dataclass
class Learned:
    """Field types and lengths seen in recordings.

    Attributes:
        by_map: ``(type, length)`` counts by map identity and tag, one per
            distinct frame.
        by_name: ``(type, length)`` counts by field name.
        frames: Distinct recorded frames matched to a map.

    """

    by_map: dict[tuple[int, str], Counter[tuple[int, int]]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    by_name: dict[str, Counter[tuple[int, int]]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    frames: int = 0


def load_maps(src: Path) -> tuple[dict[str, Desc], ModuleType]:
    """Return anker-solix-api's ``SOLIXMQTTMAP`` and its ``mqttcmdmap`` module.

    Args:
        src: The checkout's ``src`` directory.

    """
    sys.path.insert(0, str(src))
    try:
        mqttmap = importlib.import_module("anker_solix_api.mqttmap")
        keys = importlib.import_module("anker_solix_api.mqttcmdmap")
    finally:
        sys.path.remove(str(src))
    return mqttmap.SOLIXMQTTMAP, keys


def fields_of(payload: bytes) -> list[tuple[str, bytes]]:
    """Return a payload's ``(tag hex, value)`` fields if they walk to its end."""
    _, body = split_status(payload)
    fields = []
    walked = 0
    for item in FIELDS_LAYOUT.parse(body):
        if int(item.tag) < FIRST_TAG:
            break
        fields.append((f"{int(item.tag):02x}", bytes(item.value)))
        walked += FIELD_HEADER_LEN + len(item.value)
    return fields if walked == len(body) else []


def recorded_frames(paths: Iterable[Path]) -> Iterator[Recorded]:
    """Yield every frame in collector logs, MQTT records and frame lists."""
    for path in paths:
        pn: str | None = None
        with path.open("rb") as handle:
            for raw in handle:
                line = raw.replace(b"\x00", b"")
                if line.count(b"\t") == LISTED_TABS:
                    yield _listed(line)
                    continue
                if found := TOPIC_PN.search(line) or RECORD_PN.search(line):
                    pn = found.group(1).decode()
                if (match := LINE.search(line)) is not None:
                    device = DEVICE_PN.search(line)
                    yield Recorded(
                        device.group(1).decode() if device else None,
                        int(match.group(1), 16) & 0x0FFF,
                        bytes.fromhex(match.group(2).decode()),
                    )
                for data in MQTT_DATA.findall(line):
                    try:
                        frame = decode(base64.b64decode(data))
                    except FrameError:
                        continue
                    yield Recorded(pn, frame.cmd.msgtype, bytes(frame.payload))


def _listed(line: bytes) -> Recorded:
    pn, _, msgtype, payload = line.decode().rstrip("\n").split("\t")
    return Recorded(pn, int(msgtype, 16) if msgtype else None, bytes.fromhex(payload))


def message_maps(maps: dict[str, Desc], keys: ModuleType) -> dict[str, Desc]:
    """Return each product's telemetry maps by 4-hex msgtype (commands left out)."""
    return {
        pn: {
            msgtype: desc
            for msgtype, desc in entries.items()
            if isinstance(desc, dict)
            and keys.COMMAND_NAME not in desc
            and keys.COMMAND_LIST not in desc
        }
        for pn, entries in maps.items()
    }


def match_map(messages: Desc, recorded: Recorded, tags: set[str]) -> Desc | None:
    """Return the map a recorded frame belongs to: by msgtype, else by its tags.

    By tags, the map must hold nearly all the frame's tags, or, for a frame of
    a known product, the frame must hold nearly all of a sparse map's tags.
    """
    if recorded.msgtype is not None:
        found: Desc | None = messages.get(f"{recorded.msgtype:04x}")
        if found is not None:
            return found
    if len(tags) < MATCH_MIN_TAGS:
        return None
    best, share = None, 0.0
    for desc in messages.values():
        mapped = {key for key in desc if TAG.match(key)} - {"a1"}
        common = len(tags & mapped)
        covered = common / len(tags)
        if recorded.pn is not None and len(mapped) >= MATCH_MIN_TAGS:
            covered = max(covered, common / len(mapped))
        if covered > share:
            best, share = desc, covered
    return best if share >= MATCH_SHARE else None


def learn(
    frames: Iterable[Recorded], maps: dict[str, Desc], keys: ModuleType
) -> Learned:
    """Count each mapped field's type byte and length across the recordings."""
    messages = message_maps(maps, keys)
    learned = Learned()
    counted: set[Recorded] = set()
    for recorded in frames:
        if recorded in counted:
            continue
        counted.add(recorded)
        candidates = (
            [messages[recorded.pn]] if recorded.pn in messages else messages.values()
        )
        fields = fields_of(recorded.payload)
        tags = {tag for tag, _ in fields} - {"a1"}
        desc = next(
            (
                found
                for product in candidates
                if (found := match_map(product, recorded, tags)) is not None
            ),
            None,
        )
        if desc is None:
            continue
        learned.frames += 1
        for tag, value in fields:
            spec = desc.get(tag)
            if not isinstance(spec, dict) or len(value) < MIN_TYPED_VALUE:
                continue
            seen = (value[0], len(value))
            learned.by_map[id(desc), tag][seen] += 1
            if name := spec.get(keys.NAME):
                learned.by_name[name][seen] += 1
    return learned


def field_layout(desc: Desc, tag: str, learned: Learned, keys: ModuleType) -> Desc:
    """Return one field's layout: its type and length, and where they came from."""
    spec = desc[tag]
    name = spec.get(keys.NAME)
    layout: Desc = {"tag": tag, "name": name}
    named = learned.by_name.get(name, Counter()) if name else Counter()
    if recorded := learned.by_map.get((id(desc), tag)):
        (kind, length), _ = recorded.most_common(1)[0]
        layout |= {"type": f"{kind:02x}", "length": length, "from": "recorded"}
    elif (kind_bytes := spec.get(keys.TYPE)) and kind_bytes[0] in FIXED_SIZES:
        kind = kind_bytes[0]
        size = spec.get(keys.LENGTH, FIXED_SIZES[kind])
        layout |= {"type": f"{kind:02x}", "length": 1 + size, "from": "map"}
    elif len(named) == 1:
        (kind, length), _ = named.most_common(1)[0]
        layout |= {"type": f"{kind:02x}", "length": length, "from": "name"}
    sub = spec.get(keys.BYTES)
    if isinstance(sub, dict):
        layout["bytes"] = [
            _sub_field(part, keys) | {"offset": int(offset)}
            for offset, parts in sub.items()
            for part in (parts if isinstance(parts, list) else [parts])
        ]
    elif isinstance(sub, list):
        layout["sequence"] = [_sub_field(part, keys) for part in sub]
    return layout


def _sub_field(part: Desc, keys: ModuleType) -> Desc:
    """Return a sub-field; in a sequence its ``skip`` follows the previous field."""
    sub: Desc = {"name": part.get(keys.NAME)}
    if (skip := part.get(keys.OFFSET)) is not None:
        sub["skip"] = skip
    if kind := part.get(keys.TYPE):
        sub["type"] = kind.hex()
    if (length := part.get(keys.LENGTH)) is not None:
        sub["length"] = length
    if (mask := part.get(keys.MASK)) is not None:
        sub["mask"] = mask
    return sub


def _state(part: Desc, options: list[Any] | None, keys: ModuleType) -> Desc:
    """Return the telemetry field a setting shows in, and how its value maps there.

    A setting with a divider, or whose value follows another setting, is left
    unlinked. A converted one is linked only where its options give a table.
    """
    state = part.get(keys.STATE_NAME)
    if not state or keys.VALUE_DIVIDER in part or keys.VALUE_FOLLOWS in part:
        return {}
    converter = part.get(keys.STATE_CONVERTER)
    if converter is None:
        return {"state": state}
    if not options:
        return {}
    try:
        table = {str(option): converter(option, None, {}) for option in options}
    except (TypeError, KeyError, AttributeError, ValueError):
        return {}
    if not all(isinstance(value, int) for value in table.values()):
        return {}
    return {"state": state, "state_values": table}


def command_layout(spec: Desc, keys: ModuleType) -> Desc:
    """Return one command's name and its fields' types and accepted values."""
    fields = {}
    for tag, part in spec.items():
        if not TAG.match(tag) or not isinstance(part, dict):
            continue
        out: Desc = {"name": part.get(keys.NAME)}
        if kind := part.get(keys.TYPE):
            out["type"] = kind.hex()
        for key, label in (
            (keys.VALUE_MIN, "min"),
            (keys.VALUE_MAX, "max"),
            (keys.VALUE_STEP, "step"),
            (keys.VALUE_DEFAULT, "default"),
            (keys.LENGTH, "length"),
        ):
            if isinstance(value := part.get(key), int | float | str):
                out[label] = value
        options = part.get(keys.VALUE_OPTIONS)
        if isinstance(options, dict):
            out["options"] = list(options.values())
        elif isinstance(options, list):
            out["options"] = options
        out |= _state(part, out.get("options"), keys)
        fields[tag] = out
    return {"command": spec.get(keys.COMMAND_NAME), "fields": fields}


def product_layout(entries: Desc, learned: Learned, keys: ModuleType) -> Desc:
    """Return a product's message layouts and commands."""
    messages: Desc = {}
    commands: dict[str, list[Desc]] = {}
    for msgtype, desc in entries.items():
        if not isinstance(desc, dict):
            continue
        if keys.COMMAND_LIST in desc:
            commands[msgtype] = [
                command_layout(desc[name] | {keys.COMMAND_NAME: name}, keys)
                for name in desc[keys.COMMAND_LIST]
            ]
        elif keys.COMMAND_NAME in desc:
            commands[msgtype] = [command_layout(desc, keys)]
        else:
            messages[msgtype] = {
                "topic": desc.get(keys.TOPIC),
                "fields": [
                    field_layout(desc, tag, learned, keys)
                    for tag in desc
                    if TAG.match(tag)
                ],
            }
    return {"messages": messages, "commands": commands}


def revision(src: Path) -> str:
    """Return the checkout's commit and date, or an empty string outside git."""
    result = subprocess.run(  # noqa: S603  # fixed git arguments
        ["git", "-C", str(src), "log", "-1", "--format=%h %ad", "--date=short"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    """Run the import; return the exit status."""
    parser = argparse.ArgumentParser(
        description="Build emulator layouts from anker-solix-api's MQTT maps."
    )
    parser.add_argument("frames", nargs="*", type=Path)
    parser.add_argument("--solix-api", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pn", action="append", default=[])
    args = parser.parse_args(argv)

    maps, keys = load_maps(args.solix_api)
    learned = learn(recorded_frames(args.frames), maps, keys)
    source = f"anker-solix-api {revision(args.solix_api)}".strip()
    args.output.mkdir(parents=True, exist_ok=True)
    products = sorted(args.pn or maps)
    for pn in products:
        layout = {"pn": pn, "source": source} | product_layout(maps[pn], learned, keys)
        (args.output / f"{pn.lower()}.json").write_text(
            json.dumps(layout, indent=1) + "\n"
        )
    sys.stderr.write(
        f"learned from {learned.frames} frames; wrote {len(products)} layouts\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
