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
    HEADER_NOTIFY,
    OFFSET_AC_OUTPUT,
    OFFSET_DC_OUTPUT,
    OFFSET_LIGHT_MODE,
    OFFSET_POWER_SAVING_MODE,
    STATE_ACK_AC,
    STATE_ACK_CMD_BYTE,
    STATE_ACK_DC,
    STATE_ACK_LENGTH,
    STATE_ACK_LIGHT,
    STATE_ACK_POWER_SAVING,
    LegacyProfile,
    checksum,
)
from tests.fixtures.legacy import build_legacy_module, control_request, legacy_request


def test_poll_replies_with_the_extended_then_the_base_frame() -> None:
    module = build_legacy_module()

    out = module.write(legacy_request(CMD_POLL))

    assert out.frames == [EXTENDED, TELEMETRY]


@pytest.mark.parametrize(
    ("field_id", "offset", "value"),
    [
        pytest.param(FIELD_AC_OUTPUT, OFFSET_AC_OUTPUT, 0, id="ac_off"),
        pytest.param(
            FIELD_POWER_SAVING_MODE, OFFSET_POWER_SAVING_MODE, 1, id="power_saving_on"
        ),
        pytest.param(FIELD_LIGHT_MODE, OFFSET_LIGHT_MODE, 3, id="light_high"),
    ],
)
def test_control_command_patches_both_cached_frames(
    field_id: int, offset: int, value: int
) -> None:
    module = build_legacy_module()

    out = module.write(control_request(field_id, value))

    assert out.frames == []
    poll = module.write(legacy_request(CMD_POLL))
    assert poll.frames[0][offset] == value
    assert checksum(poll.frames[0][:-1]) == poll.frames[0][-1]
    if offset < len(TELEMETRY):
        assert poll.frames[1][offset] == value
        assert checksum(poll.frames[1][:-1]) == poll.frames[1][-1]


def test_dc_control_moves_both_physical_ports_together() -> None:
    module = build_legacy_module()

    module.write(control_request(FIELD_DC_OUTPUT, 1))

    poll = module.write(legacy_request(CMD_POLL))
    assert poll.frames[0][OFFSET_DC_OUTPUT[0]] == 1
    assert poll.frames[0][OFFSET_DC_OUTPUT[1]] == 1
    assert poll.frames[1][OFFSET_DC_OUTPUT[0]] == 1
    assert poll.frames[1][OFFSET_DC_OUTPUT[1]] == 1


def test_control_command_with_a_short_payload_is_ignored() -> None:
    module = build_legacy_module()

    out = module.write(legacy_request(bytes([0x02, FIELD_AC_OUTPUT]), b"\x00"))

    assert out.frames == []


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
    assert len(frame) == STATE_ACK_LENGTH
    assert frame[0:2] == HEADER_NOTIFY
    assert frame[6] == STATE_ACK_CMD_BYTE
    assert frame[STATE_ACK_AC] == 0
    assert frame[STATE_ACK_DC] == TELEMETRY[OFFSET_DC_OUTPUT[0]]
    assert frame[STATE_ACK_LIGHT] == 3
    assert frame[STATE_ACK_POWER_SAVING] == EXTENDED[OFFSET_POWER_SAVING_MODE]
    assert checksum(frame[:-1]) == frame[-1]


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
    body = HEADER_NOTIFY + b"\x00\x00\x00" + CMD_POLL + (10).to_bytes(2, "little")
    request = body + bytes([checksum(body)])

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


def test_serial_number_is_none_for_an_all_nul_range() -> None:
    frame = bytes(101)
    profile = LegacyProfile(local_name="SOLIX F2000", telemetry=frame, extended=frame)

    assert profile.serial_number is None
