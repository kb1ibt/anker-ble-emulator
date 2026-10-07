# Copyright (c) 2026 Shawn Stricker
"""Collector-style frame logs for the extraction tool's tests."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

LOG_LINE = '{{"message": "ble frame: cmd={cmd} len={n} clear={clear}"}}\n'


def write_frame_log(path: Path, frames: list[tuple[str, bytes]]) -> Path:
    """Write ``ble frame:`` lines for ``(cmd hex, cleartext)`` pairs, plus noise."""
    path.write_text(
        "".join(
            LOG_LINE.format(cmd=cmd, n=len(clear), clear=clear.hex())
            for cmd, clear in frames
        )
        + "unrelated line\n"
    )
    return path
