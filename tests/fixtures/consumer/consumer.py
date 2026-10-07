# Copyright (c) 2026 Shawn Stricker
"""A client library's connect path, shaped like SolixBLE's: the patch target."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bleak import BleakClient


if TYPE_CHECKING:
    from collections.abc import Callable

    from bleak.backends.device import BLEDevice

CONSUMER_TARGET = "tests.fixtures.consumer.consumer.establish_connection"


async def establish_connection(  # noqa: PLR0913  # bleak-retry-connector's signature
    client_class: type[BleakClient],
    device: BLEDevice,
    name: str,
    *,
    max_attempts: int = 4,
    use_services_cache: bool = True,
    disconnected_callback: Callable[[BleakClient], None] | None = None,
) -> BleakClient:
    """Fail: tests reach this only when nothing patched it."""
    msg = f"establish_connection not patched for {name}"
    raise RuntimeError(msg)


def ignore_disconnect(_client: BleakClient) -> None:
    """Ignore a drop, for tests that don't watch the link."""


async def connect(
    ble_device: BLEDevice, disconnected_callback: Callable[[BleakClient], None]
) -> BleakClient:
    """Connect the way SolixBLE's ``SolixBLEDevice.connect`` does."""
    return await establish_connection(
        BleakClient,
        device=ble_device,
        name=ble_device.address,
        max_attempts=3,
        use_services_cache=False,
        disconnected_callback=disconnected_callback,
    )
