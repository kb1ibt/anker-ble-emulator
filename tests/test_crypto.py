# Copyright (c) 2026 Shawn Stricker
"""Ciphers, the static key's known answers, and the device key pair."""

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec

from anker_ble_emulator.crypto import (
    STATIC_GCM,
    CbcCipher,
    DeviceKeyPair,
    session_cbc,
    session_gcm,
)
from anker_ble_emulator.frame import decode
from tests.fixtures.client import AppClient


FRAME_4801 = bytes.fromhex(
    "ff091e000300014801ab273ed3e27270c3f4d676ac7d69a00572793732a6"
)
FRAME_4803 = bytes.fromhex(
    "ff092b000300014803ab273ed04438d4b25db54c6d4a6ec3d481f5ad58ff7cc2be8bc8369fd98c0b914e03"
)


@pytest.mark.parametrize(
    ("frame", "plaintext"),
    [
        pytest.param(FRAME_4801, "00a10101", id="4801"),
        pytest.param(FRAME_4803, "00a10102a202fd00a30144a40101a50102", id="4803"),
    ],
)
def test_static_gcm_matches_the_recorded_handshake(
    frame: bytes, plaintext: str
) -> None:
    payload = decode(frame).payload

    assert STATIC_GCM.decrypt(payload).hex() == plaintext
    assert STATIC_GCM.encrypt(bytes.fromhex(plaintext)) == payload


def test_gcm_rejects_a_tampered_payload() -> None:
    sealed = bytearray(STATIC_GCM.encrypt(b"\x00\xa1\x01\x01"))
    sealed[0] ^= 1

    with pytest.raises(InvalidTag):
        STATIC_GCM.decrypt(bytes(sealed))


def test_cbc_round_trips_with_padding() -> None:
    cipher = CbcCipher(bytes(range(16)), bytes(range(16, 32)))

    sealed = cipher.encrypt(b"\x00\xa1\x01\x31")

    assert len(sealed) == 16
    assert cipher.decrypt(sealed) == b"\x00\xa1\x01\x31"


def test_session_ciphers_slice_the_shared_secret() -> None:
    secret = bytes(range(32))

    assert session_gcm(secret).encrypt(b"x") == session_gcm(secret).encrypt(b"x")
    assert session_cbc(secret).decrypt(session_cbc(secret).encrypt(b"x")) == b"x"


def test_device_point_is_x_and_y_without_the_prefix() -> None:
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()

    point = DeviceKeyPair(private).public_point

    assert point == numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")


def test_shared_secret_is_the_raw_x_both_sides_compute() -> None:
    device = DeviceKeyPair()
    client = AppClient()

    secret = device.shared_secret(client.public_point)

    assert secret == client.install(device.public_point)
    assert len(secret) == 32


@pytest.mark.parametrize(
    "point",
    [pytest.param(bytes(63), id="short"), pytest.param(bytes(64), id="off the curve")],
)
def test_shared_secret_rejects_a_bad_client_point(point: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011  # cryptography's message varies
        DeviceKeyPair().shared_secret(point)
