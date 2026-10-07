# Copyright (c) 2026 Shawn Stricker
"""Extract recorded cleartext frames from logs and sanitize them.

Reads collector ``ble frame: cmd=<cmd> len=<n> clear=<hex>`` lines and MQTT
records (anker-solix-api ``.ndjson`` examples and ``mqtt_monitor`` dumps, which
carry the whole frame as base64 under ``data``), keeps the newest frame of each
requested msgtype, replaces identifiers with synthetic values of the same
length, and writes ``{"<msgtype hex>": "<cleartext hex>"}`` as JSON. MQTT
payloads get the BLE routing marker. The real identifiers come from the command
line and are never stored.

Usage::

    python -m tools.sanitize_frames --output data.json
        --replace REAL_SERIAL=FAKE_SERIAL --forbid REAL_MAC_HEX
        --msgtype 421 --msgtype 900 LOG [LOG ...]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from construct import (
    Bytes,
    Const,
    ConstructError,
    GreedyBytes,
    Int8ub,
    Int32ul,
    Peek,
    Struct,
)

from anker_ble_emulator.frame import COMMAND_LAYOUT, checksum, decode
from anker_ble_emulator.tlv import FIELD_HEADER_LEN, FIELDS_LAYOUT, encode_fields


LINE = re.compile(rb"ble frame: cmd=([0-9a-f]{4}) len=\d+ clear=([0-9a-f]+)")
#: A base64 ``ff09`` frame under an MQTT ``data`` key, JSON-escaped or not.
MQTT_DATA = re.compile(rb'data\\?"\s*:\s*\\?"(/w[k-n][A-Za-z0-9+/]+=*)')
#: The leading ``a1`` routing marker: low nibble source, high nibble destination.
ROUTE_LAYOUT = Struct(
    "tag" / Const(b"\xa1"),
    "length" / Const(b"\x01"),
    "marker" / Int8ub,
    "rest" / GreedyBytes,
)
#: MCU to BLE client.
BLE_ROUTE = 0x31
#: Millisecond epoch timestamps written as ASCII digits.
EPOCH_MS = re.compile(rb"(?<![0-9])1[6-9][0-9]{11}(?![0-9])")
FIXED_EPOCH_MS = b"1790812800000"
#: The typed trailer ``fe 05 03 <u32 LE unix time>``.
TRAILER_TAG = 0xFE
TRAILER_VALUE = Struct("type" / Const(b"\x03"), "time" / Int32ul)
#: A reply payload (status byte, then fields) and a push payload (fields only).
STATUS_AND_BODY = Struct("status" / Bytes(1), "body" / GreedyBytes)
BODY_ONLY = Struct("status" / Bytes(0), "body" / GreedyBytes)
FIXED_UNIX_TIME = 1_790_812_800
#: Printable runs this long are checked against the allowlist.
SUSPECT_RUN = re.compile(rb"[A-Za-z0-9_]{6,}")
FIRST_TAG = 0xA1


class SanitizeError(ValueError):
    """A frame still holds something that looks like an identifier."""


@dataclass(frozen=True)
class Sanitizer:
    """Same-length replacements, forbidden byte strings and allowed strings.

    Attributes:
        replacements: Real bytes to their synthetic stand-ins.
        forbidden: Bytes that must not survive (e.g. a MAC).
        allowed: Printable runs known not to identify a unit.

    """

    replacements: dict[bytes, bytes] = field(default_factory=dict)
    forbidden: tuple[bytes, ...] = ()
    allowed: frozenset[bytes] = frozenset()

    def __post_init__(self) -> None:
        """Check every replacement keeps the length."""
        for real, fake in self.replacements.items():
            if len(real) != len(fake):
                msg = f"replacement for a {len(real)}-byte value is {len(fake)} bytes"
                raise ValueError(msg)

    def sanitize(self, cleartext: bytes) -> bytes:
        """Return the cleartext with identifiers and clocks replaced.

        Raises:
            SanitizeError: If a forbidden value or an unknown identifier-like
                run remains.

        """
        data = cleartext
        for real, fake in self.replacements.items():
            data = data.replace(real, fake)
        data = EPOCH_MS.sub(FIXED_EPOCH_MS, data)
        data = fix_trailer(data)
        for value in self.forbidden:
            if value in data:
                msg = f"forbidden value {value.hex()} remains"
                raise SanitizeError(msg)
        allowed = self.allowed | set(self.replacements.values()) | {FIXED_EPOCH_MS}
        unknown = sorted(
            {run.group() for run in SUSPECT_RUN.finditer(data)} - allowed,
        )
        if unknown:
            names = ", ".join(run.decode() for run in unknown)
            msg = f"unreviewed printable runs remain: {names}"
            raise SanitizeError(msg)
        return data


def fix_trailer(data: bytes) -> bytes:
    """Replace the time in each ``fe 05 03 <u32>`` trailer field.

    Only a payload that walks cleanly as fields to its end is changed.
    """
    status, body = split_status(data)
    fields = list(FIELDS_LAYOUT.parse(body))
    walked = sum(FIELD_HEADER_LEN + len(item.value) for item in fields)
    if walked != len(body) or any(item.tag < FIRST_TAG for item in fields):
        return data
    return status + encode_fields(
        (int(item.tag), _fixed_time(int(item.tag), bytes(item.value)))
        for item in fields
    )


def split_status(data: bytes) -> tuple[bytes, bytes]:
    """Return a payload's status byte (empty for a push) and its fields."""
    first = Peek(Int8ub).parse(data)
    layout = STATUS_AND_BODY if first is not None and first < FIRST_TAG else BODY_ONLY
    parsed = layout.parse(data)
    return bytes(parsed.status), bytes(parsed.body)


def ble_routed(data: bytes) -> bytes:
    """Return the payload with its ``a1`` routing marker set to MCU-to-BLE.

    The MCU sends the same frame to MQTT with another marker (``34``, ``32``).
    """
    status, body = split_status(data)
    try:
        route = ROUTE_LAYOUT.parse(body)
    except ConstructError:
        return data
    return status + ROUTE_LAYOUT.build({"marker": BLE_ROUTE, "rest": route.rest})


def _fixed_time(tag: int, value: bytes) -> bytes:
    if tag != TRAILER_TAG or len(value) != TRAILER_VALUE.sizeof():
        return value
    try:
        TRAILER_VALUE.parse(value)
    except ConstructError:
        return value
    return TRAILER_VALUE.build({"time": FIXED_UNIX_TIME})


def newest_frames(
    paths: list[Path],
    msgtypes: set[int],
    cmds: frozenset[int] = frozenset(),
    *,
    fix_checksum: bool = False,
) -> dict[int, bytes]:
    """Return the last cleartext logged for each wanted msgtype.

    Args:
        paths: Collector logs or MQTT records (plain or ``.gz``), oldest first.
        msgtypes: 12-bit message types to keep, whatever their link flags.
        cmds: Exact logged cmds to keep (``ca00`` but not ``4a00``), by msgtype.
        fix_checksum: Recompute MQTT frames' checksums, for records whose
            identifiers were anonymized after capture.

    Raises:
        FrameError: If an MQTT record's frame is malformed.

    """
    newest: dict[int, bytes] = {}
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as handle:
            for raw in handle:
                line = raw.replace(b"\x00", b"")
                for cmd, clear in _line_frames(line, fix_checksum=fix_checksum):
                    msgtype = cmd & 0x0FFF
                    if msgtype in msgtypes or cmd in cmds:
                        newest[msgtype] = clear
    return newest


def _line_frames(line: bytes, *, fix_checksum: bool) -> list[tuple[int, bytes]]:
    match = LINE.search(line)
    if match is not None:
        return [(int(match.group(1), 16), bytes.fromhex(match.group(2).decode()))]
    wire = [base64.b64decode(data) for data in MQTT_DATA.findall(line)]
    if fix_checksum:
        wire = [data[:-1] + bytes([checksum(data[:-1])]) for data in wire]
    frames = [decode(data) for data in wire]
    return [
        (int.from_bytes(COMMAND_LAYOUT.build(frame.cmd)), ble_routed(frame.payload))
        for frame in frames
    ]


def _pair(text: str) -> tuple[bytes, bytes]:
    real, _, fake = text.partition("=")
    return real.encode(), fake.encode()


def _frame(text: str) -> tuple[int, bytes]:
    msgtype, _, clear = text.partition("=")
    return int(msgtype, 16), bytes.fromhex(clear)


def main(argv: list[str] | None = None) -> int:
    """Run the extraction; return the exit status."""
    parser = argparse.ArgumentParser(
        description="Extract and sanitize recorded frames from logs and MQTT records."
    )
    parser.add_argument("logs", nargs="*", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--msgtype", action="append", default=[])
    parser.add_argument("--cmd", action="append", default=[])
    parser.add_argument("--frame", action="append", default=[], type=_frame)
    parser.add_argument("--replace", action="append", default=[], type=_pair)
    parser.add_argument("--forbid", action="append", default=[], type=bytes.fromhex)
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--fix-checksum", action="store_true")
    args = parser.parse_args(argv)

    sanitizer = Sanitizer(
        replacements=dict(args.replace),
        forbidden=tuple(args.forbid),
        allowed=frozenset(text.encode() for text in args.allow),
    )
    msgtypes = {int(text, 16) for text in args.msgtype}
    cmds = frozenset(int(text, 16) for text in args.cmd)
    recorded = newest_frames(args.logs, msgtypes, cmds, fix_checksum=args.fix_checksum)
    frames = recorded | dict(args.frame)
    missing = (msgtypes | {cmd & 0x0FFF for cmd in cmds}) - frames.keys()
    if missing:
        sys.stderr.write(f"no frames for {sorted(f'{m:03x}' for m in missing)}\n")
        return 1
    clean = {f"{m:03x}": sanitizer.sanitize(frames[m]).hex() for m in sorted(frames)}
    args.output.write_text(json.dumps(clean, indent=2) + "\n")
    sys.stderr.write(f"wrote {len(clean)} frames to {args.output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
