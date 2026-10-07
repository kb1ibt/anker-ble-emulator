# Copyright (c) 2026 Shawn Stricker
"""Negotiation message bodies, one typed layout per message.

Requests are read field by field (their fields vary by client); replies are
built from fixed layouts. Every value is typed by its construct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

from construct import (
    Bytes,
    Const,
    Construct,
    ConstructError,
    Container,
    FocusedSeq,
    GreedyBytes,
    Int8ub,
    Int16ul,
    Int32sl,
    Int32ul,
    Optional,
    Prefixed,
    Struct,
)

from .tlv import decode_fields


if TYPE_CHECKING:
    #: A request's typed fields by name (see ``REQUEST_FIELDS``).
    Fields: TypeAlias = Container[Any]


def field(tag: int, value: Construct[Any, Any]) -> Construct[Any, Any]:
    """Return one ``tag len value`` field that parses to and builds from its value."""
    return FocusedSeq(
        "value",
        "tag" / Const(tag, Int8ub),
        "value" / Prefixed(Int8ub, value),
    )


#: Request fields by msgtype: tag to (name, value layout).
REQUEST_FIELDS: dict[int, dict[int, tuple[str, Construct[Any, Any]]]] = {
    0x001: {0xA2: ("account", GreedyBytes)},
    0x003: {0xA3: ("app_encrypt", Int8ub), 0xA4: ("mtu", Int16ul)},
    0x005: {0xA5: ("method", Int8ub), 0xA6: ("auth_method", Int8ub)},
    0x021: {0xA1: ("point", GreedyBytes)},
    0x022: {
        0xA1: ("time", Int32ul),
        0xA3: ("utc_offset", Int32sl),
        0xA5: ("tz", GreedyBytes),
    },
    0x023: {0xA2: ("account", GreedyBytes)},
    0x027: {0xA2: ("token", GreedyBytes)},
}


def parse_request(msgtype: int, payload: bytes) -> Fields:
    """Return a request's known fields by name; absent or malformed ones are None.

    Args:
        msgtype: The request's msgtype.
        payload: The request's plaintext fields.

    Raises:
        FieldError: If the payload isn't a whole run of fields.

    """
    known = REQUEST_FIELDS.get(msgtype, {})
    request = Container({name: None for name, _ in known.values()})
    for tag, value in decode_fields(payload).items():
        if tag not in known:
            continue
        name, layout = known[tag]
        try:
            request[name] = layout.parse(value)
        except ConstructError:
            request[name] = None
    return request


#: A reply with only its status byte.
STATUS_REPLY = Struct("status" / Int8ub)

#: ``0801``.
CONNECT_REPLY = Struct("status" / Int8ub, "connect_type" / field(0xA1, Int8ub))

#: ``0803``: capability bits, MTU, and the auth method where the build reports it.
CAPABILITY_REPLY = Struct(
    "status" / Int8ub,
    "base_capability" / field(0xA1, Int8ub),
    "mtu" / field(0xA2, Int16ul),
    "advanced_capability" / field(0xA3, Int8ub),
    "stage2_a4" / field(0xA4, Int8ub),
    "auth_method" / Optional(field(0xA5, Int8ub)),
)

#: ``0829``: the serial is absent when the module has none.
DEVICE_INFO_REPLY = Struct(
    "status" / Int8ub,
    "info_type" / field(0xA1, Int8ub),
    "chip" / field(0xA2, GreedyBytes),
    "lib_version" / field(0xA3, GreedyBytes),
    "serial" / Optional(field(0xA4, GreedyBytes)),
    "mac" / field(0xA5, Bytes(6)),
)

#: ``0821``: the device's public point, ``X || Y``.
PUBLIC_KEY_REPLY = Struct("status" / Int8ub, "point" / field(0xA1, Bytes(64)))

#: ``0823``: the bound serial echoed back.
BIND_REPLY = Struct("status" / Int8ub, "serial" / Optional(field(0xA1, GreedyBytes)))

#: ``0827 09``: confirmation pending; the window in seconds.
AUTH_PENDING_REPLY = Struct("status" / Int8ub, "auth_timeout" / field(0xA1, Int16ul))

#: ``0830``: module firmware, device firmware, then the three component names.
VERSION_REPLY = Struct(
    "status" / Int8ub,
    "module" / field(0xA1, GreedyBytes),
    "device" / field(0xA2, GreedyBytes),
    "model" / field(0xA3, GreedyBytes),
    "mcu" / field(0xA4, GreedyBytes),
    "esp32" / field(0xA5, GreedyBytes),
)
