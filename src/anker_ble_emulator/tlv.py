# Copyright (c) 2026 Shawn Stricker
"""Negotiation fields: untyped ``tag len value``, tags counting from ``a1``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from construct import GreedyBytes, GreedyRange, Int8ub, Prefixed, Struct


if TYPE_CHECKING:
    from collections.abc import Iterable

#: One field: tag, then a 1-byte length and the value.
FIELD_LAYOUT = Struct("tag" / Int8ub, "value" / Prefixed(Int8ub, GreedyBytes))
#: Fields back to back, as many as fit.
FIELDS_LAYOUT = GreedyRange(FIELD_LAYOUT)
#: A response payload: the status byte, then the fields.
RESPONSE_LAYOUT = Struct("status" / Int8ub, "fields" / FIELDS_LAYOUT)
FIELD_HEADER_LEN = 2


class FieldError(ValueError):
    """A field run that ends inside a field."""


def encode_fields(fields: Iterable[tuple[int, bytes]]) -> bytes:
    """Return ``tag len value`` for each field, in order.

    Args:
        fields: ``(tag, value)`` pairs.

    """
    return FIELDS_LAYOUT.build([{"tag": tag, "value": value} for tag, value in fields])


def decode_fields(data: bytes) -> dict[int, bytes]:
    """Parse a run of ``tag len value`` fields.

    Args:
        data: The fields, with no status byte in front.

    Returns:
        Values by tag.

    Raises:
        FieldError: If a field's length runs past the end.

    """
    parsed = FIELDS_LAYOUT.parse(data)
    consumed = sum(FIELD_HEADER_LEN + len(field.value) for field in parsed)
    if consumed == len(data):
        return {int(field.tag): bytes(field.value) for field in parsed}
    if len(data) - consumed < FIELD_HEADER_LEN:
        msg = f"truncated field header at {consumed}: {data.hex()}"
        raise FieldError(msg)
    msg = f"field {data[consumed]:02x} runs past the end: {data.hex()}"
    raise FieldError(msg)


def response(status: int, fields: Iterable[tuple[int, bytes]] = ()) -> bytes:
    """Return a response payload: the status byte, then the fields.

    Args:
        status: The status byte (``00`` success).
        fields: ``(tag, value)`` pairs.

    """
    return RESPONSE_LAYOUT.build(
        {
            "status": status,
            "fields": [{"tag": tag, "value": value} for tag, value in fields],
        },
    )
