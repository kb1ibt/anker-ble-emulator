# Copyright (c) 2026 Shawn Stricker
"""The F2000Alt legacy protocol: frame parsing, telemetry, commands, StateAck."""

import pytest

from anker_ble_emulator.devices.a1780 import EXTENDED, TELEMETRY
from anker_ble_emulator.legacy import (
    CMD_POLL,
    FIELD_AC_OUTPUT,
    FIELD_DC_OUTPUT,
    FIELD_LIGHT_MODE,
    FIELD_POWER_SAVING_MODE,
    FRAME_OVERHEAD,
    HEADER_NOTIFY,
    LEGACY_REQUEST,
    STATE_ACK,
    STATE_ACK_CMD_BYTE,
    TELEMETRY_FRAME,
    LegacyProfile,
)
from tests.fixtures.legacy import build_legacy_module, control_request, legacy_request


def test_poll_replies_with_the_extended_then_the_base_frame() -> None:
    module = build_legacy_module()

    out = module.write(legacy_request(CMD_POLL))

    assert out.frames == [EXTENDED, TELEMETRY]


def test_baseline_frames_round_trip_through_the_telemetry_struct() -> None:
    for frame in (TELEMETRY, EXTENDED):
        fields = TELEMETRY_FRAME.parse(frame).body.value

        assert TELEMETRY_FRAME.build({"body": {"value": fields}}) == frame


@pytest.mark.parametrize(
    ("field_id", "path", "value"),
    [
        pytest.param(FIELD_AC_OUTPUT, ("ac_output",), 0, id="ac_off"),
        pytest.param(
            FIELD_POWER_SAVING_MODE,
            ("settings", "power_saving_mode"),
            1,
            id="power_saving_on",
        ),
        pytest.param(FIELD_LIGHT_MODE, ("settings", "light_mode"), 3, id="light_high"),
    ],
)
def test_control_command_patches_both_cached_frames(
    field_id: int, path: tuple[str, ...], value: int
) -> None:
    module = build_legacy_module()

    out = module.write(control_request(field_id, value))

    assert out.frames == []
    extended, base = module.write(legacy_request(CMD_POLL)).frames
    for frame in (extended, base):
        fields = TELEMETRY_FRAME.parse(frame).body.value  # checksum verified here
        for name in path[:-1]:
            fields = fields[name]
        if fields is not None:
            assert fields[path[-1]] == value
    assert TELEMETRY_FRAME.parse(base).body.value.settings is None


def test_dc_control_moves_both_physical_ports_together() -> None:
    module = build_legacy_module()

    module.write(control_request(FIELD_DC_OUTPUT, 1))

    for frame in module.write(legacy_request(CMD_POLL)).frames:
        fields = TELEMETRY_FRAME.parse(frame).body.value
        assert fields.dc_output_1 == fields.dc_output_2 == 1


def test_control_command_with_a_short_payload_is_ignored() -> None:
    module = build_legacy_module()

    out = module.write(legacy_request(bytes([0x02, FIELD_AC_OUTPUT]), b"\x00"))

    assert out.frames == []
    assert module.write(legacy_request(CMD_POLL)).frames == [EXTENDED, TELEMETRY]


def test_control_command_with_an_unknown_field_id_is_a_no_op() -> None:
    module = build_legacy_module()

    out = module.write(control_request(0xFF, 1))

    assert out.frames == []
    poll = module.write(legacy_request(CMD_POLL))
    assert poll.frames == [EXTENDED, TELEMETRY]


def test_press_button_emits_a_state_ack_of_the_current_state() -> None:
    module = build_legacy_module()
    module.write(control_request(FIELD_AC_OUTPUT, 0))
    module.write(control_request(FIELD_LIGHT_MODE, 3))

    out = module.press_button()

    frame = out.frames[0]
    ack = STATE_ACK.parse(frame).body.value  # header, cmd and checksum verified here
    baseline = TELEMETRY_FRAME.parse(TELEMETRY).body.value
    settings = TELEMETRY_FRAME.parse(EXTENDED).body.value.settings
    assert len(frame) == STATE_ACK.sizeof()
    assert ack.header == HEADER_NOTIFY
    assert ack.cmd == bytes([STATE_ACK_CMD_BYTE])
    assert ack.ac_output == 0
    assert ack.dc_output == baseline.dc_output_1
    assert ack.light_mode == 3
    assert ack.power_saving_mode == settings.power_saving_mode


def test_unknown_cmd_gets_no_reply() -> None:
    module = build_legacy_module()

    out = module.write(legacy_request(b"\x09\x99"))

    assert out.frames == []


def test_malformed_bytes_are_dropped() -> None:
    module = build_legacy_module()

    out = module.write(b"\x00\x01")

    assert out.frames == []


def test_bad_checksum_is_dropped() -> None:
    module = build_legacy_module()
    request = bytearray(legacy_request(CMD_POLL))
    request[-1] ^= 1

    out = module.write(bytes(request))

    assert out.frames == []


def test_a_notify_direction_write_is_ignored() -> None:
    module = build_legacy_module()
    request = LEGACY_REQUEST.build(
        {
            "body": {
                "value": {
                    "header": HEADER_NOTIFY,
                    "pattern": b"\x00\x00\x00",
                    "cmd": CMD_POLL,
                    "length": FRAME_OVERHEAD,
                    "payload": b"",
                }
            }
        }
    )

    out = module.write(request)

    assert out.frames == []


def test_check_timers_does_nothing() -> None:
    module = build_legacy_module()

    assert module.check_timers().frames == []


def test_connect_and_disconnect_track_the_link() -> None:
    module = build_legacy_module()

    assert not module.connected
    module.connect()
    assert module.connected
    module.disconnect()
    assert not module.connected


def test_fragment_cap_is_the_profile_s() -> None:
    module = build_legacy_module()

    assert module.fragment_cap == 253


def test_serial_number_reads_the_baseline_frame() -> None:
    profile = LegacyProfile(
        local_name="SOLIX F2000", telemetry=TELEMETRY, extended=EXTENDED
    )

    assert profile.serial_number == "0102030405060708"


def test_serial_number_is_none_when_the_frame_is_too_short() -> None:
    profile = LegacyProfile(local_name="SOLIX F2000", telemetry=b"", extended=b"")

    assert profile.serial_number is None


def test_serial_number_is_none_for_an_all_nul_serial() -> None:
    fields = TELEMETRY_FRAME.parse(TELEMETRY).body.value
    fields.serial = bytes(16)
    frame = TELEMETRY_FRAME.build({"body": {"value": fields}})
    profile = LegacyProfile(local_name="SOLIX F2000", telemetry=frame, extended=frame)

    assert profile.serial_number is None
