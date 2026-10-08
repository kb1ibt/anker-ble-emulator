# Copyright (c) 2026 Shawn Stricker
"""The legacy getAesKey confer (``0022``/``4022``), CBC under the handshake context."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anker_ble_emulator.frame import decode, encode, make_frame
from anker_ble_emulator.tlv import decode_fields, encode_fields

from .app_client import NEGOTIATION


if TYPE_CHECKING:
    from anker_ble_emulator.crypto import CbcCipher, Cipher


def confer_request(handshake: CbcCipher, fields: list[tuple[int, bytes]]) -> bytes:
    """Build a legacy ``0022``/``4022``: CBC-sealed under the handshake context."""
    return encode(
        make_frame(
            0x00,
            NEGOTIATION,
            0x022,
            handshake.encrypt(encode_fields(fields)),
            encrypted=True,
        )
    )


def open_confer_reply(cipher: Cipher, data: bytes) -> tuple[int, bytes]:
    """Return a ``0822``/``4822``: its status and the delivered ``a1`` key."""
    plaintext = cipher.decrypt(decode(data).payload)
    return plaintext[0], decode_fields(plaintext[1:])[0xA1]
