# Copyright (c) 2026 Shawn Stricker
"""The ``ff09`` frame: codec, fragmenting and reassembly."""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from operator import xor


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
        length = FRAME_OVERHEAD + len(self.payload)
        body = (
            MAGIC
            + length.to_bytes(2, "little")
            + self.pattern
            + self.cmd.to_bytes(2, "big")
            + self.payload
        )
        return body + bytes([checksum(body)])

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
        length = int.from_bytes(data[2:4], "little")
        if length != len(data):
            msg = f"length field {length} != {len(data)} bytes: {data.hex()}"
            raise FrameError(msg)
        if checksum(data[:-1]) != data[-1]:
            msg = f"bad checksum: {data.hex()}"
            raise FrameError(msg)
        return cls(
            pattern=bytes(data[4:7]),
            cmd=int.from_bytes(data[7:9], "big"),
            payload=bytes(data[HEADER_LEN:-1]),
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
        Frame(frame.pattern, cmd, bytes([(index << 4) | total]) + chunk)
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
        index, total = frame.payload[0] >> 4, frame.payload[0] & 0x0F
        if index == 1:
            self._head, self._parts = frame, []
        head = self._head
        if head is None or head.cmd != frame.cmd or index != len(self._parts) + 1:
            self._head, self._parts = None, []
            msg = f"fragment {index}/{total} out of sequence"
            raise FrameError(msg)
        self._parts.append(frame.payload[1:])
        if index < total:
            return None
        whole = Frame(head.pattern, head.cmd & ~FLAG_FRAGMENT, b"".join(self._parts))
        self._head, self._parts = None, []
        return whole
