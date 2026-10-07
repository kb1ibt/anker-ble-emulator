# Copyright (c) 2026 Shawn Stricker
"""Negotiation fields."""

import pytest

from anker_ble_emulator.tlv import FieldError, decode_fields, encode_fields, response


def test_fields_round_trip() -> None:
    fields = [(0xA1, b"\x02"), (0xA2, bytes.fromhex("fd00")), (0xA3, b"")]

    assert decode_fields(encode_fields(fields)) == dict(fields)


def test_a_repeated_tag_keeps_the_last_value() -> None:
    data = bytes.fromhex("a10101a10102a20103")

    assert decode_fields(data) == {0xA1: b"\x02", 0xA2: b"\x03"}


def test_response_leads_with_the_status() -> None:
    assert response(0x09, [(0xA1, bytes.fromhex("1e00"))]).hex() == "09a1021e00"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(b"\xa1", "truncated field header", id="half a header"),
        pytest.param(b"\xa1\x02\x00", "runs past the end", id="short value"),
    ],
)
def test_decode_rejects_truncated_fields(data: bytes, reason: str) -> None:
    with pytest.raises(FieldError, match=reason):
        decode_fields(data)
