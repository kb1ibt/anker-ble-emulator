# Copyright (c) 2026 Shawn Stricker
"""The A2345 custom-theme upload as SolixBLE #87 sends it.

docs/solixble-theme-upload.md, ``set_display_custom_theme``: ``4205`` custom,
``4311`` 4, ``4220`` kind 2,
``4221`` in windows of 10 chunks of 192 bytes, ``pad_last_chunk`` on or off.
The device side is PiField prime-a2345-a91b2.md "Theme upload".
"""

from __future__ import annotations

import zlib

import pytest

from anker_ble_emulator import A2345, ManualClock
from anker_ble_emulator.devices.a2345 import Theme
from anker_ble_emulator.parameters import decode_field
from tests.fixtures.prime import (
    CUSTOM_PHOTO,
    advance,
    command,
    prime_client,
    snapshot,
    theme_select,
    upload_chunk,
    upload_prepare,
)


TOKEN = b"owner-token"
#: #87's theme id is the image's CRC32: the bench photo, 8 321 B, 44 chunks of
#: 192 (the last 65 bytes), and the ``4311`` fields naming it.
PHOTO_ID = zlib.crc32(CUSTOM_PHOTO)
COUNT = 44
PHOTO_4311 = "a10131a20504a68adf5a"
#: #87's window and chunk size (``UPLOAD_WINDOW``, ``UPLOAD_CHUNK_SIZE``).
WINDOW = 10
CHUNK = 192
#: ``4205`` custom (``0x80`` | 5), as #87 sends it: id, CRC, default text
#: color, the app's placeholder URL in ``a6``.
CUSTOM_SELECT = theme_select(CUSTOM_PHOTO, PHOTO_ID, theme=0x85)
#: ``4220`` kind 2, as #87 sends it.
CUSTOM_PREPARE = upload_prepare(
    CUSTOM_PHOTO, PHOTO_ID, window=WINDOW, chunk=CHUNK, kind=2
)


@pytest.mark.parametrize(
    ("pad", "theme", "repeat_state"),
    [
        pytest.param(True, 5, "01", id="padded"),
        pytest.param(False, 1, "04", id="unpadded"),
    ],
)
def test_the_87_upload_is_answered_step_by_step(
    pad: bool,  # noqa: FBT001  # the parameter under test
    theme: int,
    repeat_state: str,
) -> None:
    """Every step of #87's upload, with the answer each draws.

    ``4205`` acks, then ``4311`` 4; ``4220`` acks ``00``; each window's last
    chunk draws ``00`` + the next index, the rest nothing; the last draws
    ``4311`` 1, then ``10`` + the count. Padded, the photo draws and the repeat
    select gets ``4311`` 1; unpadded, the draw fails to screen 1 and the repeat
    gets ``4311`` 4, a new upload (bench 2026-10-30).
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)

    selected = command(device, client, 0x205, CUSTOM_SELECT)
    prepared = command(device, client, 0x220, CUSTOM_PREPARE)
    chunks = [
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK, pad=pad))
        for i in range(COUNT)
    ]
    advance(device, client, clock, 0)
    af = decode_field(snapshot(device), "af", Theme)
    repeat = command(device, client, 0x205, CUSTOM_SELECT)

    assert selected == [
        ("4a05", "00a10131"),
        ("4311", f"{PHOTO_4311}a3020104a4020105"),
    ]
    assert prepared == [("4a20", "00a10131")]
    window_ends = {i: f"00a10131a20302{i + 1:02x}00" for i in (9, 19, 29, 39)}
    for i, replies in enumerate(chunks[:-1]):
        expected = [("4a21", window_ends[i])] if i in window_ends else []
        assert replies == expected, i
    assert chunks[-1] == [
        ("4311", f"{PHOTO_4311}a3020101a4020105"),
        ("4a21", f"10a10131a20302{COUNT:02x}00"),
    ]
    assert (af.screen_saver_theme, af.screen_saver_theme_id) == (theme, PHOTO_ID)
    assert repeat == [
        ("4a05", "00a10131"),
        ("4311", f"{PHOTO_4311}a30201{repeat_state}a4020105"),
    ]


def test_a_window_resent_after_its_lost_reply_is_answered_12_from_the_next() -> None:
    """#87 re-sends a window whose reply it missed; the device has moved on.

    Its first nine chunks are dropped, its last slot answers ``12`` + the
    expected index, and #87 resends from there (doc "Theme upload").
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    command(device, client, 0x205, CUSTOM_SELECT)
    command(device, client, 0x220, CUSTOM_PREPARE)
    for i in range(WINDOW):
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK))

    resent = [
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK))
        for i in range(WINDOW)
    ]
    resumed = [
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK))
        for i in range(WINDOW, 2 * WINDOW)
    ]

    assert resent[:-1] == [[]] * (WINDOW - 1)
    assert resent[-1] == [("4a21", "12a10131a203020a00")]
    assert resumed[-1] == [("4a21", "00a10131a203021400")]


def test_a_second_prepare_mid_upload_starts_it_over() -> None:
    """A ``4220`` zeroes the expected index and the CRC (doc "Theme upload").

    After it, chunk 10 is out of order and dropped; from 0 the upload ends
    ``10``, its CRC run over the second pass alone.
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    command(device, client, 0x205, CUSTOM_SELECT)
    command(device, client, 0x220, CUSTOM_PREPARE)
    for i in range(WINDOW):
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK))

    again = command(device, client, 0x220, CUSTOM_PREPARE)
    stale = command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, WINDOW, CHUNK))
    replies = [
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK, pad=True))
        for i in range(COUNT)
    ]

    assert again == [("4a20", "00a10131")]
    assert stale == []
    assert replies[-1][1] == ("4a21", f"10a10131a20302{COUNT:02x}00")


def test_chunks_after_a_finished_upload_need_a_new_prepare() -> None:
    """Only a ``4220`` zeroes the expected index: a re-upload without one is dropped.

    Each window's last slot answers ``12`` + the finished upload's count.
    """
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    command(device, client, 0x205, CUSTOM_SELECT)
    command(device, client, 0x220, CUSTOM_PREPARE)
    for i in range(COUNT):
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK, pad=True))

    again = [
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK, pad=True))
        for i in range(WINDOW)
    ]

    assert again[:-1] == [[]] * (WINDOW - 1)
    assert again[-1] == [("4a21", f"12a10131a20302{COUNT:02x}00")]


@pytest.mark.parametrize("missing", [0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8])
def test_a_prepare_missing_a_field_answers_04(missing: int) -> None:
    """``cmd_ack_param_err``: a ``4220`` without one of ``a2``-``a8`` answers ``04``."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    replies = command(
        device, client, 0x220, [f for f in CUSTOM_PREPARE if f[0] != missing]
    )

    assert replies == [("4a20", "04a10131")]


@pytest.mark.parametrize(
    ("select", "shown_id"),
    [
        pytest.param(True, PHOTO_ID, id="the-staged-id"),
        pytest.param(False, 0, id="none-staged"),
    ],
)
def test_the_stored_record_takes_the_last_staged_id(
    select: bool,  # noqa: FBT001  # the parameter under test
    shown_id: int,
) -> None:
    """The record takes the last ``4205``'s staged id, not ``4220 a3``; 0 from boot."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)
    if select:
        command(device, client, 0x205, CUSTOM_SELECT)
    other = [
        (tag, b"\x03" + (PHOTO_ID ^ 1).to_bytes(4, "little") if tag == 0xA3 else field)
        for tag, field in CUSTOM_PREPARE
    ]
    command(device, client, 0x220, other)

    replies = [
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK, pad=True))
        for i in range(COUNT)
    ]
    af = decode_field(snapshot(device), "af", Theme)

    assert replies[-1][1] == ("4a21", f"10a10131a20302{COUNT:02x}00")
    assert (af.screen_saver_theme, af.screen_saver_theme_id) == (5, shown_id)


@pytest.mark.parametrize("index", [0, 1, 9])
def test_a_chunk_with_no_prepare_since_boot_gets_no_reply(index: int) -> None:
    """The window is 0 then: no window end, no last chunk, so no ``4a21`` at all."""
    device = A2345(clock=ManualClock())
    client = prime_client(device, TOKEN)

    replies = command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, index, CHUNK))

    assert replies == []


def test_a_prepare_crc_off_the_photo_ends_11_and_the_repeat_uploads_again() -> None:
    """The last chunk answers ``11`` + the count with no ``4311``; nothing is stored.

    The theme stays as it was, and the repeat ``4205`` gets ``4311`` 4.
    """
    clock = ManualClock()
    device = A2345(clock=clock)
    client = prime_client(device, TOKEN)
    had = decode_field(snapshot(device), "af", Theme)
    command(device, client, 0x205, CUSTOM_SELECT)
    wrong = [
        (tag, b"\x03" + (PHOTO_ID ^ 1).to_bytes(4, "little") if tag == 0xA4 else field)
        for tag, field in CUSTOM_PREPARE
    ]
    command(device, client, 0x220, wrong)

    replies = [
        command(device, client, 0x221, upload_chunk(CUSTOM_PHOTO, i, CHUNK, pad=True))
        for i in range(COUNT)
    ]
    advance(device, client, clock, 0)
    repeat = command(device, client, 0x205, CUSTOM_SELECT)

    assert replies[-1] == [("4a21", f"11a10131a20302{COUNT:02x}00")]
    assert decode_field(snapshot(device), "af", Theme) == had
    assert repeat[1] == ("4311", f"{PHOTO_4311}a3020104a4020105")
