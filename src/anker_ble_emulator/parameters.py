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

from dataclasses import dataclass
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
    #: (request, state before, state after) -> a push's fields between its
    #: ``a1`` and ``fe``, or None when the change draws no push.
    PushBuilder: TypeAlias = Callable[
        ["ParameterDict", Mapping[str, Value], Mapping[str, Value]],
        "ParameterDict | None",
    ]
    #: (request, state before the command) -> the further values it sets.
    SideEffect: TypeAlias = Callable[
        ["ParameterDict", Mapping[str, Value]], dict[str, Value]
    ]
    #: (request, the device's state) -> a read's reply fields after its ``a1``.
    ReadReply: TypeAlias = Callable[
        ["ParameterDict", Mapping[str, Value]], "ParameterDict"
    ]

#: TLV tags start at 0xa1; a lower first byte is a reply's status.
STATUS_LIMIT = 0xA1
#: What a field's codec parses to.
Parsed = TypeVar("Parsed")
#: A typed sub-struct at one tag: its codec, its type byte, and each field's
#: zero value. A field named with a leading ``_`` is carried as recorded and
#: can't be set by name.
FieldSpec: TypeAlias = "tuple[Construct[Container[Any], Any], int, Mapping[str, Value]]"


def leaf_zeros(*names: str, **overrides: Value) -> dict[str, Value]:
    """Return each field's zero value: 0 unless overridden (a bytes field)."""
    return {**dict.fromkeys(names, 0), **overrides}


def struct_zeros(codec: Construct[Container[Any], Any]) -> dict[str, Value]:
    """Return each named field of ``codec`` at zero; a ``_`` field as zero bytes."""
    return {
        sc.name: bytes(sc.sizeof()) if sc.name.startswith("_") else 0
        for sc in codec.subcons  # type: ignore[attr-defined]  # Struct's own subcons
        if sc.name
    }


def one_field(name: str, codec: Construct[Any, Any], type_byte: int) -> FieldSpec:
    """Return the spec of a tag whose whole value is the one field ``name``."""
    return Struct(name / codec), type_byte, leaf_zeros(name)


def flat(
    raw: Construct[Container[Any], Any], groups: Mapping[str, tuple[str, ...]]
) -> Construct[Container[Any], Any]:
    """Promote each packed-bit group's members to the struct's flat top level."""

    def decode(obj: Container[Any], _: object) -> Container[Any]:
        out = Container(obj)
        for group in groups:
            out.update(out.pop(group))
        return out

    def encode(obj: Container[Any], _: object) -> Container[Any]:
        out = Container(obj)
        for group, names in groups.items():
            out[group] = Container({name: out.pop(name) for name in names})
        return out

    return ExprAdapter(raw, decoder=decode, encoder=encode)


@dataclass(frozen=True)
class FieldGroup:
    """One codec at several tags; instance ``n``'s field ``f`` is named ``n_f``.

    Attributes:
        codec: Each instance's struct.
        zeros: Each field's zero value.
        tags: Instance name -> its tag.
        type_byte: The tags' type byte.

    """

    codec: Construct[Container[Any], Any]
    zeros: Mapping[str, Value]
    tags: Mapping[str, str]
    type_byte: int = 0x04

    def names(self) -> set[str]:
        """Return every instance's field names."""
        return {f"{instance}_{field}" for instance in self.tags for field in self.zeros}

    def changed(self, instance: str, values: Mapping[str, Value]) -> dict[str, Value]:
        """Return the fields of ``instance`` that ``values`` sets, by field name."""
        return {
            field: values[f"{instance}_{field}"]
            for field in self.zeros
            if f"{instance}_{field}" in values
        }


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


def container(tag: str, type_byte: int, value: bytes) -> ParameterContainer:
    """Return one field: ``tag``, its type byte and its value bytes."""
    return ParameterContainer(key=bytes.fromhex(tag), type=type_byte, value=value)


def echo(*tags: str) -> PushBuilder:
    """Return a push builder that carries the request's ``tags``, unconditionally."""

    def echoed(
        request: ParameterDict,
        before: Mapping[str, Value],  # noqa: ARG001  # sent whatever changed
        after: Mapping[str, Value],  # noqa: ARG001
    ) -> ParameterDict:
        return ParameterDict({tag: request[tag] for tag in tags if tag in request})

    return echoed


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
    #: The request tags a command's accepted reply carries back after its ``a1``.
    ECHOES: ClassVar[Mapping[int, tuple[str, ...]]] = MappingProxyType({})
    #: The commands whose handler answers an accepted request with nothing.
    SILENT: ClassVar[frozenset[int]] = frozenset()
    #: The builders of a read's reply fields after its ``a1``, where its handler
    #: answers with its own data rather than the request's.
    READ_REPLIES: ClassVar[Mapping[int, ReadReply]] = MappingProxyType({})
    #: The further values an accepted command sets, from its request and the
    #: device's state before it.
    SIDE_EFFECTS: ClassVar[Mapping[int, SideEffect]] = MappingProxyType({})
    #: The pushes an accepted command can draw, in send order: (push msgtype,
    #: its builder).
    FOLLOW_UPS: ClassVar[Mapping[int, tuple[tuple[int, PushBuilder], ...]]] = (
        MappingProxyType({})
    )

    def has_command(self, msgtype: int) -> bool:
        """Whether a command maps to ``msgtype``."""
        return msgtype in self.COMMANDS

    def reply_fields(
        self, msgtype: int, plaintext: bytes, state: Mapping[str, Value]
    ) -> bytes | None:
        """Return the fields an accepted reply carries after its ``a1``, encoded.

        A read's own data from the device's state, else the request's echoed
        fields; None where the handler sends no reply to an accepted request.
        """
        if msgtype in self.SILENT:
            return None
        read = self.READ_REPLIES.get(msgtype)
        if read is not None:
            return Parameters.build(read(Parameters.parse(plaintext), state))
        tags = self.ECHOES.get(msgtype, ())
        if not tags:
            return b""
        request = Parameters.parse(plaintext)
        return Parameters.build(
            ParameterDict({tag: request[tag] for tag in tags if tag in request})
        )

    def follow_ups(
        self,
        msgtype: int,
        plaintext: bytes,
        before: Mapping[str, Value],
        after: Mapping[str, Value],
    ) -> list[tuple[int, bytes]]:
        """Return the (msgtype, payload) pushes an accepted command's change draws.

        Each push is its route, the fields its builder returns, then the
        request's clock; a builder returning None sends nothing.
        """
        pushes = self.FOLLOW_UPS.get(msgtype, ())
        if not pushes:
            return []
        request = Parameters.parse(plaintext)
        route = ParameterContainer(
            key=b"\xa1", type=None, value=bytes([BLE_REPLY_ROUTE])
        )
        clock = {"fe": request["fe"]} if "fe" in request else {}
        frames = []
        for push, build in pushes:
            fields = build(request, before, after)
            if fields is not None:
                payload = ParameterDict({"a1": route} | fields | clock)
                frames.append((push, Parameters.build(payload)))
        return frames

    def check(self, msgtype: int, plaintext: bytes) -> int:
        """Return the status a request's values earn: accepted, or rejected."""
        accepted = self._validated(msgtype, plaintext) is not None
        return STATUS_ACCEPTED if accepted else STATUS_REJECTED

    def state_changes(
        self, msgtype: int, plaintext: bytes, state: Mapping[str, Value]
    ) -> dict[str, Value]:
        """Return the values an accepted command sets, its side effects included."""
        changes = self._validated(msgtype, plaintext)
        if changes is None:
            return {}
        effect = self.SIDE_EFFECTS.get(msgtype)
        if effect is not None:
            changes |= effect(Parameters.parse(plaintext), state)
        return changes

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


class TableLayout(StructFieldLayout, CommandDispatchLayout):
    """A recorded device's telemetry, named by per-message tables.

    The device supplies ``FIELDS`` (msgtype -> tag -> ``FieldSpec``) and
    ``GROUPS`` (msgtype -> its ``FieldGroup``s). Its frames are always
    recorded: ``build`` only checks a value against its field, and ``update``
    writes named values into a recorded frame.
    """

    FIELDS: ClassVar[Mapping[int, Mapping[str, FieldSpec]]] = MappingProxyType({})
    GROUPS: ClassVar[Mapping[int, tuple[FieldGroup, ...]]] = MappingProxyType({})

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names ``set_values()`` can reach in ``msgtype``."""
        leaves = {
            name
            for _, _, zeros in self.FIELDS.get(msgtype, {}).values()
            for name in zeros
            if not name.startswith("_")
        }
        for group in self.GROUPS.get(msgtype, ()):
            leaves |= group.names()
        return frozenset(leaves)

    def locate(self, name: str) -> tuple[int, ...]:
        """Return the messages with a named field matching ``name``."""
        msgtypes = dict.fromkeys([*self.FIELDS, *self.GROUPS])
        return tuple(msgtype for msgtype in msgtypes if name in self.names(msgtype))

    def implied(
        self,
        values: Mapping[str, Value],  # noqa: ARG002  # no value implies another here
    ) -> dict[str, Value]:
        """Return the further values that setting ``values`` sets: none."""
        return {}

    def read(self, msgtype: int, payload: bytes) -> dict[str, Value]:
        """Return the named values ``payload`` carries."""
        fields = self.FIELDS.get(msgtype, {})
        groups = self.GROUPS.get(msgtype, ())
        if not fields and not groups:
            return {}
        params = Parameters.parse(payload)
        values: dict[str, Value] = {}
        for tag, (codec, _, zeros) in fields.items():
            parsed = self._decode_field(params, tag, codec)
            values.update(
                {name: parsed[name] for name in zeros if not name.startswith("_")}
            )
        for group in groups:
            for instance, tag in group.tags.items():
                parsed = self._decode_field(params, tag, group.codec)
                values.update({f"{instance}_{f}": parsed[f] for f in group.zeros})
        return values

    def build(self, msgtype: int, values: Mapping[str, Value] | None = None) -> bytes:
        """Check each named value against its field; the frame itself is recorded.

        Raises:
            ConstructError: If a value doesn't fit its field.

        """
        for name, value in (values or {}).items():
            for codec, _, zeros in self.FIELDS.get(msgtype, {}).values():
                if name in zeros:
                    codec.build({**zeros, name: value})
            for group in self.GROUPS.get(msgtype, ()):
                for instance in group.tags:
                    field = name.removeprefix(f"{instance}_")
                    if field != name and field in group.zeros:
                        group.codec.build({**group.zeros, field: value})
        return b""

    def update(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> bytes:
        """Return ``payload`` with the named fields set; the rest as recorded."""
        fields = self.FIELDS.get(msgtype, {})
        groups = self.GROUPS.get(msgtype, ())
        if not fields and not groups:
            return payload
        params = Parameters.parse(payload)
        for tag, (codec, type_byte, zeros) in fields.items():
            changed = {name: values[name] for name in zeros if name in values}
            if changed:
                self._encode_field(params, tag, codec, type_byte, **changed)
        for group in groups:
            for instance, tag in group.tags.items():
                changed = group.changed(instance, values)
                if changed:
                    self._encode_field(
                        params, tag, group.codec, group.type_byte, **changed
                    )
        self.finish(msgtype, params, values)
        return Parameters.build(params)

    def finish(
        self, msgtype: int, params: ParameterDict, values: Mapping[str, Value]
    ) -> None:
        """Reshape the frame once its fields are written; nothing by default."""


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

    def implied(
        self,
        values: Mapping[str, Value],  # noqa: ARG002  # no value implies another here
    ) -> dict[str, Value]:
        """Return the further values that setting ``values`` sets: none."""
        return {}

    def read(self, msgtype: int, payload: bytes) -> dict[str, Value]:
        """Return the named values ``payload`` carries."""
        telemetry = self.TELEMETRY.get(msgtype, ())
        if not telemetry:
            return {}
        params = Parameters.parse(payload)
        return {
            field.name: decode_field(params, field.tag, field.codec)
            for field in telemetry
        }

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
