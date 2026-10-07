# Copyright (c) 2026 Shawn Stricker
"""The device MCU behind the module: recorded cleartext replies and pushes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class McuFrame:
    """One frame the MCU sends: its msgtype and cleartext payload."""

    msgtype: int
    cleartext: bytes


@dataclass(frozen=True)
class McuScript:
    """What the MCU answers, by request msgtype, and what it can push.

    Attributes:
        replies: Frames sent in order for a request msgtype (flags removed).
        pushes: Cleartext by push msgtype, sent on demand.

    """

    replies: Mapping[int, Sequence[McuFrame]] = field(default_factory=dict)
    pushes: Mapping[int, bytes] = field(default_factory=dict)

    def respond(self, msgtype: int) -> list[McuFrame]:
        """Return the frames answering a request; none for an unscripted one.

        Args:
            msgtype: The request's 12-bit message type.

        """
        return list(self.replies.get(msgtype, ()))

    def push(self, msgtype: int) -> McuFrame:
        """Return the scripted push of ``msgtype``.

        Raises:
            KeyError: If the script has no such push.

        """
        return McuFrame(msgtype, self.pushes[msgtype])
