# Copyright (c) 2026 Shawn Stricker
"""The ff09 frame codec, fragmenting and reassembly."""

import pytest

from anker_ble_emulator.frame import (
    FLAG_FRAGMENT,
    Frame,
    FrameError,
    Reassembler,
    fragment,
)


#: The fixed stage-1 reply every encrypted-outer device sends.
FRAME_4801 = bytes.fromhex(
    "ff091e000300014801ab273ed3e27270c3f4d676ac7d69a00572793732a6"
)


def test_decode_reads_header_and_payload() -> None:
    frame = Frame.decode(FRAME_4801)

    assert frame.pattern == bytes.fromhex("030001")
    assert frame.cmd == 0x4801
    assert frame.msgtype == 0x801
    assert frame.encrypted
    assert not frame.fragmented
    assert frame.channel == 0x01
    assert frame.payload == bytes.fromhex("ab273ed3e27270c3f4d676ac7d69a00572793732")


def test_encode_round_trips_a_real_frame() -> None:
    assert Frame.decode(FRAME_4801).encode() == FRAME_4801


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(b"\xff\x09\x0a\x00", "not an ff09 frame", id="too short"),
        pytest.param(
            bytes.fromhex("fe09") + FRAME_4801[2:], "not an ff09 frame", id="magic"
        ),
        pytest.param(FRAME_4801[:-2] + FRAME_4801[-1:], "length field", id="length"),
        pytest.param(FRAME_4801[:-1] + b"\x00", "bad checksum", id="checksum"),
    ],
)
def test_decode_rejects_malformed_bytes(data: bytes, reason: str) -> None:
    with pytest.raises(FrameError, match=reason):
        Frame.decode(data)


def test_fragment_leaves_a_frame_that_fits() -> None:
    frame = Frame(bytes.fromhex("03010f"), 0x4900, bytes(243))

    assert fragment(frame, 253) == [frame]


def test_fragment_fills_the_cap_then_closes_with_a_shorter_frame() -> None:
    frame = Frame(bytes.fromhex("03010f"), 0x4900, bytes(range(256)) + bytes(131))

    parts = fragment(frame, 253)

    assert [len(part.encode()) for part in parts] == [253, 156]
    assert [part.cmd for part in parts] == [0xC900, 0xC900]
    assert [part.payload[0] for part in parts] == [0x12, 0x22]


def test_fragment_rejects_more_than_fifteen_parts() -> None:
    frame = Frame(bytes.fromhex("03010f"), 0x4900, bytes(16 * 242))

    with pytest.raises(FrameError, match="fragments"):
        fragment(frame, 253)


def test_reassembler_joins_a_fragment_run() -> None:
    frame = Frame(bytes.fromhex("03010f"), 0x4490, bytes(range(256)) * 3)
    reassembler = Reassembler()

    results = [reassembler.feed(part) for part in fragment(frame, 253)]

    assert results[:-1] == [None, None, None]
    assert results[-1] == frame


def test_reassembler_passes_an_unfragmented_frame_through() -> None:
    frame = Frame(bytes.fromhex("030001"), 0x4801, b"\x00")

    assert Reassembler().feed(frame) == frame


@pytest.mark.parametrize(
    ("accepted", "rejected"),
    [
        pytest.param([], 0x22, id="starts mid-run"),
        pytest.param([0x13], 0x33, id="skips a fragment"),
    ],
)
def test_reassembler_rejects_out_of_sequence_fragments(
    accepted: list[int], rejected: int
) -> None:
    reassembler = Reassembler()
    for info in accepted:
        reassembler.feed(
            Frame(bytes.fromhex("03010f"), FLAG_FRAGMENT | 0x4900, bytes([info, 0]))
        )
    bad = Frame(bytes.fromhex("03010f"), FLAG_FRAGMENT | 0x4900, bytes([rejected, 0]))

    with pytest.raises(FrameError, match="out of sequence"):
        reassembler.feed(bad)


def test_reassembler_rejects_a_fragment_without_its_index() -> None:
    frame = Frame(bytes.fromhex("03010f"), FLAG_FRAGMENT | 0x4900, b"")

    with pytest.raises(FrameError, match="index byte"):
        Reassembler().feed(frame)
