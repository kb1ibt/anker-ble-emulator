# Copyright (c) 2026 Shawn Stricker
"""Ciphers and the device's P-256 key pair."""

from __future__ import annotations

from typing import Protocol

from construct import Bytes, BytesInteger, Const, ConstructError, Struct
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher as _AesCipher
from cryptography.hazmat.primitives.ciphers import algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


#: Static AES-128-GCM key, nonce and AAD of the encrypted outer before ECDH.
STATIC_KEY = bytes.fromhex("b8ff7422955d4eb6d554a2c470280559")
#: The full static IV; the encrypted outer's legacy CBC confer uses all 16
#: bytes, its GCM frames only the first 12 as the nonce.
STATIC_IV = bytes.fromhex("6ba3e3f2f3a60f2971ce5d1fd821cfa3")
STATIC_NONCE = STATIC_IV[:12]
#: The session GCM uses the same AAD.
AAD = bytes.fromhex("3322110077665544bbaa9988ffeeddcc")
#: The legacy-mode handshake key when the plain outer's ``0001`` carries no
#: account (``a2``).
DEFAULT_ACCOUNT = b"ANKER_DEFAULT_ACCOUNT_1"

COORDINATE_LEN = 32

#: A P-256 public point as the link carries it: ``X || Y``, big-endian, no ``04``.
POINT_LAYOUT = Struct(
    "x" / BytesInteger(COORDINATE_LEN),
    "y" / BytesInteger(COORDINATE_LEN),
)
#: The same point in SEC1 uncompressed form.
UNCOMPRESSED_POINT_LAYOUT = Struct("prefix" / Const(b"\x04"), "point" / Bytes(64))
#: The session's GCM key material in the 32-byte shared secret.
GCM_KEYS_LAYOUT = Struct("key" / Bytes(16), "nonce" / Bytes(12), Bytes(4))
#: The session's CBC key material in the 32-byte shared secret.
CBC_KEYS_LAYOUT = Struct("key" / Bytes(16), "iv" / Bytes(16))


class Cipher(Protocol):
    """Seals and opens payloads."""

    def encrypt(self, plaintext: bytes) -> bytes:
        """Return the sealed payload."""
        ...

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Return the opened payload."""
        ...


class GcmCipher:
    """AES-128-GCM with a fixed nonce and the static AAD; a 16-byte tag."""

    def __init__(self, key: bytes, nonce: bytes) -> None:
        """Use ``key`` and ``nonce`` for every payload."""
        self._aead = AESGCM(key)
        self._nonce = nonce

    def encrypt(self, plaintext: bytes) -> bytes:
        """Return ciphertext followed by the tag."""
        return self._aead.encrypt(self._nonce, plaintext, AAD)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Return the plaintext of ciphertext followed by the tag.

        Raises:
            cryptography.exceptions.InvalidTag: If the tag doesn't verify.

        """
        return self._aead.decrypt(self._nonce, ciphertext, AAD)


class CbcCipher:
    """AES-128-CBC with a fixed IV and PKCS7 padding."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        """Use ``key`` and ``iv`` for every payload."""
        self._cipher = _AesCipher(algorithms.AES(key), modes.CBC(iv))

    def encrypt(self, plaintext: bytes) -> bytes:
        """Return the padded ciphertext."""
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = self._cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Return the unpadded plaintext.

        Raises:
            ValueError: If the length or padding is wrong.

        """
        decryptor = self._cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()


STATIC_GCM = GcmCipher(STATIC_KEY, STATIC_NONCE)


def session_gcm(shared_secret: bytes) -> GcmCipher:
    """Return the session GCM: key ``ss[:16]``, nonce ``ss[16:28]``."""
    keys = GCM_KEYS_LAYOUT.parse(shared_secret)
    return GcmCipher(keys.key, keys.nonce)


def session_cbc(shared_secret: bytes) -> CbcCipher:
    """Return the session CBC: key ``ss[:16]``, IV ``ss[16:32]``."""
    keys = CBC_KEYS_LAYOUT.parse(shared_secret)
    return CbcCipher(keys.key, keys.iv)


class DeviceKeyPair:
    """The module's P-256 key pair for one key exchange."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey | None = None) -> None:
        """Use ``private_key``, or generate a fresh one."""
        self._private = private_key or ec.generate_private_key(ec.SECP256R1())

    @property
    def public_point(self) -> bytes:
        """The public point as ``X || Y``, 64 bytes, no ``04`` prefix."""
        numbers = self._private.public_key().public_numbers()
        return POINT_LAYOUT.build({"x": numbers.x, "y": numbers.y})

    def shared_secret(self, client_point: bytes) -> bytes:
        """Return the raw ECDH X coordinate with the client's ``X || Y`` point.

        Args:
            client_point: The client's 64-byte public point.

        Raises:
            ValueError: If the point isn't 64 bytes or isn't on the curve.

        """
        try:
            encoded = UNCOMPRESSED_POINT_LAYOUT.build({"point": client_point})
        except ConstructError as error:
            msg = f"client point is {len(client_point)} bytes, not 64"
            raise ValueError(msg) from error
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), encoded)
        return self._private.exchange(ec.ECDH(), peer)
