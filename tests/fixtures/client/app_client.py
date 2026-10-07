# Copyright (c) 2026 Shawn Stricker
"""The client half of the handshake, as the vendor app sends it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from construct import Bytes, Const, GreedyBytes, Int8ub, Struct
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from anker_ble_emulator.crypto import STATIC_GCM, Cipher, session_cbc, session_gcm
from anker_ble_emulator.frame import (
    CHANNEL_NEGOTIATION,
    CHANNEL_SESSION,
    COMMAND_LAYOUT,
    COMPOSER_REPLY,
    PATTERN_LAYOUT,
    RESPONSE,
    Reassembler,
    decode,
    encode,
    make_frame,
)
from anker_ble_emulator.tlv import decode_fields, encode_fields


if TYPE_CHECKING:
    from anker_ble_emulator.frame import Frame


NEGOTIATION = CHANNEL_NEGOTIATION
SESSION = CHANNEL_SESSION
TIMESTAMP = bytes.fromhex("1a69a76a")

#: SEC1 uncompressed point, as cryptography reads and writes it.
_SEC1_POINT = Struct("prefix" / Const(b"\x04"), "point" / Bytes(64))
#: A negotiation reply: status byte, then its fields.
_REPLY_BODY = Struct("status" / Int8ub, "fields" / GreedyBytes)


def cmd_hex(frame: Frame) -> str:
    """Return a frame's cmd as wire hex (``4900``)."""
    return COMMAND_LAYOUT.build(frame.cmd).hex()


def pattern_hex(frame: Frame) -> str:
    """Return a frame's pattern as wire hex (``03010f``)."""
    return PATTERN_LAYOUT.build(frame.pattern).hex()


@dataclass(frozen=True)
class Reply:
    """An opened reply: the frame as received, its status and fields."""

    frame: Frame
    status: int | None
    fields: dict[int, bytes]
    plaintext: bytes


class AppClient:
    """Builds requests and opens replies; holds the client key and session."""

    def __init__(self, *, encrypted_outer: bool = True) -> None:
        """Start before any key exchange.

        Args:
            encrypted_outer: Open with ``4001`` under the static key, else ``0001``.

        """
        self.encrypted_outer = encrypted_outer
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.session: Cipher | None = None
        self._reassembler = Reassembler()

    @property
    def public_point(self) -> bytes:
        """The client point as ``X || Y``."""
        uncompressed = self.private_key.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        return bytes(_SEC1_POINT.parse(uncompressed).point)

    def request(
        self,
        msgtype: int,
        fields: list[tuple[int, bytes]],
        channel: int = NEGOTIATION,
    ) -> bytes:
        """Return one request frame, sealed for the current state."""
        plaintext = encode_fields(fields)
        cipher = self._cipher()
        if cipher is None:
            return encode(make_frame(COMPOSER_REPLY, channel, msgtype, plaintext))
        return encode(
            make_frame(
                COMPOSER_REPLY,
                channel,
                msgtype,
                cipher.encrypt(plaintext),
                encrypted=True,
            ),
        )

    def open(self, data: bytes) -> Reply | None:
        """Open one notification; None while a fragment run is incomplete."""
        frame = self._reassembler.feed(decode(data))
        if frame is None:
            return None
        plaintext = bytes(frame.payload)
        if frame.cmd.encrypted:
            plaintext = (self.session or STATIC_GCM).decrypt(plaintext)
        if frame.cmd.msgtype & RESPONSE and frame.pattern.channel == NEGOTIATION:
            body = _REPLY_BODY.parse(plaintext)
            return Reply(frame, body.status, decode_fields(body.fields), plaintext)
        return Reply(frame, None, {}, plaintext)

    def install(self, device_point: bytes) -> bytes:
        """Derive the session from the device's ``0821 a1``; return the secret."""
        peer = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), _SEC1_POINT.build({"point": device_point})
        )
        secret = self.private_key.exchange(ec.ECDH(), peer)
        self.session = (
            session_gcm(secret) if self.encrypted_outer else session_cbc(secret)
        )
        return secret

    def _cipher(self) -> Cipher | None:
        if self.session is not None:
            return self.session
        return STATIC_GCM if self.encrypted_outer else None
