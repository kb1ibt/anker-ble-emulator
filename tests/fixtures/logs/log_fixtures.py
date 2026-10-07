# Copyright (c) 2026 Shawn Stricker
"""Collector-style frame logs for the extraction tool's tests."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from anker_ble_emulator.frame import CHANNEL_SESSION, COMPOSER_SEND, encode, make_frame


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


def mqtt_frame(msgtype: int, payload: str) -> bytes:
    """Return a device frame as MQTT carries it: cleartext ``03010f``, no flags."""
    return encode(
        make_frame(COMPOSER_SEND, CHANNEL_SESSION, msgtype, bytes.fromhex(payload))
    )


def write_mqtt_records(path: Path, frames: list[bytes]) -> Path:
    """Write wire frames as MQTT records, alternating the two on-disk shapes.

    Even frames are anker-solix-api ``.ndjson`` lines (the payload JSON is a
    string, so its quotes are escaped); odd ones are ``mqtt_monitor`` dump
    lines (a Python dict repr holding unescaped JSON).
    """
    lines = []
    for index, frame in enumerate(frames):
        data = base64.b64encode(frame).decode()
        if index % 2 == 0:
            payload = json.dumps({"data": data, "sn": "ZZTEST"})
            lines.append(json.dumps({"topic": "dt/x", "payload": payload}))
        else:
            payload = json.dumps({"data": data}, separators=(",", ":"))
            lines.append(f"{{'head': {{'cmd': 16}}, 'payload': '{payload}'}}")
    path.write_text("\n".join([*lines, "unrelated line"]) + "\n")
    return path
