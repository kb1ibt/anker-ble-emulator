# Copyright (c) 2026 Shawn Stricker
"""The device MCU behind the module: recorded cleartext replies and pushes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .frame import CHANNEL_SESSION, COMPOSER_SEND, RESPONSE, make_frame
from .messages import (
    BLE_REPLY_ROUTE,
    CLOUD_REPLY_ROUTE,
    ROUTE_APP,
    ROUTE_BLE,
    with_route,
)


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .frame import Frame


def mcu_frame(msgtype: int, cleartext: bytes) -> Frame:
    """Return a frame as the MCU hands it to the module: cleartext, on ``03010f``."""
    return make_frame(COMPOSER_SEND, CHANNEL_SESSION, msgtype, cleartext)


@dataclass(frozen=True)
class McuScript:
    """What the MCU answers, by request msgtype, and what it can push.

    The MCU routes each frame by its own ``a1``: a reply to an ack op goes back
    by the request's source; a reply to a push-rule op, and every push, goes to
    BLE, or to MQTT ``param_info`` while the MCU reports to the cloud.

    Attributes:
        replies: Cleartext frames sent in order for a request msgtype.
        pushes: Cleartext frames by push msgtype, sent on demand.
        push_route_requests: Requests whose replies follow the push rule.

    """

    replies: Mapping[int, Sequence[Frame]] = field(default_factory=dict)
    pushes: Mapping[int, Frame] = field(default_factory=dict)
    push_route_requests: frozenset[int] = frozenset()

    def respond(
        self, msgtype: int, source: int = ROUTE_BLE, *, cloud: bool = False
    ) -> list[Frame]:
        """Return the frames answering a request, routed; none for an unscripted one.

        Args:
            msgtype: The request's 12-bit message type.
            source: The request's route source nibble.
            cloud: The MCU sends push-rule frames to the cloud.

        """
        reply_route = (
            _push_route(cloud=cloud)
            if msgtype in self.push_route_requests
            else ROUTE_APP << 4 | source
        )
        return [
            _routed(
                frame,
                reply_route
                if frame.cmd.msgtype == msgtype | RESPONSE
                else _push_route(cloud=cloud),
            )
            for frame in self.replies.get(msgtype, ())
        ]

    def push(self, msgtype: int, *, cloud: bool = False) -> Frame:
        """Return the scripted push of ``msgtype``, routed.

        Raises:
            KeyError: If the script has no such push.

        """
        return _routed(self.pushes[msgtype], _push_route(cloud=cloud))


def _push_route(*, cloud: bool) -> int:
    return CLOUD_REPLY_ROUTE if cloud else BLE_REPLY_ROUTE


def _routed(frame: Frame, route: int) -> Frame:
    return mcu_frame(frame.cmd.msgtype, with_route(frame.payload, route))
