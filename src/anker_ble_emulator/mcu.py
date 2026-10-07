# Copyright (c) 2026 Shawn Stricker
"""The device MCU behind the module: recorded cleartext replies and pushes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .frame import CHANNEL_SESSION, COMPOSER_SEND, RESPONSE, make_frame
from .messages import BLE_REPLY_ROUTE, ROUTE_FIELD


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .frame import Frame
    from .layouts import Layout, Value


def mcu_frame(msgtype: int, cleartext: bytes) -> Frame:
    """Return a frame as the MCU hands it to the module: cleartext, on ``03010f``."""
    return make_frame(COMPOSER_SEND, CHANNEL_SESSION, msgtype, cleartext)


@dataclass(frozen=True)
class McuScript:
    """What the MCU answers, by request msgtype, and what it can push.

    A command the layout maps but nothing recorded answers gets an ack whose
    status is the layout's check of its values.

    Attributes:
        replies: Cleartext frames sent in order for a request msgtype.
        pushes: Cleartext frames by push msgtype, sent on demand.
        layout: The product's layout, for command acks and named values.

    """

    replies: Mapping[int, Sequence[Frame]] = field(default_factory=dict)
    pushes: Mapping[int, Frame] = field(default_factory=dict)
    layout: Layout | None = None

    def respond(
        self,
        msgtype: int,
        *,
        request: bytes = b"",
        values: Mapping[int, Mapping[str, Value]] | None = None,
    ) -> list[Frame]:
        """Return the frames answering a request; none for an unknown one.

        Args:
            msgtype: The request's 12-bit message type.
            request: The request's cleartext fields, checked against the layout.
            values: Telemetry values set by name, by frame msgtype.

        """
        frames = list(self.replies.get(msgtype, ()))
        if not frames and self.layout is not None and self.layout.has_command(msgtype):
            status = self.layout.check(msgtype, request)
            ack = bytes([status]) + ROUTE_FIELD.build({"route": BLE_REPLY_ROUTE})
            frames = [mcu_frame(msgtype | RESPONSE, ack)]
        return [self._with_values(frame, values) for frame in frames]

    def push(
        self, msgtype: int, *, values: Mapping[int, Mapping[str, Value]] | None = None
    ) -> Frame:
        """Return the scripted push of ``msgtype``.

        Raises:
            KeyError: If the script has no such push.

        """
        return self._with_values(self.pushes[msgtype], values)

    def _with_values(
        self, frame: Frame, values: Mapping[int, Mapping[str, Value]] | None
    ) -> Frame:
        named = (values or {}).get(frame.cmd.msgtype)
        if not named or self.layout is None:
            return frame
        payload = self.layout.update(frame.cmd.msgtype, frame.payload, named)
        return mcu_frame(frame.cmd.msgtype, payload)
