# Copyright (c) 2026 Shawn Stricker
"""Prime rigs: an authorized app link on the module, its commands and pushes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from construct import Int32ul

from anker_ble_emulator import Outer
from anker_ble_emulator.layouts import ui
from anker_ble_emulator.parameters import ParameterDict, Parameters
from tests.fixtures.client import (
    SESSION,
    TIMESTAMP,
    AppClient,
    cmd_hex,
    negotiate,
)


if TYPE_CHECKING:
    from anker_ble_emulator import EmulatedDevice, ManualClock
    from anker_ble_emulator.module import Output

#: The device's UTC epoch once the handshake's ``0022`` sets it; its RTC (local
#: seconds, ``UTC_OFFSET`` west) then reads Sunday 23:25:14.
PRIME_EPOCH = Int32ul.parse(TIMESTAMP)
#: A request's route: BLE to the MCU.
ROUTE = (0xA1, b"\x21")


def prime_client(device: EmulatedDevice, token: bytes) -> AppClient:
    """Return an app client on the device's module, negotiated and authorized.

    It connects on the device's outer: the A2345 (encrypted) needs the button,
    the A91B2 (plain) authorizes at the key exchange.
    """
    module = device.negotiated_module
    client = AppClient(encrypted_outer=device.outer == Outer.ENCRYPTED)
    module.connect()
    negotiate(module, client, token)
    device.press_button()
    return client


def command(
    device: EmulatedDevice,
    client: AppClient,
    msgtype: int,
    fields: list[tuple[int, bytes]],
) -> list[tuple[str, str]]:
    """Send a session request routed to the MCU; return each frame as (cmd, hex)."""
    request = client.request(msgtype, [ROUTE, *fields], SESSION)
    return opened(client, device.negotiated_module.write(request))


def advance(
    device: EmulatedDevice, client: AppClient, clock: ManualClock, seconds: float
) -> list[tuple[str, str]]:
    """Advance the clock, run what fell due; return its pushes as (cmd, hex)."""
    clock.advance(seconds)
    return opened(client, device.negotiated_module.check_pushes())


def opened(client: AppClient, out: Output) -> list[tuple[str, str]]:
    """Return each frame the module sent, opened, as (cmd hex, cleartext hex)."""
    replies = (client.open(data) for data in out.frames)
    return [(cmd_hex(r.frame), r.plaintext.hex()) for r in replies if r is not None]


def snapshot(device: EmulatedDevice) -> ParameterDict:
    """Return the ``4a00`` snapshot's fields as the device would answer now."""
    module = device.negotiated_module
    reply = module.mcu.respond(0x200, values=module.values, rtc=module.rtc)[0]
    return Parameters.parse(reply.payload)


def arm(seconds: int) -> tuple[int, bytes]:
    """Return ``4209``'s ``a3`` field: the countdown armed for ``seconds``."""
    return 0xA3, b"\x04\x01" + ui(4).build(seconds)


def device_clock(seconds: int) -> str:
    """Return the ``fe`` a push carries ``seconds`` after the handshake, as hex."""
    return "fe0503" + ui(4).build(PRIME_EPOCH + seconds).hex()
