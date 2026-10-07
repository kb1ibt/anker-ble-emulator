# Copyright (c) 2026 Shawn Stricker
"""The app's negotiation sequence against a module, step by step."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .app_client import TIMESTAMP, AppClient, Reply


if TYPE_CHECKING:
    from anker_ble_emulator.module import Module

TZ = b"EST5EDT,M3.2.0,M11.1.0"
UTC_OFFSET = bytes.fromhex("40380000")


def exchange(
    module: Module, client: AppClient, msgtype: int, fields: list[tuple[int, bytes]]
) -> list[Reply]:
    """Write one request and open every frame the module sends back."""
    out = module.write(client.request(msgtype, fields))
    return [reply for data in out.frames if (reply := client.open(data)) is not None]


def negotiation_steps(
    client: AppClient, token: bytes
) -> list[tuple[int, list[tuple[int, bytes]]]]:
    """Return the app's requests for the client's outer, in order."""
    method = b"\x44" if client.encrypted_outer else b"\x40"
    steps: list[tuple[int, list[tuple[int, bytes]]]] = [
        (0x001, [(0xA1, TIMESTAMP)]),
        (0x003, [(0xA1, TIMESTAMP), (0xA3, b"\x20"), (0xA4, bytes.fromhex("00f0"))]),
        (0x029, [(0xA1, TIMESTAMP)]),
        (
            0x005,
            [(0xA1, TIMESTAMP), (0xA3, b"\x20"), (0xA4, bytes.fromhex("fd00"))]
            + [(0xA5, method)]
            + ([(0xA6, b"\x02")] if client.encrypted_outer else []),
        ),
        (0x021, [(0xA1, client.public_point)]),
        (0x022, [(0xA1, TIMESTAMP), (0xA3, UTC_OFFSET), (0xA5, TZ)]),
    ]
    if client.encrypted_outer:
        steps.append((0x027, [(0xA1, TIMESTAMP), (0xA2, token)]))
    return steps


def negotiate(module: Module, client: AppClient, token: bytes) -> dict[int, Reply]:
    """Run the app's full sequence for the client's outer; replies by msgtype.

    The key exchange reply installs the client's session before ``x022``.
    """
    replies: dict[int, Reply] = {}
    for msgtype, fields in negotiation_steps(client, token):
        for reply in exchange(module, client, msgtype, fields):
            replies[reply.frame.cmd.msgtype] = reply
        if msgtype == 0x021:
            client.install(replies[0x821].fields[0xA1])
    return replies
