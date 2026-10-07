# Copyright (c) 2026 Shawn Stricker
"""Extract recorded cleartext frames from collector logs and sanitize them.

Reads ``ble frame: cmd=<cmd> len=<n> clear=<hex>`` lines, keeps the newest
frame of each requested msgtype, replaces identifiers with synthetic values of
the same length, and writes ``{"<msgtype hex>": "<cleartext hex>"}`` as JSON.
The real identifiers come from the command line and are never stored.

Usage::

    python -m tools.sanitize_frames --output data.json
        --replace REAL_SERIAL=FAKE_SERIAL --forbid REAL_MAC_HEX
        --msgtype 421 --msgtype 900 LOG [LOG ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


LINE = re.compile(rb"ble frame: cmd=([0-9a-f]{4}) len=\d+ clear=([0-9a-f]+)")
#: Millisecond epoch timestamps written as ASCII digits.
EPOCH_MS = re.compile(rb"(?<![0-9])1[6-9][0-9]{11}(?![0-9])")
FIXED_EPOCH_MS = b"1790812800000"
#: The typed trailer ``fe 05 03 <u32 LE unix time>``.
TRAILER_TAG = 0xFE
TRAILER_TYPE = 0x03
FIXED_UNIX_TIME = (1_790_812_800).to_bytes(4, "little")
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
    """Replace the time in a trailing ``fe 05 03 <u32>`` field.

    Only a payload that walks cleanly as typed fields to its end is changed.
    """
    offset = 1 if data and data[0] < FIRST_TAG else 0
    trailer = -1
    while offset < len(data):
        if offset + 2 > len(data):
            return data
        tag, length = data[offset], data[offset + 1]
        if tag < FIRST_TAG or offset + 2 + length > len(data):
            return data
        if tag == TRAILER_TAG and length == 5 and data[offset + 2] == TRAILER_TYPE:  # noqa: PLR2004  # type byte + u32
            trailer = offset + 3
        offset += 2 + length
    if trailer < 0:
        return data
    return data[:trailer] + FIXED_UNIX_TIME + data[trailer + 4 :]


def newest_frames(paths: list[Path], msgtypes: set[int]) -> dict[int, bytes]:
    """Return the last cleartext logged for each wanted msgtype.

    Args:
        paths: Collector logs, oldest first.
        msgtypes: 12-bit message types to keep.

    """
    newest: dict[int, bytes] = {}
    for path in paths:
        with path.open("rb") as handle:
            for raw in handle:
                match = LINE.search(raw.replace(b"\x00", b""))
                if match is None:
                    continue
                msgtype = int(match.group(1), 16) & 0x0FFF
                if msgtype in msgtypes:
                    newest[msgtype] = bytes.fromhex(match.group(2).decode())
    return newest


def _pair(text: str) -> tuple[bytes, bytes]:
    real, _, fake = text.partition("=")
    return real.encode(), fake.encode()


def _frame(text: str) -> tuple[int, bytes]:
    msgtype, _, clear = text.partition("=")
    return int(msgtype, 16), bytes.fromhex(clear)


def main(argv: list[str] | None = None) -> int:
    """Run the extraction; return the exit status."""
    parser = argparse.ArgumentParser(
        description="Extract and sanitize recorded frames from collector logs."
    )
    parser.add_argument("logs", nargs="*", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--msgtype", action="append", default=[])
    parser.add_argument("--frame", action="append", default=[], type=_frame)
    parser.add_argument("--replace", action="append", default=[], type=_pair)
    parser.add_argument("--forbid", action="append", default=[], type=bytes.fromhex)
    parser.add_argument("--allow", action="append", default=[])
    args = parser.parse_args(argv)

    sanitizer = Sanitizer(
        replacements=dict(args.replace),
        forbidden=tuple(args.forbid),
        allowed=frozenset(text.encode() for text in args.allow),
    )
    wanted = {int(text, 16) for text in args.msgtype}
    frames = newest_frames(args.logs, wanted) | dict(args.frame)
    missing = wanted - frames.keys()
    if missing:
        sys.stderr.write(f"no frames for {sorted(f'{m:03x}' for m in missing)}\n")
        return 1
    clean = {f"{m:03x}": sanitizer.sanitize(frames[m]).hex() for m in sorted(frames)}
    args.output.write_text(json.dumps(clean, indent=2) + "\n")
    sys.stderr.write(f"wrote {len(clean)} frames to {args.output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
