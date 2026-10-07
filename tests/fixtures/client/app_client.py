# Copyright (c) 2026 Shawn Stricker
"""The client half of the handshake, as the vendor app sends it."""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from anker_ble_emulator.crypto import STATIC_GCM, Cipher, session_cbc, session_gcm
from anker_ble_emulator.frame import FLAG_ENCRYPTED, Frame, Reassembler
from anker_ble_emulator.tlv import decode_fields, encode_fields


NEGOTIATION = bytes.fromhex("030001")
SESSION = bytes.fromhex("03000f")
TIMESTAMP = bytes.fromhex("1a69a76a")


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
        return uncompressed[1:]

    def request(
        self,
        msgtype: int,
        fields: list[tuple[int, bytes]],
        pattern: bytes = NEGOTIATION,
    ) -> bytes:
        """Return one request frame, sealed for the current state."""
        plaintext = encode_fields(fields)
        cipher = self._cipher()
        if cipher is None:
            return Frame(pattern, msgtype, plaintext).encode()
        return Frame(
            pattern, FLAG_ENCRYPTED | msgtype, cipher.encrypt(plaintext)
        ).encode()

    def open(self, data: bytes) -> Reply | None:
        """Open one notification; None while a fragment run is incomplete."""
        frame = self._reassembler.feed(Frame.decode(data))
        if frame is None:
            return None
        plaintext = frame.payload
        if frame.encrypted:
            cipher = self.session or STATIC_GCM
            plaintext = cipher.decrypt(frame.payload)
        if frame.msgtype & 0x800 and frame.channel == NEGOTIATION[2]:
            return Reply(frame, plaintext[0], decode_fields(plaintext[1:]), plaintext)
        return Reply(frame, None, {}, plaintext)

    def install(self, device_point: bytes) -> bytes:
        """Derive the session from the device's ``0821 a1``; return the secret."""
        peer = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), b"\x04" + device_point
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
