# Copyright (c) 2026 Shawn Stricker
"""The ff09 frame codec, fragmenting and reassembly."""

import pytest

from anker_ble_emulator.frame import (
    COMMAND_LAYOUT,
    FRAGMENT_LAYOUT,
    PATTERN_LAYOUT,
    FrameError,
    Reassembler,
    decode,
    encode,
    fragment,
    make_frame,
)


#: The fixed stage-1 reply every encrypted-outer device sends.
FRAME_4801 = bytes.fromhex(
    "ff091e000300014801ab273ed3e27270c3f4d676ac7d69a00572793732a6"
)


def test_decode_reads_header_and_payload() -> None:
    frame = decode(FRAME_4801)

    assert PATTERN_LAYOUT.build(frame.pattern).hex() == "030001"
    assert COMMAND_LAYOUT.build(frame.cmd).hex() == "4801"
    assert frame.pattern.composer == 0x00
    assert frame.pattern.channel == 0x01
    assert frame.cmd.msgtype == 0x801
    assert frame.cmd.encrypted
    assert not frame.cmd.fragmented
    assert frame.payload == bytes.fromhex("ab273ed3e27270c3f4d676ac7d69a00572793732")


def test_encode_round_trips_a_real_frame() -> None:
    assert encode(decode(FRAME_4801)) == FRAME_4801


def test_make_frame_matches_the_decoded_frame() -> None:
    frame = make_frame(
        0x00,
        0x01,
        0x801,
        bytes.fromhex("ab273ed3e27270c3f4d676ac7d69a00572793732"),
        encrypted=True,
    )

    assert frame == decode(FRAME_4801)
    assert encode(frame) == FRAME_4801


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(b"\xff\x09\x0a\x00", "not an ff09 frame", id="too short"),
        pytest.param(
            bytes.fromhex("fe09") + FRAME_4801[2:], "not an ff09 frame", id="magic"
        ),
        pytest.param(
            FRAME_4801[:4] + b"\x04" + FRAME_4801[5:-1] + b"\xa2",
            "not an ff09 frame",
            id="pattern family",
        ),
        pytest.param(FRAME_4801[:-2] + FRAME_4801[-1:], "length field", id="length"),
        pytest.param(FRAME_4801[:-1] + b"\x00", "bad checksum", id="checksum"),
    ],
)
def test_decode_rejects_malformed_bytes(data: bytes, reason: str) -> None:
    with pytest.raises(FrameError, match=reason):
        decode(data)


def test_fragment_leaves_a_frame_that_fits() -> None:
    frame = make_frame(0x01, 0x0F, 0x900, bytes(243), encrypted=True)

    assert fragment(frame, 253) == [frame]


def test_fragment_fills_the_cap_then_closes_with_a_shorter_frame() -> None:
    frame = make_frame(
        0x01, 0x0F, 0x900, bytes(range(256)) + bytes(131), encrypted=True
    )

    parts = fragment(frame, 253)

    assert [len(encode(part)) for part in parts] == [253, 156]
    assert [COMMAND_LAYOUT.build(part.cmd).hex() for part in parts] == ["c900"] * 2
    infos = [FRAGMENT_LAYOUT.parse(part.payload).info for part in parts]
    assert [(info.index, info.total) for info in infos] == [(1, 2), (2, 2)]


def test_fragment_rejects_more_than_fifteen_parts() -> None:
    frame = make_frame(0x01, 0x0F, 0x900, bytes(16 * 242), encrypted=True)

    with pytest.raises(FrameError, match="fragments"):
        fragment(frame, 253)


def test_reassembler_joins_a_fragment_run() -> None:
    frame = make_frame(0x01, 0x0F, 0x490, bytes(range(256)) * 3, encrypted=True)
    reassembler = Reassembler()

    results = [reassembler.feed(part) for part in fragment(frame, 253)]

    assert results[:-1] == [None, None, None]
    assert results[-1] == frame


def test_reassembler_passes_an_unfragmented_frame_through() -> None:
    frame = make_frame(0x00, 0x01, 0x801, b"\x00", encrypted=True)

    assert Reassembler().feed(frame) == frame


@pytest.mark.parametrize(
    ("accepted", "rejected"),
    [
        pytest.param([], (2, 2), id="starts mid-run"),
        pytest.param([(1, 3)], (3, 3), id="skips a fragment"),
    ],
)
def test_reassembler_rejects_out_of_sequence_fragments(
    accepted: list[tuple[int, int]], rejected: tuple[int, int]
) -> None:
    reassembler = Reassembler()
    for index, total in accepted:
        reassembler.feed(
            make_frame(
                0x01,
                0x0F,
                0x900,
                FRAGMENT_LAYOUT.build(
                    {"info": {"index": index, "total": total}, "chunk": b"\x00"}
                ),
                encrypted=True,
                fragmented=True,
            ),
        )
    index, total = rejected
    bad = make_frame(
        0x01,
        0x0F,
        0x900,
        FRAGMENT_LAYOUT.build({"info": {"index": index, "total": total}, "chunk": b""}),
        encrypted=True,
        fragmented=True,
    )

    with pytest.raises(FrameError, match="out of sequence"):
        reassembler.feed(bad)


def test_reassembler_rejects_a_fragment_without_its_index() -> None:
    frame = make_frame(0x01, 0x0F, 0x900, b"", encrypted=True, fragmented=True)

    with pytest.raises(FrameError, match="index byte"):
        Reassembler().feed(frame)
