# Copyright (c) 2026 Shawn Stricker
"""C Gen 2 rigs: an authorized app link to watch pushes, and the next ``0421``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from construct import GreedyBytes

from anker_ble_emulator.parameters import ParameterDict, Parameters, decode_field
from tests.fixtures.client import (
    SESSION,
    AppClient,
    Reply,
    cmd_hex,
    exchange,
    negotiate,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

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


def command(
    device: EmulatedDevice,
    client: AppClient,
    msgtype: int,
    fields: list[tuple[int, bytes]],
) -> list[Reply]:
    """Send one session request routed BLE -> MCU; return the replies, opened."""
    request = client.request(msgtype, [(0xA1, b"\x21"), *fields], SESSION)
    out = device.negotiated_module.write(request)
    return [reply for data in out.frames if (reply := client.open(data)) is not None]


def tou_on_the_cloud(
    device: EmulatedDevice,
    token: bytes,
    clock_set: list[tuple[int, bytes]],
    plan: list[tuple[int, bytes]],
) -> None:
    """Set the RTC (``0022``) and store ``plan`` (``4090``), then go on the cloud."""
    client = authorized_client(device, token)
    exchange(device.negotiated_module, client, 0x022, clock_set)
    command(device, client, 0x090, plan)
    device.set_cloud(True)  # the device's own on/off switch


def backup_plan(
    mode: int,
    switch: int | None = None,
    plans: tuple[tuple[int, int], ...] = (),
    *,
    cancel: bool = False,
) -> list[tuple[int, bytes]]:
    """Return a ``405e``'s fields: ``a3`` 3, ``mode``, the switch, plans, a cancel.

    Each plan is (start, end) UTC epochs, sent as ``{100, 0, start, end}``.
    """
    fields = [(0xA3, b"\x01\x03"), (0xA4, bytes([0x01, mode]))]
    if switch is not None:
        fields.append((0xA5, bytes([0x01, switch])))
    if plans:
        fields.append((0xA6, bytes([0x01, len(plans)])))
        body = b"".join(
            bytes([100, 0]) + start.to_bytes(4, "little") + end.to_bytes(4, "little")
            for start, end in plans
        )
        fields.append((0xA7, b"\x04" + body))
    if cancel:
        fields.append((0xAA, b"\x01\x01"))
    return fields


def backup_tail(values: Mapping[str, Value]) -> tuple[Value, ...]:
    """Return the ``d9`` backup tail: type, both switches, table 1, the active pair."""
    return tuple(
        values[name]
        for name in (
            "tou_setting_system_status",
            "manual_backup_switch",
            "auto_backup_switch",
            "manual_backup_start_time",
            "manual_backup_end_time",
            "backup_start_time",
            "backup_end_time",
        )
    )


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


def next_0490(device: EmulatedDevice) -> dict[str, Value]:
    """Return the named values of the ``0490`` the device would post now.

    Raises:
        TypeError: If the device posts no ``0490``.

    """
    module = device.negotiated_module
    push_490 = module.mcu.push_490
    if push_490 is None:
        msg = f"{device.pn} posts no 0490"
        raise TypeError(msg)
    frame = module.mcu.push(0x490, values=module.values, rtc=module.rtc)
    return push_490.read(frame.payload)


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
