# Copyright (c) 2026 Shawn Stricker
"""The frame extraction and sanitizing tool."""

import gzip
import json
from pathlib import Path

import pytest

from anker_ble_emulator.frame import FrameError
from tests.fixtures.logs import mqtt_frame, write_frame_log, write_mqtt_records
from tools.sanitize_frames import (
    FIXED_EPOCH_MS,
    FIXED_UNIX_TIME,
    SanitizeError,
    Sanitizer,
    ble_routed,
    fix_trailer,
    main,
    newest_frames,
)


REAL = b"ZZTEST0REAL000001"
FAKE = b"ZZTEST00000000001"


def test_replacements_keep_the_length_and_remove_the_real_value() -> None:
    sanitizer = Sanitizer(replacements={REAL: FAKE})

    clean = sanitizer.sanitize(b"\xa2\x11" + REAL)

    assert clean == b"\xa2\x11" + FAKE


def test_replacement_of_another_length_is_refused() -> None:
    with pytest.raises(ValueError, match="bytes"):
        Sanitizer(replacements={REAL: b"short"})


def test_millisecond_epochs_are_fixed() -> None:
    clean = Sanitizer().sanitize(b"\xfd\x0e\x00" + b"1791343809887")

    assert clean.endswith(FIXED_EPOCH_MS)


def test_trailer_time_is_fixed() -> None:
    data = bytes.fromhex("00a10131fe05031a69a76a")

    assert fix_trailer(data) == bytes.fromhex(
        "00a10131fe0503"
    ) + FIXED_UNIX_TIME.to_bytes(4, "little")


def test_a_fe_field_that_isnt_a_time_is_left_alone() -> None:
    data = bytes.fromhex("a10131fe050401020304")

    assert fix_trailer(data) == data


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(bytes.fromhex("a10131a25301040a"), id="not fields to the end"),
        pytest.param(bytes.fromhex("a10131a2"), id="truncated header"),
        pytest.param(bytes.fromhex("a1013130"), id="tag below a1"),
        pytest.param(bytes.fromhex("00a10131"), id="no trailer"),
    ],
)
def test_trailer_is_left_alone_unless_fields_walk_cleanly(data: bytes) -> None:
    assert fix_trailer(data) == data


def test_forbidden_value_fails_the_frame() -> None:
    mac = bytes.fromhex("aa12deadbeef")

    with pytest.raises(SanitizeError, match="forbidden"):
        Sanitizer(forbidden=(mac,)).sanitize(b"\xa5\x06" + mac)


def test_unreviewed_printable_run_fails_the_frame() -> None:
    with pytest.raises(SanitizeError, match="OTHERSERIAL1"):
        Sanitizer().sanitize(b"\xa2\x0cOTHERSERIAL1")


def test_allowed_runs_pass() -> None:
    sanitizer = Sanitizer(allowed=frozenset({b"A1783_2kWh"}))

    assert sanitizer.sanitize(b"\xa1\x0aA1783_2kWh") == b"\xa1\x0aA1783_2kWh"


def test_newest_frame_per_msgtype_wins(tmp_path: Path) -> None:
    older = write_frame_log(tmp_path / "a.log", [("c421", b"\x01"), ("4303", b"\x09")])
    newer = write_frame_log(tmp_path / "b.log", [("4421", b"\x02")])

    assert newest_frames([older, newer], {0x421}) == {0x421: b"\x02"}


def test_exact_cmd_keeps_only_that_flag_variant(tmp_path: Path) -> None:
    log = write_frame_log(tmp_path / "e.log", [("ca00", b"\x01"), ("4a00", b"\x02")])

    assert newest_frames([log], set(), frozenset({0xCA00})) == {0xA00: b"\x01"}


def test_gzipped_logs_are_read(tmp_path: Path) -> None:
    plain = write_frame_log(tmp_path / "f.log", [("4303", b"\x03")])
    packed = tmp_path / "f.log.gz"
    packed.write_bytes(gzip.compress(plain.read_bytes()))

    assert newest_frames([packed], {0x303}) == {0x303: b"\x03"}


def test_main_writes_sanitized_frames(tmp_path: Path) -> None:
    log = write_frame_log(tmp_path / "c.log", [("c900", b"\x00\xa2\x11" + REAL)])
    output = tmp_path / "out.json"

    status = main(
        [
            str(log),
            "--output",
            str(output),
            "--msgtype",
            "900",
            "--frame",
            "903=00a10131",
            "--replace",
            f"{REAL.decode()}={FAKE.decode()}",
        ]
    )

    assert status == 0
    assert json.loads(output.read_text()) == {
        "900": (b"\x00\xa2\x11" + FAKE).hex(),
        "903": "00a10131",
    }


@pytest.mark.parametrize(
    ("data", "routed"),
    [
        pytest.param("a10134a20101", "a10131a20101", id="param_info push"),
        pytest.param("00a10132", "00a10131", id="state_info ack"),
        pytest.param("00a10131a20101", "00a10131a20101", id="already BLE"),
        pytest.param("04", "04", id="status only"),
        pytest.param("00a20101", "00a20101", id="no routing marker"),
    ],
)
def test_mqtt_payloads_get_the_ble_routing_marker(data: str, routed: str) -> None:
    assert ble_routed(bytes.fromhex(data)).hex() == routed


def test_mqtt_records_of_both_shapes_are_read(tmp_path: Path) -> None:
    records = write_mqtt_records(
        tmp_path / "m.ndjson",
        [mqtt_frame(0x421, "a10134a20101"), mqtt_frame(0x903, "00a10132")],
    )

    assert newest_frames([records], {0x421, 0x903}) == {
        0x421: bytes.fromhex("a10131a20101"),
        0x903: bytes.fromhex("00a10131"),
    }


def test_mqtt_frame_with_a_stale_checksum_is_refused(tmp_path: Path) -> None:
    stale = mqtt_frame(0x900, "00a10134a20101")[:-1] + b"\x00"
    records = write_mqtt_records(tmp_path / "s.ndjson", [stale])

    with pytest.raises(FrameError, match="checksum"):
        newest_frames([records], {0x900})


def test_main_fixes_stale_checksums_when_asked(tmp_path: Path) -> None:
    stale = mqtt_frame(0x900, "00a10134a211" + REAL.hex())[:-1] + b"\x00"
    records = write_mqtt_records(tmp_path / "t.ndjson", [stale])
    output = tmp_path / "fixed.json"

    status = main(
        [
            str(records),
            "--output",
            str(output),
            "--msgtype",
            "900",
            "--fix-checksum",
            "--replace",
            f"{REAL.decode()}={FAKE.decode()}",
        ]
    )

    assert status == 0
    assert json.loads(output.read_text()) == {"900": "00a10131a211" + FAKE.hex()}


def test_main_fails_when_a_msgtype_is_missing(tmp_path: Path) -> None:
    log = write_frame_log(tmp_path / "d.log", [])
    output = tmp_path / "x.json"

    status = main([str(log), "--output", str(output), "--msgtype", "490"])

    assert status == 1
    assert not output.exists()
