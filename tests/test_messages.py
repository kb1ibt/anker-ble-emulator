# Copyright (c) 2026 Shawn Stricker
"""Typed negotiation messages against recorded plaintext."""

from __future__ import annotations

from typing import Any

import pytest
from construct import Construct, Container

from anker_ble_emulator.messages import (
    AUTH_PENDING_REPLY,
    CAPABILITY_REPLY,
    CONNECT_REPLY,
    DEVICE_INFO_REPLY,
    PUBLIC_KEY_REPLY,
    STATUS_REPLY,
    parse_request,
    payload_route,
    with_route,
)


def test_capability_reply_matches_the_recorded_stage_two() -> None:
    payload = CAPABILITY_REPLY.build(
        {
            "status": 0,
            "base_capability": 0x02,
            "mtu": 253,
            "advanced_capability": 0x44,
            "stage2_a4": 0x01,
            "auth_method": 0x02,
        },
    )

    assert payload.hex() == "00a10102a202fd00a30144a40101a50102"
    assert CAPABILITY_REPLY.parse(payload).mtu == 253


def test_capability_reply_without_an_auth_method() -> None:
    payload = CAPABILITY_REPLY.build(
        {
            "status": 0,
            "base_capability": 0x02,
            "mtu": 253,
            "advanced_capability": 0x04,
            "stage2_a4": 0x01,
            "auth_method": None,
        },
    )

    assert payload.hex() == "00a10102a202fd00a30104a40101"
    assert CAPABILITY_REPLY.parse(payload).auth_method is None


def test_device_info_reply_round_trips() -> None:
    values = {
        "status": 0,
        "info_type": 0x03,
        "chip": b"ESP32",
        "lib_version": b"0.0.0.3",
        "serial": b"APCDKKE0000000001",
        "mac": bytes.fromhex("aa12deadbeef"),
    }

    payload = DEVICE_INFO_REPLY.build(values)

    assert payload.hex() == (
        "00a10103a2054553503332a307302e302e302e33"
        "a411" + b"APCDKKE0000000001".hex() + "a506aa12deadbeef"
    )
    assert DEVICE_INFO_REPLY.parse(payload) == Container(values)


def test_device_info_reply_omits_a_missing_serial() -> None:
    payload = DEVICE_INFO_REPLY.build(
        {
            "status": 0,
            "info_type": 0x03,
            "chip": b"ESP32",
            "lib_version": b"0.0.0.3",
            "serial": None,
            "mac": bytes(6),
        },
    )

    assert payload.hex() == "00a10103a2054553503332a307302e302e302e33a506000000000000"
    assert DEVICE_INFO_REPLY.parse(payload).serial is None


@pytest.mark.parametrize(
    ("layout", "values", "expected"),
    [
        pytest.param(STATUS_REPLY, {"status": 0x01}, "01", id="status"),
        pytest.param(
            CONNECT_REPLY, {"status": 0, "connect_type": 1}, "00a10101", id="0801"
        ),
        pytest.param(
            AUTH_PENDING_REPLY,
            {"status": 0x09, "auth_timeout": 30},
            "09a1021e00",
            id="0827 09",
        ),
        pytest.param(
            PUBLIC_KEY_REPLY,
            {"status": 0, "point": bytes(range(64))},
            "00a140" + bytes(range(64)).hex(),
            id="0821",
        ),
    ],
)
def test_reply_layouts(
    layout: Construct[Any, Any], values: dict[str, object], expected: str
) -> None:
    assert layout.build(values).hex() == expected


def test_request_fields_are_typed_by_message() -> None:
    clock = parse_request(0x022, bytes.fromhex("a1041a69a76aa30440380000a503455354"))

    assert clock.time == 0x6AA7691A
    assert clock.utc_offset == 0x3840
    assert clock.tz == b"EST"


def test_absent_and_malformed_request_fields_are_none() -> None:
    request = parse_request(0x003, bytes.fromhex("a10400000000a40100"))

    assert request.app_encrypt is None
    assert request.mtu is None


def test_unknown_request_fields_are_ignored() -> None:
    request = parse_request(0x005, bytes.fromhex("a2020102a50144"))

    assert request == Container(method=0x44, auth_method=None)


@pytest.mark.parametrize(
    ("payload", "route", "rerouted"),
    [
        pytest.param("a10131a20101", 0x31, "a10134a20101", id="push"),
        pytest.param("00a10131", 0x31, "00a10134", id="reply"),
        pytest.param("04", None, "04", id="status only"),
        pytest.param("00a20101", None, "00a20101", id="no route field"),
    ],
)
def test_an_mcu_payload_route_is_read_and_set(
    payload: str, route: int | None, rerouted: str
) -> None:
    data = bytes.fromhex(payload)

    assert payload_route(data) == route
    assert with_route(data, 0x34).hex() == rerouted
