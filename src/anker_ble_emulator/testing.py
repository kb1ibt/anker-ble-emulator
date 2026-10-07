# Copyright (c) 2026 Shawn Stricker
"""A drop-in for SolixBLE's test ``MockDevice``, backed by an emulated device.

``EmulatedConnection`` patches the client library's ``establish_connection``
so it returns a real, connected ``BleakClient`` on the emulator backend. The
device answers for itself; the ``MockDevice`` names are kept so tests move over
file by file, and ``expect_ordered`` becomes an optional assertion on writes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self
from unittest import mock

from bleak.exc import BleakError

from .backend import EmulatedBleakBackend
from .module import Output


if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from bleak import BleakClient
    from bleak.args import SizedBuffer
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.client import NotifyCallback

    from .devices import EmulatedDevice

#: Where SolixBLE looks up ``establish_connection``.
SOLIXBLE_TARGET = "SolixBLE.device.establish_connection"


@dataclass
class RequestResponse:
    """One expected write, as ``MockDevice`` records it.

    Attributes:
        name: Names the expectation in failure messages.
        expected: The exact bytes expected, or None for any.
        response: Raw notifications sent after the device's own replies.
        called: Whether the write arrived.
        refuse: Drop the link instead of letting the device answer.
        refuse_during_write: Drop before the write returns, so it raises.

    """

    name: str
    expected: bytes | None
    response: list[bytes] = field(default_factory=list)
    called: bool = False
    refuse: bool = False
    refuse_during_write: bool = False


class EmulatedConnection:
    """Connects a client library's ``establish_connection`` to an emulated device."""

    def __init__(self, device: EmulatedDevice, target: str = SOLIXBLE_TARGET) -> None:
        """Prepare to patch ``target`` for ``device``.

        Args:
            device: The emulated device every new client connects to.
            target: The dotted path of the ``establish_connection`` to patch.

        """
        self.device = device
        self.clients: list[BleakClient] = []
        self.writes: list[bytes] = []
        self.write_uuids: list[str] = []
        self.notify_uuids: list[str] = []
        self._expectations: list[RequestResponse] = []
        self._position = 0
        self._connection_error: BaseException | type[BaseException] | None = None
        self._patcher = mock.patch(target, new=self._establish)
        self._backend = _recording_backend(self)

    async def __aenter__(self) -> Self:
        """Start patching."""
        self._patcher.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Disconnect any client still connected and stop patching."""
        for client in self.clients:
            if client.is_connected:
                await client.disconnect()
        self._patcher.stop()

    def expect_ordered(
        self, value: bytes | None = None, response: list[bytes] | None = None
    ) -> None:
        """Expect the next write to be ``value`` (None for any).

        Args:
            value: The exact bytes expected, or None for any.
            response: Raw notifications to send after the device's own replies.

        """
        self._expectations.append(
            RequestResponse(
                name=f"num {len(self._expectations)}",
                expected=value,
                response=list(response or []),
            ),
        )

    def expect_ordered_all(self, requests: list[RequestResponse]) -> None:
        """Expect each of ``requests``, in order."""
        self._expectations.extend(requests)

    def refuse_after(
        self, value: bytes | None = None, *, during_write: bool = False
    ) -> None:
        """Expect the next write to be ``value``; drop the link instead of answering.

        Args:
            value: The exact bytes expected, or None for any.
            during_write: Drop before the write returns, so it raises.

        """
        self._expectations.append(
            RequestResponse(
                name=f"num {len(self._expectations)} (refused)",
                expected=value,
                refuse=True,
                refuse_during_write=during_write,
            ),
        )

    def check_assertions(self) -> None:
        """Check every expected write arrived.

        Raises:
            AssertionError: If one didn't.

        """
        missing = [item.name for item in self._expectations if not item.called]
        if missing:
            msg = f"expected writes never made: {', '.join(missing)}"
            raise AssertionError(msg)

    def new_connection_error(self, error: BaseException | type[BaseException]) -> None:
        """Make every new connection raise ``error`` until ``allow_connect``."""
        self._connection_error = error

    def allow_connect(self) -> None:
        """Let new connections succeed again."""
        self._connection_error = None

    def disconnect(self) -> None:
        """Drop the link from the device side."""
        self.device.drop()

    async def send_data(self, data: list[bytes]) -> None:
        """Send raw bytes to the connected client as notifications."""
        self.device.notify(data)
        await asyncio.sleep(0)

    async def _establish(
        self,
        client_class: type[BleakClient],
        device: object = None,
        name: str | None = None,
        *,
        disconnected_callback: Callable[[BleakClient], None] | None = None,
        **kwargs: object,
    ) -> BleakClient:
        """Stand in for ``establish_connection``: connect a client to the device."""
        del device, name, kwargs
        if self._connection_error is not None:
            raise self._connection_error
        client = client_class(
            self.device.ble_device,
            disconnected_callback=disconnected_callback,
            backend=self._backend,
        )
        await client.connect()
        self.clients.append(client)
        return client

    def _expectation_for(self, data: bytes) -> RequestResponse | None:
        """Check a write against the next expectation, if any are set.

        Raises:
            AssertionError: If the write is unexpected or isn't the expected bytes.

        """
        if not self._expectations:
            return None
        if self._position >= len(self._expectations):
            msg = (
                f"unexpected write {data.hex()}: "
                f"{len(self._expectations)} expected, all made"
            )
            raise AssertionError(msg)
        expectation = self._expectations[self._position]
        if expectation.expected is not None and expectation.expected != data:
            msg = (
                f"expected {expectation.expected.hex()} for {expectation.name}, "
                f"got {data.hex()}"
            )
            raise AssertionError(msg)
        self._position += 1
        expectation.called = True
        return expectation


def _recording_backend(connection: EmulatedConnection) -> type[EmulatedBleakBackend]:
    """Return a backend class that reports writes and subscriptions."""

    class RecordingBackend(EmulatedBleakBackend):
        async def write_gatt_char(
            self,
            characteristic: BleakGATTCharacteristic,
            data: SizedBuffer,
            response: bool,
        ) -> None:
            written = bytes(data)
            connection.writes.append(written)
            connection.write_uuids.append(characteristic.uuid)
            expectation = connection._expectation_for(written)  # noqa: SLF001
            if expectation is not None and expectation.refuse:
                if expectation.refuse_during_write:
                    self._drop()
                    msg = "disconnected"
                    raise BleakError(msg)
                self._deliver(Output(disconnect=True))
                return
            await super().write_gatt_char(characteristic, data, response)
            if expectation is not None and expectation.response:
                self._deliver(Output(list(expectation.response)))

        async def start_notify(
            self,
            characteristic: BleakGATTCharacteristic,
            callback: NotifyCallback,
            **kwargs: Any,
        ) -> None:
            connection.notify_uuids.append(characteristic.uuid)
            await super().start_notify(characteristic, callback, **kwargs)

    return RecordingBackend
