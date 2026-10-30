# Copyright (c) 2026 Shawn Stricker
"""C Gen 2 rigs: an authorized app link to watch pushes, and the next ``0421``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from construct import GreedyBytes

from anker_ble_emulator.parameters import ParameterDict, Parameters, decode_field
from tests.fixtures.client import AppClient, cmd_hex, negotiate


if TYPE_CHECKING:
    from anker_ble_emulator import EmulatedDevice
    from anker_ble_emulator.layouts import Value
    from anker_ble_emulator.module import Output


def authorized_client(device: EmulatedDevice, token: bytes) -> AppClient:
    """Return an app client on the device's module, negotiated and button-granted."""
    module = device.negotiated_module
    client = AppClient()
    module.connect()
    negotiate(module, client, token)
    device.press_button()
    return client


def pushed(client: AppClient, outputs: list[Output]) -> list[tuple[str, str]]:
    """Return each opened frame in ``outputs`` as (cmd hex, cleartext hex)."""
    replies = (client.open(data) for output in outputs for data in output.frames)
    return [
        (cmd_hex(reply.frame), reply.plaintext.hex())
        for reply in replies
        if reply is not None
    ]


def next_0421(device: EmulatedDevice) -> tuple[dict[str, Value], ParameterDict]:
    """Return the ``0421`` the device would push now: its named values, its tags.

    Raises:
        TypeError: If the device has no layout to name the values.

    """
    layout = device.layout
    if layout is None:
        msg = f"{device.pn} has no layout"
        raise TypeError(msg)
    module = device.negotiated_module
    frame = module.mcu.push(0x421, values=module.values, rtc=module.rtc)
    return layout.read(0x421, frame.payload), Parameters.parse(frame.payload)


def socket_and_ports(fields: ParameterDict) -> tuple[int, str, str, str, str, int]:
    """Return ``b2[0]``, the PD1-PD3 blocks (hex), ``a6[0:2]`` (hex) and ``a5[1]``."""
    return (
        decode_field(fields, "b2", GreedyBytes)[0],
        decode_field(fields, "aa", GreedyBytes).hex(),
        decode_field(fields, "ab", GreedyBytes).hex(),
        decode_field(fields, "ac", GreedyBytes).hex(),
        decode_field(fields, "a6", GreedyBytes)[0:2].hex(),
        decode_field(fields, "a5", GreedyBytes)[1],
    )
