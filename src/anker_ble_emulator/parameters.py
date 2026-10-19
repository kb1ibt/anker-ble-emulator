# Copyright (c) 2026 Shawn Stricker
"""A message's TLV fields, generically: SolixBLE's own Parameter/Parameters shape.

Ported from SolixBLE's constructs.py (flip-dots, the upstream client this
package emulates): a field's type byte is optional, its value stays raw
bytes, and ``Parameters`` gives dict-by-hex-tag access with a status prefix.
A device decodes and validates its own specific fields on top of this,
directly in its own module -- this layer knows nothing about what any tag
means.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, TypeAlias, TypeVar

from construct import (
    Bytes,
    Construct,
    ConstructError,
    Container,
    ExprAdapter,
    ExprValidator,
    GreedyRange,
    Hex,
    HexDump,
    If,
    Int8ul,
    Optional,
    Rebuild,
    Struct,
)

from .layouts import STATUS_ACCEPTED, STATUS_REJECTED
from .messages import BLE_REPLY_ROUTE


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .layouts import ScalarField, Value

    #: A command's request fields: tag -> (the name its value is returned
    #: under, the codec of its value bytes).
    RequestFields: TypeAlias = Mapping[str, tuple[str, Construct[Any, Any]]]

#: TLV tags start at 0xa1; a lower first byte is a reply's status.
STATUS_LIMIT = 0xA1
#: What a field's codec parses to.
Parsed = TypeVar("Parsed")


class ParameterContainer(dict[str, Any]):
    """One field's key, optional type, and value bytes."""

    @property
    def value_legacy(self) -> bytes:
        """The type byte (if any) prepended to the value bytes."""
        type_byte = bytes([self["type"]]) if self.get("type") is not None else b""
        return type_byte + (self.get("value") or b"")


Parameter = ExprAdapter(
    Struct(
        "key" / Hex(Bytes(1)),
        "length"
        / Rebuild(
            Int8ul,
            lambda p: (
                (1 if p.get("type") is not None else 0) + len(p.get("value") or b"")
            ),
        ),
        "type"
        / If(
            lambda p: (
                p.get("type") is not None  # construct's own context flag
                if p._building  # noqa: SLF001
                else p.length > 1
            ),
            Int8ul,
        ),
        "value"
        / If(
            lambda p: (p.length - (1 if p.type is not None else 0)) > 0,
            HexDump(Bytes(lambda p: p.length - (1 if p.type is not None else 0))),
        ),
    ),
    decoder=lambda obj, _: ParameterContainer(obj),
    encoder=lambda obj, _: obj,
)
"""One field: ``<key 1B> <length 1B> <type 1B?> <value nB>``, type optional."""


class ParameterDict(dict[str, ParameterContainer]):
    """Parameters by hex tag, with the optional leading status byte."""

    def __init__(
        self,
        *args: Mapping[str, ParameterContainer],
        prefix: bytes | None = None,
        **kwargs: ParameterContainer,
    ) -> None:
        """Build the dict; ``prefix`` is the status byte, None for a push."""
        super().__init__(*args, **kwargs)
        self.prefix = prefix


_STATUS_PREFIX: Construct[bytes, bytes] = ExprValidator(
    Bytes(1), lambda obj, _: obj[0] < STATUS_LIMIT
)

Parameters = ExprAdapter(
    Struct(
        "prefix"
        / If(
            lambda this: this._parsing or this.prefix is not None,  # noqa: SLF001  # construct's own context flag
            Optional(_STATUS_PREFIX),
        ),
        "parameters" / GreedyRange(Parameter),
    ),
    decoder=lambda obj, _: ParameterDict(
        {p["key"].hex(): p for p in obj.parameters}, prefix=obj.prefix
    ),
    encoder=lambda ps, _: {
        "prefix": getattr(ps, "prefix", None),
        "parameters": list(ps.values()) if isinstance(ps, dict) else ps,
    },
)
"""A payload's parameters: an optional status byte, then a run of fields."""


class StructFieldLayout:
    """Decode/encode a fixed-shape, type-prefixed sub-field at a tag.

    Mix into a product's own ``LayoutProtocol`` implementation for its typed
    sub-structs (a port block, a timer record, ...): the product supplies
    the construct codec and type byte per tag; this supplies the
    parse/mutate/rebuild mechanics, shared across every field shaped this way.
    """

    @staticmethod
    def _decode_field(
        params: ParameterDict, tag: str, codec: Construct[Container[Any], Any]
    ) -> Container[Any]:
        """Decode a typed sub-field's current value."""
        return decode_field(params, tag, codec)

    @classmethod
    def _encode_field(
        cls,
        params: ParameterDict,
        tag: str,
        codec: Construct[Container[Any], Any],
        type_byte: int,
        **values: Value,
    ) -> None:
        """Encode a typed sub-field's value back in place."""
        current = cls._decode_field(params, tag, codec)
        current.update(values)
        params[tag] = ParameterContainer(
            key=bytes.fromhex(tag), type=type_byte, value=codec.build(current)
        )


class CommandDispatchLayout:
    """Dispatch a command by msgtype to a per-command validator.

    Mix into a product's own ``LayoutProtocol`` implementation; the product
    supplies its own ``COMMANDS`` class attribute: msgtype -> a validator
    taking the request's parsed fields and returning the telemetry values
    an accepted request sets (``{}`` if it sets none), or ``None`` if refused.
    """

    COMMANDS: ClassVar[
        Mapping[int, Callable[[ParameterDict], dict[str, Value] | None]]
    ] = MappingProxyType({})

    def has_command(self, msgtype: int) -> bool:
        """Whether a command maps to ``msgtype``."""
        return msgtype in self.COMMANDS

    def check(self, msgtype: int, plaintext: bytes) -> int:
        """Return the status a request's values earn: accepted, or rejected."""
        accepted = self._validated(msgtype, plaintext) is not None
        return STATUS_ACCEPTED if accepted else STATUS_REJECTED

    def state_changes(self, msgtype: int, plaintext: bytes) -> dict[str, Value]:
        """Return the telemetry values an accepted command sets, by field name."""
        return self._validated(msgtype, plaintext) or {}

    def _validated(self, msgtype: int, plaintext: bytes) -> dict[str, Value] | None:
        """Return what ``msgtype``'s validator accepts; None if refused or unknown.

        A value that fails its codec (a ``ConstructError``) refuses the request.
        """
        validate = self.COMMANDS.get(msgtype)
        if validate is None:
            return None
        try:
            return validate(Parameters.parse(plaintext))
        except ConstructError:
            return None


class ScalarFieldLayout:
    """Name, build and update a product's scalar telemetry fields.

    Mix into a product's own ``LayoutProtocol`` implementation; the product
    supplies ``TELEMETRY``: msgtype -> its fields, in wire order. For a
    message that is a flat run of single-value fields (the Solarbank
    ``0405`` shape), not the nested port/schedule structs ``StructFieldLayout``
    covers.
    """

    TELEMETRY: ClassVar[Mapping[int, tuple[ScalarField, ...]]] = MappingProxyType({})

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the named fields of ``msgtype``; none if it has none."""
        return frozenset(field.name for field in self.TELEMETRY.get(msgtype, ()))

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages with a field called ``name``."""
        return tuple(m for m in self.TELEMETRY if name in self.names(m))

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Return ``msgtype`` with every field zero unless set, after the BLE route.

        Raises:
            ConstructError: If a value doesn't fit its field.

        """
        given = values or {}
        params = ParameterDict(
            {
                "a1": ParameterContainer(
                    key=b"\xa1", type=None, value=bytes([BLE_REPLY_ROUTE])
                )
            }
        )
        for field in self.TELEMETRY.get(msgtype, ()):
            params[field.tag] = _scalar_container(
                field, given.get(field.name, field.zero)
            )
        return Parameters.build(params)

    def update(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> bytes:
        """Return ``payload`` with the named fields set; the rest as they were."""
        params = Parameters.parse(payload)
        for field in self.TELEMETRY.get(msgtype, ()):
            if field.name in values:
                params[field.tag] = _scalar_container(field, values[field.name])
        return Parameters.build(params)


def _scalar_container(field: ScalarField, value: Value) -> ParameterContainer:
    return ParameterContainer(
        key=bytes.fromhex(field.tag),
        type=field.type_byte,
        value=field.codec.build(value),
    )


def decode_field(
    params: ParameterDict, tag: str, codec: Construct[Parsed, Any]
) -> Parsed:
    """Return ``tag``'s value bytes, after its type byte, decoded by ``codec``.

    Raises:
        ConstructError: If the value doesn't parse or fails the codec's validator.

    """
    return codec.parse(params[tag].get("value") or b"")


def decode_request(request: ParameterDict, fields: RequestFields) -> dict[str, Any]:
    """Return each of ``fields`` the request carries, decoded, by name.

    Args:
        request: The command's parsed parameters.
        fields: Tag -> (the name its value is returned under, the value's codec).
            A tag the request leaves out is skipped.

    Raises:
        ConstructError: If a carried value doesn't parse or fails its validator.

    """
    return {
        name: decode_field(request, tag, codec)
        for tag, (name, codec) in fields.items()
        if tag in request
    }
