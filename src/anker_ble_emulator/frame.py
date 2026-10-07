# Copyright (c) 2026 Shawn Stricker
"""The ``ff09`` frame: codec, fragmenting and reassembly."""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from operator import xor

from construct import (
    BitStruct,
    Bytes,
    Checksum,
    ChecksumError,
    Const,
    GreedyBytes,
    Int8ub,
    Int16ub,
    Int16ul,
    Nibble,
    RawCopy,
    Rebuild,
    Struct,
    this,
)


MAGIC = b"\xff\x09"
#: Magic (2) + length (2) + pattern (3) + cmd (2).
HEADER_LEN = 9
#: Header plus the trailing checksum byte.
FRAME_OVERHEAD = HEADER_LEN + 1

FLAG_FRAGMENT = 0x8000
FLAG_ENCRYPTED = 0x4000
RESPONSE = 0x0800
MSGTYPE_MASK = 0x0FFF
#: Fragment index and total are nibbles.
MAX_FRAGMENTS = 0x0F


class FrameError(ValueError):
    """Bytes that are not a well-formed frame, or a broken fragment run."""


def checksum(data: bytes) -> int:
    """Return the XOR of every byte."""
    return reduce(xor, data, 0)


#: ``ff09 | len (u16 LE, whole frame) | pattern | cmd (u16 BE) | payload | xor``.
FRAME_LAYOUT = Struct(
    "body"
    / RawCopy(
        Struct(
            "magic" / Const(MAGIC),
            "length"
            / Rebuild(Int16ul, lambda this: FRAME_OVERHEAD + len(this.payload)),
            "pattern" / Bytes(3),
            "cmd" / Int16ub,
            "payload" / Bytes(lambda this: this.length - FRAME_OVERHEAD),
        ),
    ),
    "checksum" / Checksum(Int8ub, checksum, this.body.data),
)

#: A fragment's payload: ``index << 4 | total`` (index from 1), then its chunk.
FRAGMENT_LAYOUT = Struct(
    "info" / BitStruct("index" / Nibble, "total" / Nibble),
    "chunk" / GreedyBytes,
)


@dataclass(frozen=True)
class Frame:
    """One frame: pattern ``03 <composer> <channel>``, 16-bit cmd, payload."""

    pattern: bytes
    cmd: int
    payload: bytes = b""

    @property
    def msgtype(self) -> int:
        """The 12-bit message type, link flags removed."""
        return self.cmd & MSGTYPE_MASK

    @property
    def encrypted(self) -> bool:
        """Whether the ``0x40`` link flag is set."""
        return bool(self.cmd & FLAG_ENCRYPTED)

    @property
    def fragmented(self) -> bool:
        """Whether the ``0x80`` fragment flag is set."""
        return bool(self.cmd & FLAG_FRAGMENT)

    @property
    def channel(self) -> int:
        """The module's dispatch channel, the pattern's last byte."""
        return self.pattern[2]

    def encode(self) -> bytes:
        """Return the frame as wire bytes, length and checksum included."""
        return FRAME_LAYOUT.build(
            {
                "body": {
                    "value": {
                        "pattern": self.pattern,
                        "cmd": self.cmd,
                        "payload": self.payload,
                    },
                },
                "checksum": None,
            },
        )

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        """Parse wire bytes into a frame.

        Args:
            data: One whole frame.

        Returns:
            The parsed ``Frame``.

        Raises:
            FrameError: If the magic, length or checksum is wrong.

        """
        if len(data) < FRAME_OVERHEAD or data[:2] != MAGIC:
            msg = f"not an ff09 frame: {data.hex()}"
            raise FrameError(msg)
        length = Int16ul.parse(data[2:4])
        if length != len(data):
            msg = f"length field {length} != {len(data)} bytes: {data.hex()}"
            raise FrameError(msg)
        try:
            body = FRAME_LAYOUT.parse(data).body.value
        except ChecksumError as error:
            msg = f"bad checksum: {data.hex()}"
            raise FrameError(msg) from error
        return cls(
            pattern=bytes(body.pattern),
            cmd=int(body.cmd),
            payload=bytes(body.payload),
        )


def fragment(frame: Frame, cap: int) -> list[Frame]:
    """Split a frame longer than ``cap`` bytes into fragment frames.

    Every fragment but the last fills ``cap``; each payload starts with
    ``index << 4 | total``, index counting from 1.

    Args:
        frame: The whole frame.
        cap: The largest frame the link carries (ATT MTU - 3).

    Returns:
        ``[frame]`` if it fits, else the fragments in order.

    Raises:
        FrameError: If the frame needs more than 15 fragments.

    """
    if FRAME_OVERHEAD + len(frame.payload) <= cap:
        return [frame]
    room = cap - FRAME_OVERHEAD - 1
    chunks = [frame.payload[i : i + room] for i in range(0, len(frame.payload), room)]
    total = len(chunks)
    if total > MAX_FRAGMENTS:
        msg = f"{len(frame.payload)} B needs {total} fragments at cap {cap}"
        raise FrameError(msg)
    cmd = frame.cmd | FLAG_FRAGMENT
    return [
        Frame(
            frame.pattern,
            cmd,
            FRAGMENT_LAYOUT.build(
                {"info": {"index": index, "total": total}, "chunk": chunk}
            ),
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
        if not frame.fragmented:
            return frame
        if not frame.payload:
            msg = "fragment without an index byte"
            raise FrameError(msg)
        part = FRAGMENT_LAYOUT.parse(frame.payload)
        index, total = int(part.info.index), int(part.info.total)
        if index == 1:
            self._head, self._parts = frame, []
        head = self._head
        if head is None or head.cmd != frame.cmd or index != len(self._parts) + 1:
            self._head, self._parts = None, []
            msg = f"fragment {index}/{total} out of sequence"
            raise FrameError(msg)
        self._parts.append(bytes(part.chunk))
        if index < total:
            return None
        whole = Frame(head.pattern, head.cmd & ~FLAG_FRAGMENT, b"".join(self._parts))
        self._head, self._parts = None, []
        return whole
