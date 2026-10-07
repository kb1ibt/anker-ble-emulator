# Copyright (c) 2026 Shawn Stricker
"""The module: negotiation, authorization, the relay and the authorize timer."""

import pytest

from anker_ble_emulator.frame import decode, encode, fragment, make_frame
from anker_ble_emulator.module import AuthMode
from tests.fixtures.client import (
    SESSION,
    TIMESTAMP,
    AppClient,
    cmd_hex,
    exchange,
    negotiate,
    pattern_hex,
)
from tests.fixtures.module import (
    ACK_REPLY,
    ENROLLED_TOKEN,
    LONG_REPLY,
    PUSH,
    TEST_MAC,
    TEST_SERIAL,
    build_module,
)


FRAME_4801 = bytes.fromhex(
    "ff091e000300014801ab273ed3e27270c3f4d676ac7d69a00572793732a6"
)
FRAME_4803 = bytes.fromhex(
    "ff092b000300014803ab273ed04438d4b25db54c6d4a6ec3d481f5ad58ff7cc2be8bc8369fd98c0b914e03"
)
#: ``0830 a1``: the module firmware version.
VERSION_REPLY = bytes.fromhex("00a10876302e332e332e30")
#: A session request's route: from BLE to the module or MCU.
BLE_ROUTE = [(0xA1, b"\x21")]


def test_stage_one_and_two_replies_match_the_recorded_frames() -> None:
    rig = build_module()
    client = AppClient()

    connect = rig.module.write(client.request(0x001, [(0xA1, TIMESTAMP)]))
    capability = rig.module.write(
        client.request(
            0x003, [(0xA1, TIMESTAMP), (0xA3, b"\x20"), (0xA4, bytes.fromhex("00f0"))]
        )
    )

    assert connect.frames == [FRAME_4801]
    assert capability.frames == [FRAME_4803]


def test_device_info_reports_the_identity() -> None:
    rig = build_module()

    replies = negotiate(rig.module, AppClient(), ENROLLED_TOKEN)

    assert replies[0x829].fields == {
        0xA1: b"\x03",
        0xA2: b"ESP32",
        0xA3: b"0.0.0.3",
        0xA4: TEST_SERIAL,
        0xA5: TEST_MAC,
    }


def test_device_info_omits_a_missing_serial() -> None:
    rig = build_module(serial=None)

    replies = negotiate(rig.module, AppClient(), ENROLLED_TOKEN)

    assert 0xA4 not in replies[0x829].fields
    assert replies[0x829].fields[0xA5] == TEST_MAC


def test_encrypted_outer_authorizes_an_enrolled_token() -> None:
    rig = build_module()

    replies = negotiate(rig.module, AppClient(), ENROLLED_TOKEN)

    assert [replies[op].status for op in (0x801, 0x803, 0x829, 0x805, 0x821)] == [0] * 5
    assert [replies[op].status for op in (0x822, 0x827)] == [0, 0]
    assert all(reply.frame.cmd.encrypted for reply in replies.values())
    assert rig.module.authorized


def test_session_replies_with_equal_plaintext_have_equal_bodies() -> None:
    rig = build_module()

    replies = negotiate(rig.module, AppClient(), ENROLLED_TOKEN)

    assert replies[0x822].frame.payload == replies[0x827].frame.payload


def test_key_exchange_reply_is_under_the_static_key() -> None:
    rig = build_module()
    client = AppClient()
    exchange(rig.module, client, 0x005, [(0xA5, b"\x44")])

    replies = exchange(rig.module, client, 0x021, [(0xA1, client.public_point)])

    assert client.session is None
    assert cmd_hex(replies[0].frame) == "4821"
    assert len(replies[0].fields[0xA1]) == 64


def test_mcu_replies_are_relayed_encrypted_and_fragmented() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x100, [(0xA1, b"\x21")], SESSION))
    frames = [decode(data) for data in out.frames]
    replies = [reply for data in out.frames if (reply := client.open(data))]

    assert {pattern_hex(frame) for frame in frames} == {"03010f"}
    assert len(frames) > 1
    assert all(cmd_hex(frame) == "c900" for frame in frames)
    assert len(replies) == 1
    assert cmd_hex(replies[0].frame) == "4900"
    assert replies[0].plaintext == LONG_REPLY


def test_unfragmented_mcu_reply() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x057, [(0xA1, b"\x21")], SESSION))

    reply = client.open(out.frames[0])
    assert reply is not None
    assert cmd_hex(reply.frame) == "4857"
    assert reply.plaintext == ACK_REPLY


def test_unscripted_mcu_request_gets_no_reply() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x0A0, [(0xA1, b"\x21")], SESSION))

    assert out.frames == []


def test_push_goes_out_once_authorized() -> None:
    rig = build_module()
    client = AppClient()
    before = rig.module.push(0x421)
    negotiate(rig.module, client, ENROLLED_TOKEN)

    after = rig.module.push(0x421)

    assert before.frames == []
    reply = client.open(after.frames[0])
    assert reply is not None
    assert cmd_hex(reply.frame) == "4421"
    assert reply.plaintext == PUSH


def test_unauthorized_link_reaches_no_mcu() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, b"unknown-token")

    out = rig.module.write(client.request(0x100, [(0xA1, b"\x21")], SESSION))

    assert not rig.module.authorized
    assert out.frames == []


def test_new_token_waits_for_the_button() -> None:
    rig = build_module()
    client = AppClient()

    replies = negotiate(rig.module, client, b"new-token")

    assert replies[0x827].status == 0x09
    assert replies[0x827].fields == {0xA1: bytes.fromhex("1e00")}
    assert not rig.module.authorized


def test_button_press_grants_and_enrolls_the_token() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, b"new-token")

    out = rig.module.press_button()

    grant = client.open(out.frames[0])
    assert grant is not None
    assert pattern_hex(grant.frame) == "030101"
    assert cmd_hex(grant.frame) == "4827"
    assert grant.status == 0
    assert rig.module.authorized
    assert b"new-token" in rig.module.enrolled


def test_button_press_without_a_pending_confirmation_does_nothing() -> None:
    rig = build_module()

    assert rig.module.press_button().frames == []


def test_cleartext_connect_is_dropped_under_enforcement() -> None:
    rig = build_module()

    out = rig.module.write(AppClient(encrypted_outer=False).request(0x001, []))

    assert out.frames == []
    assert out.disconnect


def test_non_ecdh_method_is_refused_under_enforcement() -> None:
    rig = build_module()
    client = AppClient()

    replies = exchange(rig.module, client, 0x005, [(0xA5, b"\x02")])

    assert replies[0].status == 0x01


@pytest.mark.parametrize(
    ("method", "status"),
    [
        pytest.param(b"\x00", 0x00, id="no encryption"),
        pytest.param(b"\x01", 0x01, id="no method bit"),
        pytest.param(b"\x40", 0x00, id="ecdh 0x40"),
        pytest.param(b"\x04", 0x00, id="ecdh 0x04"),
    ],
)
def test_method_choice(method: bytes, status: int) -> None:
    rig = build_module()

    replies = exchange(rig.module, AppClient(), 0x005, [(0xA5, method)])

    assert replies[0].status == status


def test_key_exchange_without_an_ecdh_choice_answers_bare() -> None:
    rig = build_module()
    client = AppClient()

    replies = exchange(rig.module, client, 0x021, [(0xA1, client.public_point)])

    assert replies[0].status == 0
    assert replies[0].fields == {}


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        pytest.param([], 0x04, id="no point"),
        pytest.param([(0xA1, bytes(64))], 0x01, id="point off the curve"),
    ],
)
def test_key_exchange_rejects_a_bad_point(
    fields: list[tuple[int, bytes]], status: int
) -> None:
    rig = build_module()
    client = AppClient()
    exchange(rig.module, client, 0x005, [(0xA5, b"\x44")])

    replies = exchange(rig.module, client, 0x021, fields)

    assert replies[0].status == status


def test_capabilities_without_mtu_fields_is_a_parameter_error() -> None:
    rig = build_module()

    replies = exchange(rig.module, AppClient(), 0x003, [(0xA1, TIMESTAMP)])

    assert replies[0].status == 0x04


def test_auth_method_is_absent_on_the_older_shape() -> None:
    rig = build_module(reports_auth_method=False, advanced_capability=0x04)

    replies = exchange(
        rig.module,
        AppClient(),
        0x003,
        [(0xA3, b"\x20"), (0xA4, bytes.fromhex("00f0"))],
    )

    assert replies[0].fields[0xA3] == b"\x04"
    assert 0xA5 not in replies[0].fields


def test_plain_outer_authorizes_at_the_key_exchange() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN, enforce=False)
    client = AppClient(encrypted_outer=False)

    replies = negotiate(rig.module, client, ENROLLED_TOKEN)

    assert not replies[0x801].frame.cmd.encrypted
    assert not replies[0x821].frame.cmd.encrypted
    assert replies[0x822].frame.cmd.encrypted
    assert len(replies[0x822].frame.payload) % 16 == 0
    assert rig.module.authorized


def test_open_auth_mode_disconnects_after_its_reply() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN)
    client = AppClient()
    steps = negotiate(rig.module, client, ENROLLED_TOKEN)
    assert steps[0x822].status == 0

    out = rig.module.write(
        client.request(0x027, [(0xA1, TIMESTAMP), (0xA2, ENROLLED_TOKEN)])
    )

    assert len(out.frames) == 1
    assert out.disconnect


def test_time_limited_auth_mode_authorizes_any_token() -> None:
    rig = build_module(auth_mode=AuthMode.TIME_LIMITED)

    negotiate(rig.module, AppClient(), b"anyone")

    assert rig.module.authorized


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param([(0xA1, TIMESTAMP)], id="no token"),
        pytest.param([(0xA2, bytes(64))], id="token too long"),
    ],
)
def test_authentication_rejects_a_bad_token(fields: list[tuple[int, bytes]]) -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, b"new-token")

    replies = exchange(rig.module, client, 0x027, fields)

    assert replies[0].status == 0x01


def test_authentication_before_the_key_exchange_fails_under_enforcement() -> None:
    rig = build_module()

    replies = exchange(rig.module, AppClient(), 0x027, [(0xA2, ENROLLED_TOKEN)])

    assert replies[0].status == 0x01


def test_clock_needs_its_time_and_offset() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    replies = exchange(rig.module, client, 0x022, [(0xA1, TIMESTAMP)])

    assert replies[0].status == 0x04


def test_clock_before_an_ecdh_choice_gets_no_reply() -> None:
    rig = build_module()

    assert exchange(rig.module, AppClient(), 0x022, [(0xA1, TIMESTAMP)]) == []


def test_unknown_negotiation_opcode_gets_no_reply() -> None:
    rig = build_module()

    assert exchange(rig.module, AppClient(), 0x00B, []) == []


def test_unrecorded_module_session_opcode_gets_no_reply() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x030, BLE_ROUTE, SESSION))

    assert out.frames == []


def test_recorded_module_session_reply_comes_back_on_the_request_pattern() -> None:
    rig = build_module(session_replies={0x030: VERSION_REPLY})
    client = AppClient()
    before = rig.module.write(client.request(0x030, BLE_ROUTE, SESSION))
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x030, BLE_ROUTE, SESSION))

    reply = client.open(out.frames[0])
    assert before.frames == []
    assert reply is not None
    assert pattern_hex(reply.frame) == "03000f"
    assert cmd_hex(reply.frame) == "4830"
    assert reply.plaintext == VERSION_REPLY


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param([(0xA1, b"\x22")], id="from MQTT"),
        pytest.param([(0xA1, b"\x24")], id="from MQTT_2"),
        pytest.param([(0xA1, b"\x31")], id="from BLE to the app"),
        pytest.param([(0xA1, b"\x2c")], id="unknown source"),
        pytest.param([], id="no route"),
        pytest.param([(0xA1, b"\x21\x00")], id="route too long"),
    ],
)
def test_an_mcu_request_not_routed_from_ble_to_the_mcu_is_dropped(
    fields: list[tuple[int, bytes]],
) -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x100, fields, SESSION))

    assert out.frames == []


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param([(0xA1, b"\x21")], id="from BLE"),
        pytest.param([(0xA1, b"\x22")], id="from MQTT"),
        pytest.param([], id="no route"),
    ],
)
def test_a_module_op_answers_on_the_arrival_port_whatever_its_route(
    fields: list[tuple[int, bytes]],
) -> None:
    rig = build_module(session_replies={0x030: VERSION_REPLY})
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x030, fields, SESSION))

    reply = client.open(out.frames[0])
    assert reply is not None
    assert reply.plaintext == VERSION_REPLY


def test_a_logging_channel_request_is_answered_by_the_module() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x100, [(0xA1, b"\x2a")], SESSION))

    reply = client.open(out.frames[0])
    assert reply is not None
    assert cmd_hex(reply.frame) == "4900"
    assert reply.plaintext == b"\x00"


def test_cloud_push_keeps_push_routed_frames_off_ble() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    rig.module.cloud_push = True

    status = rig.module.write(client.request(0x100, BLE_ROUTE, SESSION))
    ack = rig.module.write(client.request(0x057, BLE_ROUTE, SESSION))
    push = rig.module.push(0x421)

    replies = [reply for data in ack.frames if (reply := client.open(data))]
    assert status.frames == []
    assert push.frames == []
    assert [cmd_hex(reply.frame) for reply in replies] == ["4857"]


def test_a_request_with_malformed_fields_gets_no_reply() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    assert client.session is not None
    request = make_frame(
        0x00, SESSION, 0x100, client.session.encrypt(b"\xa1\x05\x21"), encrypted=True
    )

    out = rig.module.write(encode(request))

    assert out.frames == []


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\x00\x01", id="not a frame"),
        pytest.param(
            encode(make_frame(0x00, 0x01, 0x001, bytes(20), encrypted=True)),
            id="bad tag",
        ),
        pytest.param(
            encode(make_frame(0x00, 0x01, 0x001, b"\xa1\x09")), id="bad fields"
        ),
        pytest.param(encode(make_frame(0x00, 0x13, 0x022)), id="other channel"),
    ],
)
def test_bad_writes_are_dropped(data: bytes) -> None:
    rig = build_module(enforce=False)

    out = rig.module.write(data)

    assert out.frames == []
    assert not out.disconnect


def test_undecryptable_session_request_is_dropped() -> None:
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)

    out = rig.module.write(
        encode(make_frame(0x00, SESSION, 0x100, bytes(20), encrypted=True))
    )

    assert out.frames == []


def test_write_without_a_link_raises() -> None:
    rig = build_module()
    rig.module.disconnect()

    with pytest.raises(RuntimeError, match="without a connected link"):
        rig.module.write(FRAME_4801)


def test_unauthorized_link_drops_thirty_seconds_after_connect() -> None:
    rig = build_module()

    rig.clock.advance(29.9)
    early = rig.module.check_timers()
    rig.clock.advance(0.1)
    due = rig.module.check_timers()

    assert not early.disconnect
    assert due.disconnect


def test_confirmation_window_extends_the_limit() -> None:
    rig = build_module()
    client = AppClient()
    rig.clock.advance(20)
    negotiate(rig.module, client, b"new-token")

    rig.clock.advance(34.9)
    early = rig.module.check_timers()
    rig.clock.advance(0.1)
    due = rig.module.check_timers()

    assert not early.disconnect
    assert due.disconnect


def test_authorized_link_never_drops() -> None:
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)

    rig.clock.advance(3600)

    assert not rig.module.check_timers().disconnect


def test_timer_without_a_link_does_nothing() -> None:
    rig = build_module()
    rig.module.disconnect()

    assert not rig.module.check_timers().disconnect
    assert not rig.module.connected


def test_fragmented_request_is_answered_once_complete() -> None:
    rig = build_module()
    client = AppClient()
    exchange(rig.module, client, 0x005, [(0xA5, b"\x44")])
    whole = decode(client.request(0x021, [(0xA1, client.public_point)]))
    first, second = fragment(whole, 60)

    pending = rig.module.write(encode(first))
    done = rig.module.write(encode(second))

    assert pending.frames == []
    reply = client.open(done.frames[0])
    assert reply is not None
    assert cmd_hex(reply.frame) == "4821"


def test_cleartext_clock_on_the_ecdh_path_fails() -> None:
    rig = build_module(enforce=False)
    client = AppClient(encrypted_outer=False)
    exchange(rig.module, client, 0x005, [(0xA5, b"\x40")])

    replies = exchange(rig.module, client, 0x022, [(0xA1, TIMESTAMP)])

    assert replies[0].status == 0x01


def test_cleartext_session_request_is_relayed() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(encode(make_frame(0x00, SESSION, 0x057, b"\xa1\x01\x21")))

    reply = client.open(out.frames[0])
    assert reply is not None
    assert reply.plaintext == ACK_REPLY


def test_push_without_a_session_sends_nothing() -> None:
    rig = build_module(enforce=False, auth_mode=AuthMode.TIME_LIMITED)
    exchange(rig.module, AppClient(), 0x027, [(0xA2, b"anyone")])

    out = rig.module.push(0x421)

    assert rig.module.authorized
    assert out.frames == []
