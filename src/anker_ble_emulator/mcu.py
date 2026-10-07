# Copyright (c) 2026 Shawn Stricker
"""The device MCU behind the module: recorded cleartext replies and pushes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .frame import CHANNEL_SESSION, COMPOSER_SEND, make_frame


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .frame import Frame


def mcu_frame(msgtype: int, cleartext: bytes) -> Frame:
    """Return a frame as the MCU hands it to the module: cleartext, on ``03010f``."""
    return make_frame(COMPOSER_SEND, CHANNEL_SESSION, msgtype, cleartext)


@dataclass(frozen=True)
class McuScript:
    """What the MCU answers, by request msgtype, and what it can push.

    Attributes:
        replies: Cleartext frames sent in order for a request msgtype.
        pushes: Cleartext frames by push msgtype, sent on demand.

    """

    replies: Mapping[int, Sequence[Frame]] = field(default_factory=dict)
    pushes: Mapping[int, Frame] = field(default_factory=dict)

    def respond(self, msgtype: int) -> list[Frame]:
        """Return the frames answering a request; none for an unscripted one.

        Args:
            msgtype: The request's 12-bit message type.

        """
        return list(self.replies.get(msgtype, ()))

    def push(self, msgtype: int) -> Frame:
        """Return the scripted push of ``msgtype``.

        Raises:
            KeyError: If the script has no such push.

        """
        return self.pushes[msgtype]
