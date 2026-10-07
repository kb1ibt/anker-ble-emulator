# Copyright (c) 2026 Shawn Stricker
"""Negotiation fields: untyped ``tag len value``, tags counting from ``a1``."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable


class FieldError(ValueError):
    """A field run that ends inside a field."""


def encode_fields(fields: Iterable[tuple[int, bytes]]) -> bytes:
    """Return ``tag len value`` for each field, in order.

    Args:
        fields: ``(tag, value)`` pairs.

    """
    return b"".join(bytes([tag, len(value)]) + value for tag, value in fields)


def decode_fields(data: bytes) -> dict[int, bytes]:
    """Parse a run of ``tag len value`` fields.

    Args:
        data: The fields, with no status byte in front.

    Returns:
        Values by tag.

    Raises:
        FieldError: If a field's length runs past the end.

    """
    fields: dict[int, bytes] = {}
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            msg = f"truncated field header at {offset}: {data.hex()}"
            raise FieldError(msg)
        tag, length = data[offset], data[offset + 1]
        end = offset + 2 + length
        if end > len(data):
            msg = f"field {tag:02x} runs past the end: {data.hex()}"
            raise FieldError(msg)
        fields[tag] = data[offset + 2 : end]
        offset = end
    return fields


def response(status: int, fields: Iterable[tuple[int, bytes]] = ()) -> bytes:
    """Return a response payload: the status byte, then the fields.

    Args:
        status: The status byte (``00`` success).
        fields: ``(tag, value)`` pairs.

    """
    return bytes([status]) + encode_fields(fields)
