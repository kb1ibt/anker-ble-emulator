# Copyright (c) 2026 Shawn Stricker
"""Negotiation fields: untyped ``tag len value``, tags counting from ``a1``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from construct import (
    Bytes,
    ConstructError,
    GreedyBytes,
    GreedyRange,
    Int8ub,
    Prefixed,
    Struct,
    Tell,
    Terminated,
    this,
)


if TYPE_CHECKING:
    from collections.abc import Iterable

#: One field: tag, then a 1-byte length and the value.
FIELD_LAYOUT = Struct("tag" / Int8ub, "value" / Prefixed(Int8ub, GreedyBytes))
#: Fields back to back, as many as fit.
FIELDS_LAYOUT = GreedyRange(FIELD_LAYOUT)
#: A whole run of fields, nothing after them.
WHOLE_FIELDS = Struct("fields" / FIELDS_LAYOUT, Terminated)
#: As many fields as fit, where they stopped, and what's left.
FIELD_RUN = Struct("fields" / FIELDS_LAYOUT, "consumed" / Tell, "rest" / GreedyBytes)
FIELD_HEADER = Struct("tag" / Int8ub, "length" / Int8ub)
#: One field and where its value starts.
PLACED_FIELD = Struct(
    "tag" / Int8ub, "length" / Int8ub, "start" / Tell, "value" / Bytes(this.length)
)


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
    run = FIELD_RUN.parse(data)
    if not run.rest:
        return {int(field.tag): bytes(field.value) for field in run.fields}
    try:
        FIELD_HEADER.parse(run.rest)
    except ConstructError as error:
        msg = f"truncated field header at {run.consumed}: {data.hex()}"
        raise FieldError(msg) from error
    msg = f"the field at {run.consumed} runs past the end: {data.hex()}"
    raise FieldError(msg)


def field_starts(data: bytes) -> dict[int, int]:
    """Return where each field's value starts in ``data``, by tag.

    Args:
        data: A whole run of fields, as ``decode_fields`` accepts it.

    """
    placed = GreedyRange(PLACED_FIELD).parse(data)
    return {int(field.tag): int(field.start) for field in placed}
