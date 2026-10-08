# Copyright (c) 2026 Shawn Stricker
"""The device MCU behind the module: recorded cleartext replies and pushes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .frame import CHANNEL_SESSION, COMPOSER_SEND, RESPONSE, make_frame
from .layouts import FIRST_TAG, STATUS_ACCEPTED
from .messages import BLE_REPLY_ROUTE, ROUTE_FIELD
from .summary import SUMMARY_MSGTYPE


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .frame import Frame
    from .layouts import Layout, Value
    from .summary import Summary


def mcu_frame(msgtype: int, cleartext: bytes, channel: int = CHANNEL_SESSION) -> Frame:
    """Return a frame as the MCU hands it to the module: cleartext, on ``0301xx``."""
    return make_frame(COMPOSER_SEND, channel, msgtype, cleartext)


def _rebuilt(frame: Frame, payload: bytes) -> Frame:
    """Return ``frame`` with another payload, on its own channel."""
    return mcu_frame(frame.cmd.msgtype, payload, frame.pattern.channel)


def _with_status(frame: Frame, status: int) -> Frame:
    """Return a reply with its leading status byte set; as is if it has none."""
    payload = frame.payload
    if not payload or payload[0] >= FIRST_TAG:
        return frame
    return _rebuilt(frame, bytes([status]) + payload[1:])


@dataclass(frozen=True)
class McuScript:
    """What the MCU answers, by request msgtype, and what it can push.

    A command the layout maps but nothing recorded answers gets an ack. An MCU
    that rejects answers a setting the layout refuses with ``04``; one that
    doesn't acks it ``00`` and leaves it unapplied.

    Attributes:
        replies: Cleartext frames sent in order for a request msgtype.
        pushes: Cleartext frames by push msgtype, sent on demand.
        layout: The product's layout, for command acks and named values.
        summary: The ``c490`` summary's named fields, where the MCU posts one.
        rejects: The MCU answers a refused setting ``04`` (the Prime MCUs),
            not ``00`` (the C Gen 2 display board's acks).
        channel: The channel the MCU's frames travel on: ``0f``, or ``11`` on
            the Prime Charger 160W.

    """

    replies: Mapping[int, Sequence[Frame]] = field(default_factory=dict)
    pushes: Mapping[int, Frame] = field(default_factory=dict)
    layout: Layout | None = None
    summary: Summary | None = None
    rejects: bool = True
    channel: int = CHANNEL_SESSION

    def respond(
        self,
        msgtype: int,
        *,
        request: bytes = b"",
        values: Mapping[str, Value] | None = None,
    ) -> list[Frame]:
        """Return the frames answering a request; none for an unknown one.

        A listed request answers with its frames, an empty list being silence.
        Where the layout maps the command and the MCU rejects, the reply's
        status is the layout's check of the request's values.

        Args:
            msgtype: The request's 12-bit message type.
            request: The request's cleartext fields, checked against the layout.
            values: The device's telemetry values by name, set in every frame
                that carries them.

        """
        layout = self.layout
        mapped = layout is not None and layout.has_command(msgtype)
        if msgtype in self.replies:
            frames = list(self.replies[msgtype])
        elif mapped:
            ack = bytes([STATUS_ACCEPTED]) + ROUTE_FIELD.build(
                {"route": BLE_REPLY_ROUTE}
            )
            frames = [mcu_frame(msgtype | RESPONSE, ack, self.channel)]
        else:
            return []
        if layout is not None and mapped and self.rejects:
            status = layout.check(msgtype, request)
            frames = [
                _with_status(frame, status)
                if frame.cmd.msgtype == msgtype | RESPONSE
                else frame
                for frame in frames
            ]
        return [self._with_values(frame, values) for frame in frames]

    def push(self, msgtype: int, *, values: Mapping[str, Value] | None = None) -> Frame:
        """Return the scripted push of ``msgtype``.

        Raises:
            KeyError: If the script has no such push.

        """
        return self._with_values(self.pushes[msgtype], values)

    def _with_values(self, frame: Frame, values: Mapping[str, Value] | None) -> Frame:
        msgtype = frame.cmd.msgtype
        if not values:
            return frame
        if self.summary is not None and msgtype == SUMMARY_MSGTYPE:
            payload = self.summary.update(frame.payload, values)
        elif self.layout is not None:
            names = self.layout.names(msgtype)
            named = {name: value for name, value in values.items() if name in names}
            if not named:
                return frame
            payload = self.layout.update(msgtype, frame.payload, named)
        else:
            return frame
        return _rebuilt(frame, payload)
