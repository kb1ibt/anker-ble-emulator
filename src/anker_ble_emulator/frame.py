# Copyright (c) 2026 Shawn Stricker
"""The ``ff09`` frame: layouts, codec, fragmenting and reassembly.

A frame is a construct ``Container``: ``pattern`` (``family``, ``composer``,
``channel``), ``cmd`` (``fragmented``, ``encrypted``, ``reserved``,
``msgtype``) and ``payload``.
"""

from __future__ import annotations

from functools import reduce
from operator import xor
from typing import TYPE_CHECKING, Any, TypeAlias

from construct import (
    BitsInteger,
    BitStruct,
    Bytes,
    Checksum,
    ChecksumError,
    Const,
    ConstError,
    Container,
    Flag,
    GreedyBytes,
    Int8ub,
    Int16ul,
    Nibble,
    RawCopy,
    Rebuild,
    Struct,
    this,
)


if TYPE_CHECKING:
    #: A frame ``Container`` (see the module docstring).
    Frame: TypeAlias = Container[Any]


MAGIC = b"\xff\x09"
#: Magic (2) + length (2) + pattern (3) + cmd (2) + checksum (1).
FRAME_OVERHEAD = 10
#: The msgtype bit that marks a response.
RESPONSE = 0x800
#: Fragment index and total are nibbles.
MAX_FRAGMENTS = 0x0F
PATTERN_FAMILY = 0x03
#: ``pattern.composer``: the reply composer echoes the client's ``00``; frames
#: the module composes on its own carry ``01``.
COMPOSER_REPLY = 0x00
COMPOSER_SEND = 0x01
#: ``pattern.channel``: the module's dispatch key.
CHANNEL_NEGOTIATION = 0x01
CHANNEL_SESSION = 0x0F
CHANNEL_APP = 0x11


class FrameError(ValueError):
    """Bytes that are not a well-formed frame, or a broken fragment run."""


def checksum(data: bytes) -> int:
    """Return the XOR of every byte."""
    return reduce(xor, data, 0)


#: ``03 <composer> <channel>``.
PATTERN_LAYOUT = Struct(
    "family" / Const(PATTERN_FAMILY, Int8ub),
    "composer" / Int8ub,
    "channel" / Int8ub,
)

#: Link flags in the high bits, then the 12-bit message type (``0x800`` = response).
COMMAND_LAYOUT = BitStruct(
    "fragmented" / Flag,
    "encrypted" / Flag,
    "reserved" / BitsInteger(2),
    "msgtype" / BitsInteger(12),
)

#: ``ff09 | len (u16 LE, whole frame) | pattern | cmd | payload | xor``.
FRAME_LAYOUT = Struct(
    "body"
    / RawCopy(
        Struct(
            "magic" / Const(MAGIC),
            "length"
            / Rebuild(Int16ul, lambda this: FRAME_OVERHEAD + len(this.payload)),
            "pattern" / PATTERN_LAYOUT,
            "cmd" / COMMAND_LAYOUT,
            "payload" / Bytes(lambda this: this.length - FRAME_OVERHEAD),
        ),
    ),
    "checksum" / Checksum(Int8ub, checksum, this.body.data),
)

#: The magic and length that open every frame.
HEADER_LAYOUT = Struct("magic" / Const(MAGIC), "length" / Int16ul)

#: A fragment's payload: ``index << 4 | total`` (index from 1), then its chunk.
FRAGMENT_LAYOUT = Struct(
    "info" / BitStruct("index" / Nibble, "total" / Nibble),
    "chunk" / GreedyBytes,
)


def make_frame(  # noqa: PLR0913  # pattern, cmd and payload fields
    composer: int,
    channel: int,
    msgtype: int,
    payload: bytes = b"",
    *,
    encrypted: bool = False,
    fragmented: bool = False,
) -> Frame:
    """Return a frame ``Container``.

    Args:
        composer: ``pattern.composer``.
        channel: ``pattern.channel``.
        msgtype: The 12-bit message type.
        payload: The payload as sent (ciphertext if ``encrypted``).
        encrypted: The ``0x40`` link flag.
        fragmented: The ``0x80`` fragment flag.

    """
    return Container(
        pattern=Container(family=PATTERN_FAMILY, composer=composer, channel=channel),
        cmd=Container(
            fragmented=fragmented,
            encrypted=encrypted,
            reserved=0,
            msgtype=msgtype,
        ),
        payload=payload,
    )


def reply_frame(request: Frame, payload: bytes) -> Frame:
    """Return the reply to ``request``: its pattern and flags, msgtype ``| 0x800``."""
    return make_frame(
        request.pattern.composer,
        request.pattern.channel,
        request.cmd.msgtype | RESPONSE,
        payload,
        encrypted=request.cmd.encrypted,
    )


def encode(frame: Frame) -> bytes:
    """Return the frame as wire bytes, length and checksum included."""
    return FRAME_LAYOUT.build({"body": {"value": frame}, "checksum": None})


def decode(data: bytes) -> Frame:
    """Parse wire bytes into a frame ``Container``.

    Raises:
        FrameError: If the magic, length or checksum is wrong.

    """
    if len(data) < FRAME_OVERHEAD:
        msg = f"not an ff09 frame: {data.hex()}"
        raise FrameError(msg)
    try:
        header = HEADER_LAYOUT.parse(data)
    except ConstError as error:
        msg = f"not an ff09 frame: {data.hex()}"
        raise FrameError(msg) from error
    if header.length != len(data):
        msg = f"length field {header.length} != {len(data)} bytes: {data.hex()}"
        raise FrameError(msg)
    try:
        body = FRAME_LAYOUT.parse(data).body.value
    except ChecksumError as error:
        msg = f"bad checksum: {data.hex()}"
        raise FrameError(msg) from error
    except ConstError as error:
        msg = f"not an ff09 frame: {data.hex()}"
        raise FrameError(msg) from error
    return make_frame(
        body.pattern.composer,
        body.pattern.channel,
        body.cmd.msgtype,
        body.payload,
        encrypted=body.cmd.encrypted,
        fragmented=body.cmd.fragmented,
    )


def fragment(frame: Frame, cap: int) -> list[Frame]:
    """Split a frame longer than ``cap`` bytes into fragment frames.

    Every fragment but the last fills ``cap``.

    Args:
        frame: The whole frame.
        cap: The largest frame the link carries (ATT MTU - 3).

    Returns:
        ``[frame]`` if it fits, else the fragments in order.

    Raises:
        FrameError: If the frame needs more than 15 fragments.

    """
    payload = frame.payload
    if FRAME_OVERHEAD + len(payload) <= cap:
        return [frame]
    room = cap - FRAME_OVERHEAD - 1
    chunks = [payload[i : i + room] for i in range(0, len(payload), room)]
    total = len(chunks)
    if total > MAX_FRAGMENTS:
        msg = f"{len(payload)} B needs {total} fragments at cap {cap}"
        raise FrameError(msg)
    return [
        make_frame(
            frame.pattern.composer,
            frame.pattern.channel,
            frame.cmd.msgtype,
            FRAGMENT_LAYOUT.build(
                {"info": {"index": index, "total": total}, "chunk": chunk}
            ),
            encrypted=frame.cmd.encrypted,
            fragmented=True,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


class Reassembler:
    """Joins a run of fragment frames back into one frame."""

    def __init__(self) -> None:
        """Start with no run in progress."""
        self._head: Frame | None = None
        self._parts: list[bytes] = []

    def feed(self, frame: Frame) -> Frame | None:
        """Take one frame; return a whole frame once one is complete.

        Args:
            frame: A frame as received.

        Returns:
            The frame itself if unfragmented, the joined frame (fragment flag
            cleared) on the last fragment, else None.

        Raises:
            FrameError: If a fragment arrives out of order or from another run.

        """
        if not frame.cmd.fragmented:
            return frame
        if not frame.payload:
            msg = "fragment without an index byte"
            raise FrameError(msg)
        part = FRAGMENT_LAYOUT.parse(frame.payload)
        index, total = part.info.index, part.info.total
        if index == 1:
            self._head, self._parts = frame, []
        head = self._head
        if head is None or head.cmd != frame.cmd or index != len(self._parts) + 1:
            self._head, self._parts = None, []
            msg = f"fragment {index}/{total} out of sequence"
            raise FrameError(msg)
        self._parts.append(part.chunk)
        if index < total:
            return None
        payload = b"".join(self._parts)
        self._head, self._parts = None, []
        return make_frame(
            head.pattern.composer,
            head.pattern.channel,
            head.cmd.msgtype,
            payload,
            encrypted=head.cmd.encrypted,
        )
