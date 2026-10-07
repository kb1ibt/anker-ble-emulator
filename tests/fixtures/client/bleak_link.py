# Copyright (c) 2026 Shawn Stricker
"""The app's side of the link over a real ``bleak.BleakClient``."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from anker_ble_emulator.backend import COMMAND_UUID, TELEMETRY_UUID

from .handshake import negotiation_steps


if TYPE_CHECKING:
    from bleak import BleakClient
    from bleak.backends.characteristic import BleakGATTCharacteristic

    from .app_client import AppClient, Reply


class BleakLink:
    """Writes requests through bleak and collects opened notifications."""

    def __init__(self, client: BleakClient, app: AppClient) -> None:
        """Pair a connected bleak client with the app's crypto state."""
        self.client = client
        self.app = app
        self.raw: list[bytes] = []
        self.replies: list[Reply] = []

    async def start(self) -> None:
        """Subscribe to the telemetry characteristic."""
        await self.client.start_notify(TELEMETRY_UUID, self._on_notify)

    async def send(
        self,
        msgtype: int,
        fields: list[tuple[int, bytes]],
        pattern: bytes | None = None,
    ) -> list[Reply]:
        """Write one request; return the replies it drew."""
        before = len(self.replies)
        request = (
            self.app.request(msgtype, fields)
            if pattern is None
            else self.app.request(msgtype, fields, pattern)
        )
        await self.client.write_gatt_char(COMMAND_UUID, request, response=False)
        await settle()
        return self.replies[before:]

    async def negotiate(self, token: bytes) -> dict[int, Reply]:
        """Run the app's sequence; replies by msgtype."""
        replies: dict[int, Reply] = {}
        for msgtype, fields in negotiation_steps(self.app, token):
            for reply in await self.send(msgtype, fields):
                replies[reply.frame.msgtype] = reply
            if msgtype == 0x021:
                self.app.install(replies[0x821].fields[0xA1])
        return replies

    def _on_notify(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self.raw.append(bytes(data))
        reply = self.app.open(bytes(data))
        if reply is not None:
            self.replies.append(reply)


async def settle() -> None:
    """Let scheduled notifications run."""
    for _ in range(3):
        await asyncio.sleep(0)
