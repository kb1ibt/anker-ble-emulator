# Copyright (c) 2026 Shawn Stricker
"""The module: negotiation, authorization, the relay and the authorize timer."""

from __future__ import annotations

import calendar
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from construct import Int32sl, Int32ul

from anker_ble_emulator import A1783
from anker_ble_emulator.crypto import (
    DEFAULT_ACCOUNT,
    STATIC_GCM,
    STATIC_IV,
    STATIC_KEY,
    STATIC_NONCE,
    CbcCipher,
    GcmCipher,
)
from anker_ble_emulator.frame import (
    CHANNEL_APP,
    CHANNEL_WIFI,
    decode,
    encode,
    fragment,
    make_frame,
)
from anker_ble_emulator.mcu import Burst
from anker_ble_emulator.module import AuthMode, Output
from anker_ble_emulator.parameters import TableLayout
from anker_ble_emulator.timed import Step, Timed
from anker_ble_emulator.tlv import encode_fields
from tests.fixtures.client import (
    NEGOTIATION,
    SESSION,
    TIMESTAMP,
    TZ,
    UTC_OFFSET,
    AppClient,
    cmd_hex,
    confer_request,
    connected_client,
    exchange,
    negotiate,
    open_confer_reply,
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


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator.layouts import Value

FRAME_4801 = bytes.fromhex(
    "ff091e000300014801ab273ed3e27270c3f4d676ac7d69a00572793732a6"
)
FRAME_4803 = bytes.fromhex(
    "ff092b000300014803ab273ed04438d4b25db54c6d4a6ec3d481f5ad58ff7cc2be8bc8369fd98c0b914e03"
)
#: ``0830 a1``: the module firmware version.
VERSION_REPLY = bytes.fromhex("00a10876302e332e332e30")
#: ``0822`` on fid ``0x0f``: the scanWifi reply (A1783).
SCAN_WIFI_REPLY = bytes.fromhex("00a10109")
#: A session request's route: from BLE to the module or MCU.
BLE_ROUTE = [(0xA1, b"\x21")]
#: ``0023 a2``: the account a bind stores.
ACCOUNT = b"an-account-id"
#: ``002b a3``: five hours west of UTC, as hours then tenths.
ZONE_WEST = b"\xfb\x00"


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
    client = connected_client(rig.module)
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


def test_an_mcu_without_a_burst_has_no_timed_pushes() -> None:
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)
    rig.clock.advance(60)

    out = rig.module.check_pushes()

    assert rig.module.authorized
    assert out == Output()


def test_a_timed_event_sets_its_values_and_pushes_once_due() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    rig.module.schedule(
        Timed(
            5.0,
            "event",
            lambda _values, _state: Step(values={"level": 1}, pushes=(0x421,)),
        )
    )

    early = rig.module.check_pushes()
    rig.clock.advance(5.0)
    due = rig.module.check_pushes()

    assert early == Output()
    assert rig.module.values == {"level": 1}
    reply = client.open(due.frames[0])
    assert reply is not None
    assert cmd_hex(reply.frame) == "4421"


def test_a_recurring_event_keeps_its_cadence_when_caught_up() -> None:
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)

    def tick(values: Mapping[str, Value], _state: Mapping[str, Value]) -> Step:
        count = int(values.get("ticks", 0)) + 1
        return Step(values={"ticks": count}, pushes=(0x421,), then=(every_ten,))

    every_ten = Timed(10.0, "tick", tick)
    rig.module.schedule(every_ten)

    rig.clock.advance(35.0)
    caught_up = rig.module.check_pushes()
    rig.clock.advance(5.0)
    on_cadence = rig.module.check_pushes()

    assert rig.module.values == {"ticks": 4}
    assert len(caught_up.frames) == 3
    assert len(on_cadence.frames) == 1


def test_a_jump_sets_bypassed_values_but_sends_nothing() -> None:
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)
    rig.module.schedule(
        Timed(
            5.0,
            "event",
            lambda _values, _state: Step(values={"level": 1}, pushes=(0x421,)),
        )
    )

    rig.clock.jump(10.0)
    out = rig.module.check_pushes()

    assert out == Output()
    assert rig.module.values == {"level": 1}


def test_a_new_event_replaces_the_queued_one_with_its_key() -> None:
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)
    rig.module.schedule(
        Timed(5.0, "event", lambda _values, _state: Step(values={"level": 1}))
    )
    rig.module.schedule(
        Timed(8.0, "event", lambda _values, _state: Step(values={"level": 2}))
    )

    rig.clock.advance(6.0)
    rig.module.check_pushes()
    at_six = dict(rig.module.values)
    rig.clock.advance(2.0)
    rig.module.check_pushes()

    assert at_six == {}
    assert rig.module.values == {"level": 2}


def test_a_link_event_ends_with_the_link() -> None:
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)
    rig.module.schedule(
        Timed(
            5.0, "event", lambda _values, _state: Step(values={"level": 1}), link=True
        )
    )

    rig.module.disconnect()
    rig.clock.advance(10.0)
    rig.module.connect()
    rig.module.check_pushes()

    assert rig.module.values == {}


def test_an_event_due_with_no_link_sets_its_values_but_sends_nothing() -> None:
    rig = build_module()
    client = AppClient()
    rig.module.schedule(
        Timed(
            5.0,
            "event",
            lambda _values, _state: Step(values={"level": 1}, pushes=(0x421,)),
        )
    )
    rig.module.disconnect()

    rig.clock.advance(10.0)
    unlinked = rig.module.check_pushes()
    rig.module.connect()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    out = rig.module.check_pushes()

    assert rig.module.values == {"level": 1}
    assert unlinked == out == Output()


def test_an_event_due_before_a_connect_runs_at_the_connect() -> None:
    rig = build_module()
    rig.module.schedule(
        Timed(5.0, "event", lambda _values, _state: Step(values={"level": 1}))
    )
    rig.module.disconnect()

    rig.clock.advance(10.0)
    rig.module.connect()

    assert rig.module.values == {"level": 1}


def test_a_step_sends_the_frames_it_builds_after_its_pushes() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    built = bytes.fromhex("a10131a2020100a3020100")
    rig.module.schedule(
        Timed(
            1.0,
            "event",
            lambda _values, _state: Step(pushes=(0x421,), frames=((0x302, built),)),
        )
    )

    rig.clock.advance(1.0)
    out = rig.module.check_pushes()

    replies = [client.open(frame) for frame in out.frames]
    assert [None if r is None else cmd_hex(r.frame) for r in replies] == [
        "4421",
        "4302",
    ]
    assert replies[1] is not None
    assert replies[1].plaintext == built


def test_a_step_unsets_values_back_to_the_recording() -> None:
    rig = build_module()
    rig.module.values.update({"level": 1, "other": 2})
    rig.module.schedule(
        Timed(1.0, "event", lambda _values, _state: Step(unset=("level", "absent")))
    )

    rig.clock.advance(1.0)
    rig.module.check_pushes()

    assert rig.module.values == {"other": 2}


def test_an_action_gets_the_set_values_and_the_whole_state() -> None:
    rig = build_module()
    rig.module.values["level"] = 1
    seen: list[tuple[Mapping[str, Value], Mapping[str, Value]]] = []

    def record(values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
        seen.append((values, state))
        return Step()

    rig.module.schedule(Timed(7.0, "event", record))
    rig.clock.advance(7.0)
    rig.module.check_pushes()

    assert seen == [({"level": 1}, {"level": 1, "rtc": 7, "epoch": 7, "cloud": 0})]
    rig.module.cloud = True
    rig.module.schedule(Timed(1.0, "event", record))
    rig.clock.advance(1.0)
    rig.module.check_pushes()
    assert seen[-1][1]["cloud"] == 1


def test_a_bursts_first_push_the_layout_holds_back_is_not_sent() -> None:
    """``sends`` gates the push a burst trigger answers with, not just the rest."""

    class Quiet(TableLayout):
        def sends(  # a layout hook
            self,
            msgtype: int,  # holds every push back
            values: Mapping[str, Value],
        ) -> bool:
            return False

    rig = build_module()
    client = AppClient()
    rig.module.mcu = replace(
        rig.module.mcu,
        layout=Quiet(),
        burst=Burst(trigger=0x057, push=0x421, count=2, period=1.0),
    )
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x057, [(0xA1, b"\x21")], SESSION))
    rig.clock.advance(1.0)
    later = rig.module.check_pushes()

    replies = [client.open(frame) for frame in out.frames]
    assert [None if r is None else cmd_hex(r.frame) for r in replies] == ["4857"]
    assert later == Output()


def test_the_module_keeps_the_utc_epoch_beside_the_local_rtc() -> None:
    """A clock set puts the RTC on local time; a push's ``fe`` is the UTC epoch.

    prime-a2345-a91b2.md ``4207`` (request ``fe`` 06:10:12 UTC, logged 02:10:12
    EDT) and the ``0305`` example (device ``fe`` 0x6ad160c9 = 19:24:57 EDT).
    """
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)

    rig.clock.advance(5.0)

    assert rig.module.epoch == 0x6AA7691A + 5
    assert rig.module.rtc == 0x6AA7691A - 14400 + 5


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
    client = connected_client(rig.module)

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

    replies = exchange(
        rig.module, connected_client(rig.module), 0x005, [(0xA5, method)]
    )

    assert replies[0].status == status


def test_key_exchange_without_an_ecdh_choice_answers_bare() -> None:
    rig = build_module()
    client = connected_client(rig.module)

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
    client = connected_client(rig.module)
    exchange(rig.module, client, 0x005, [(0xA5, b"\x44")])

    replies = exchange(rig.module, client, 0x021, fields)

    assert replies[0].status == status


def test_capabilities_without_mtu_fields_is_a_parameter_error() -> None:
    rig = build_module()

    replies = exchange(
        rig.module, connected_client(rig.module), 0x003, [(0xA1, TIMESTAMP)]
    )

    assert replies[0].status == 0x04


def test_auth_method_is_absent_on_the_older_shape() -> None:
    rig = build_module(reports_auth_method=False, advanced_capability=0x04)

    replies = exchange(
        rig.module,
        connected_client(rig.module),
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

    replies = exchange(
        rig.module, connected_client(rig.module), 0x027, [(0xA2, ENROLLED_TOKEN)]
    )

    assert replies[0].status == 0x01


def test_clock_needs_its_time_and_offset() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    replies = exchange(rig.module, client, 0x022, [(0xA1, TIMESTAMP)])

    assert replies[0].status == 0x04


def test_clock_before_an_ecdh_choice_gets_no_reply() -> None:
    rig = build_module()

    client = connected_client(rig.module)

    assert exchange(rig.module, client, 0x022, [(0xA1, TIMESTAMP)]) == []


@pytest.mark.parametrize(
    ("zone", "east"),
    [
        pytest.param(b"\x05\x05", 19800, id="5.5 h east"),
        pytest.param(ZONE_WEST, 18000, id="5 h west lands 5 h east"),
        pytest.param(b"\xfb\x05", 16200, id="5.5 h west lands 4.5 h east"),
    ],
)
def test_a_utc_time_set_puts_the_rtc_on_its_zone(zone: bytes, east: int) -> None:
    """a1783-control.md ``0x02b``: west of UTC, only the hours lose their sign."""
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    exchange(rig.module, client, 0x023, [(0xA2, ACCOUNT)])

    replies = exchange(
        rig.module, client, 0x02B, [(0xA1, TIMESTAMP), (0xA2, ACCOUNT), (0xA3, zone)]
    )

    assert [(cmd_hex(reply.frame), reply.status) for reply in replies] == [("482b", 0)]
    assert rig.module.rtc == 0x6AA7691A + east


@pytest.mark.parametrize(
    ("account", "status"),
    [
        pytest.param(ACCOUNT, 0x00, id="bound account"),
        pytest.param(ACCOUNT[:4], 0x00, id="prefix"),
        pytest.param(b"", 0x00, id="empty"),
        pytest.param(b"another-account", 0x02, id="other account"),
    ],
)
def test_a_utc_time_set_checks_its_account_against_the_bind(
    account: bytes, status: int
) -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    exchange(rig.module, client, 0x023, [(0xA2, ACCOUNT)])

    replies = exchange(
        rig.module,
        client,
        0x02B,
        [(0xA1, TIMESTAMP), (0xA2, account), (0xA3, ZONE_WEST)],
    )

    assert [reply.status for reply in replies] == [status]


@pytest.mark.parametrize("missing", [0xA1, 0xA2, 0xA3])
def test_a_utc_time_set_needs_all_three_fields(missing: int) -> None:
    rig = build_module()
    fields = [(0xA1, TIMESTAMP), (0xA2, b""), (0xA3, ZONE_WEST)]

    replies = exchange(
        rig.module,
        AppClient(encrypted_outer=False),
        0x02B,
        [(tag, value) for tag, value in fields if tag != missing],
    )

    assert [reply.status for reply in replies] == [0x04]


def test_a_zero_length_zone_loads_the_bytes_after_it() -> None:
    """a1783-control.md ``0x02b``: no length check, so ``a3`` reads ``a1 04``."""
    rig = build_module()

    replies = exchange(
        rig.module,
        AppClient(encrypted_outer=False),
        0x02B,
        [(0xA2, b""), (0xA3, b""), (0xA1, TIMESTAMP)],
    )

    assert [reply.status for reply in replies] == [0x00]
    assert rig.module.rtc == 0x6AA7691A + 95 * 3600 - 4 * 360


def test_a_cleartext_utc_time_set_before_the_sync_floor_is_acked_only() -> None:
    rig = build_module()
    rtc = rig.module.rtc

    replies = exchange(
        rig.module,
        AppClient(encrypted_outer=False),
        0x02B,
        [(0xA1, b"\x9f\xac\x61\x65"), (0xA2, b""), (0xA3, ZONE_WEST)],
    )

    assert [(cmd_hex(reply.frame), reply.status) for reply in replies] == [("082b", 0)]
    assert rig.module.rtc == rtc


def test_the_bound_account_outlives_the_link_but_not_the_module() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    exchange(rig.module, client, 0x023, [(0xA2, ACCOUNT)])
    set_time = [(0xA1, TIMESTAMP), (0xA2, ACCOUNT), (0xA3, ZONE_WEST)]
    rig.module.disconnect()
    rig.module.connect()

    kept = exchange(rig.module, AppClient(encrypted_outer=False), 0x02B, set_time)
    rebooted = exchange(
        build_module().module, AppClient(encrypted_outer=False), 0x02B, set_time
    )

    assert [reply.status for reply in kept] == [0x00]
    assert [reply.status for reply in rebooted] == [0x02]


def test_unknown_negotiation_opcode_gets_no_reply() -> None:
    rig = build_module()

    assert exchange(rig.module, connected_client(rig.module), 0x00B, []) == []


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


def test_an_mcu_request_on_fid_0x11_gets_no_reply() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(0x057, BLE_ROUTE, CHANNEL_APP))

    assert out.frames == []


@pytest.mark.parametrize(
    ("msgtype", "channel", "patterns"),
    [
        pytest.param(0x022, SESSION, ["03000f"], id="WiFi op on 0x0f"),
        pytest.param(0x022, CHANNEL_WIFI, ["030013"], id="WiFi op on 0x13"),
        pytest.param(0x022, CHANNEL_APP, [], id="WiFi op on 0x11"),
        pytest.param(0x030, CHANNEL_APP, ["030011"], id="module op on 0x11"),
        pytest.param(0x030, CHANNEL_WIFI, [], id="module op on 0x13"),
        pytest.param(0x100, CHANNEL_WIFI, [], id="MCU op on 0x13"),
    ],
)
def test_a_session_op_answers_only_on_the_channels_its_handler_serves(
    msgtype: int, channel: int, patterns: list[str]
) -> None:
    rig = build_module(session_replies={0x022: SCAN_WIFI_REPLY, 0x030: VERSION_REPLY})
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(client.request(msgtype, BLE_ROUTE, channel))

    replies = [client.open(frame) for frame in out.frames]
    assert [pattern_hex(reply.frame) for reply in replies if reply] == patterns


def test_the_stored_tz_replaces_a3_a_tenth_of_a_second_later() -> None:
    """a1783-control.md "Clock": the TZ timer fires 100 ms after ``0022``."""
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    utc = Int32ul.parse(TIMESTAMP)

    exchange(
        rig.module,
        client,
        0x022,
        [(0xA1, TIMESTAMP), (0xA3, Int32sl.build(3600)), (0xA5, TZ)],
    )
    at_once = rig.module.rtc
    rig.clock.advance(0.1)
    rig.module.check_pushes()

    assert at_once == utc - 3600
    assert rig.module.rtc == utc - 14400


def test_a_late_tz_tick_keeps_the_clock_continuous() -> None:
    """A tick run late (one long advance) re-sets the clock as of its whole second."""
    rig = build_module()
    negotiate(rig.module, AppClient(), ENROLLED_TOKEN)
    utc = Int32ul.parse(TIMESTAMP)

    rig.clock.advance(5.9)
    rig.module.check_pushes()
    rig.clock.advance(0.5)

    assert rig.module.epoch == utc + 6


def test_dst_switches_with_no_new_0022() -> None:
    """The TZ timer polls every 5 s within 300 s of a change (2026-11-01 06:00 UTC)."""
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    fall = calendar.timegm((2026, 11, 1, 6, 0, 0))
    exchange(
        rig.module,
        client,
        0x022,
        [(0xA1, Int32ul.build(fall - 60)), (0xA3, UTC_OFFSET), (0xA5, TZ)],
    )

    rig.clock.advance(59.9)
    rig.module.check_pushes()
    before = rig.module.epoch - rig.module.rtc
    rig.clock.advance(5.1)
    rig.module.check_pushes()

    assert before == 14400
    assert rig.module.epoch - rig.module.rtc == 18000


def test_a_bare_gmt0_takes_its_offset_from_a3() -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    exchange(
        rig.module,
        client,
        0x022,
        [(0xA1, TIMESTAMP), (0xA3, Int32sl.build(3600)), (0xA5, b"GMT0")],
    )
    rig.clock.advance(0.1)
    rig.module.check_pushes()

    assert rig.module.epoch - rig.module.rtc == 3600


def test_a_tz_the_parser_refuses_keeps_the_stored_zone() -> None:
    """The handshake's ``EST5EDT`` stays stored, and its timer overrides ``a3``."""
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    exchange(
        rig.module,
        client,
        0x022,
        [(0xA1, TIMESTAMP), (0xA3, Int32sl.build(3600)), (0xA5, b"America/New_York")],
    )
    at_once = rig.module.epoch - rig.module.rtc
    rig.clock.advance(0.1)
    rig.module.check_pushes()

    assert at_once == 3600
    assert rig.module.epoch - rig.module.rtc == 14400


def test_a_c_gen_2_mcu_keeps_its_offset_on_a_utc_zone() -> None:
    """MainMcu op ``0x26`` keeps the previous offset when it is 0 or -1."""
    rig = build_module()
    client = AppClient()
    rig.module.mcu = replace(rig.module.mcu, clock_keeps_blank=True)
    negotiate(rig.module, client, ENROLLED_TOKEN)

    exchange(
        rig.module,
        client,
        0x022,
        [(0xA1, TIMESTAMP), (0xA3, Int32sl.build(0)), (0xA5, b"UTC0")],
    )
    at_once = rig.module.epoch - rig.module.rtc
    rig.clock.advance(0.1)
    rig.module.check_pushes()

    assert at_once == 14400
    assert rig.module.epoch - rig.module.rtc == 14400
    assert A1783().mcu.clock_keeps_blank


def test_a_session_0022_is_the_wifi_scan_and_leaves_the_rtc_alone() -> None:
    rig = build_module(session_replies={0x022: SCAN_WIFI_REPLY})
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)
    rtc = rig.module.rtc
    later = bytes.fromhex("00000070")

    out = rig.module.write(
        client.request(0x022, [(0xA1, later), (0xA3, UTC_OFFSET)], SESSION)
    )

    reply = client.open(out.frames[0])
    assert reply is not None
    assert reply.plaintext == SCAN_WIFI_REPLY
    assert rig.module.rtc == rtc


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
            encode(make_frame(0x00, 0x01, 0x001, b"\xa1\x09")), id="bad fields"
        ),
        pytest.param(encode(make_frame(0x00, 0x10, 0x022)), id="other channel"),
    ],
)
def test_bad_writes_are_dropped(data: bytes) -> None:
    rig = build_module(enforce=False)

    out = rig.module.write(data)

    assert out.frames == []
    assert not out.disconnect


@pytest.mark.parametrize(
    ("channel", "msgtype", "cmd", "pattern"),
    [
        pytest.param(SESSION, 0x100, "4900", "03000f", id="session"),
        pytest.param(NEGOTIATION, 0x023, "4823", "030001", id="negotiation"),
    ],
)
def test_an_undecryptable_request_gets_a_sealed_01_and_the_link_stays_up(
    channel: int, msgtype: int, cmd: str, pattern: str
) -> None:
    """anker-crypto-and-negotiation.md "Decrypt-failure ``01``": 17 B, ``01`` + tag."""
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(
        encode(make_frame(0x00, channel, msgtype, bytes(20), encrypted=True))
    )

    reply = client.open(out.frames[0])
    assert reply is not None
    assert (cmd_hex(reply.frame), pattern_hex(reply.frame)) == (cmd, pattern)
    assert len(reply.frame.payload) == 17
    assert reply.plaintext == b"\x01"
    assert not out.disconnect
    assert rig.module.connected


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        pytest.param(b"", 0x08, id="empty"),
        pytest.param(bytes(8), 0x01, id="shorter than a tag"),
    ],
)
def test_a_gcm_request_with_no_room_for_a_tag(payload: bytes, status: int) -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(
        encode(make_frame(0x00, SESSION, 0x100, payload, encrypted=True))
    )

    reply = client.open(out.frames[0])
    assert reply is not None
    assert reply.plaintext == bytes([status])


def test_an_encrypted_connect_with_a_bad_body_gets_01_under_the_static_key() -> None:
    rig = build_module()

    out = rig.module.write(
        encode(make_frame(0x00, NEGOTIATION, 0x001, bytes(20), encrypted=True))
    )

    assert STATIC_GCM.decrypt(decode(out.frames[0]).payload) == b"\x01"


def test_an_encrypted_request_before_any_connect_meets_the_zeroed_cbc_context() -> None:
    """A GCM-sealed ``4003`` (29 B) isn't whole CBC blocks: ``08`` under zeros."""
    rig = build_module()

    replies = rig.module.write(
        AppClient().request(
            0x003, [(0xA1, TIMESTAMP), (0xA3, b"\x20"), (0xA4, b"\x00\xf0")]
        )
    )

    reply = decode(replies.frames[0])
    assert cmd_hex(reply) == "4803"
    assert CbcCipher(bytes(16), bytes(16)).decrypt(reply.payload) == b"\x08"


def test_a_new_link_keeps_the_last_links_port_context() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN, enforce=False)
    exchange(rig.module, AppClient(encrypted_outer=False), 0x001, [(0xA2, ACCOUNT)])
    rig.module.disconnect()
    rig.module.connect()
    context = CbcCipher(ACCOUNT.ljust(16, b"\0"), TEST_SERIAL[:16])
    zero_pad = context.encrypt(bytes(16))[:16]

    out = rig.module.write(
        encode(make_frame(0x00, NEGOTIATION, 0x003, zero_pad, encrypted=True))
    )

    assert context.decrypt(decode(out.frames[0]).payload) == b"\x01"


@pytest.mark.parametrize(
    "payload", [pytest.param(b"", id="empty"), pytest.param(bytes(20), id="bad tag")]
)
def test_an_undecryptable_key_exchange_is_dropped(payload: bytes) -> None:
    rig = build_module()
    client = AppClient()
    exchange(rig.module, client, 0x001, [])
    exchange(rig.module, client, 0x005, [(0xA5, b"\x44")])

    out = rig.module.write(
        encode(make_frame(0x00, NEGOTIATION, 0x021, payload, encrypted=True))
    )

    assert out.frames == []


@pytest.mark.parametrize(
    ("payload", "statuses"),
    [
        pytest.param(b"", [0x08], id="empty"),
        pytest.param(bytes(20), [], id="bad tag"),
    ],
)
def test_an_undecryptable_clock_set_gets_08_or_nothing(
    payload: bytes, statuses: list[int]
) -> None:
    rig = build_module()
    client = AppClient()
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(
        encode(make_frame(0x00, NEGOTIATION, 0x022, payload, encrypted=True))
    )

    replies = [client.open(data) for data in out.frames]
    assert [reply.status for reply in replies if reply] == statuses


def test_a_cbc_request_checks_only_its_last_pad_byte() -> None:
    """``decrypt_aes_cbc``: pad ``09 09 09 04`` strips 4 bytes uncompared."""
    rig = build_module(auth_mode=AuthMode.OPEN, enforce=False)
    client = AppClient(encrypted_outer=False)
    negotiate(rig.module, client, ENROLLED_TOKEN)
    assert client.session is not None
    block = b"\xa1\x01\x21\xa2\x07" + bytes(7) + b"\x09\x09\x09\x04"

    out = rig.module.write(
        encode(
            make_frame(
                0x00, SESSION, 0x057, client.session.encrypt(block)[:16], encrypted=True
            )
        )
    )

    reply = client.open(out.frames[0])
    assert reply is not None
    assert reply.plaintext == ACK_REPLY


@pytest.mark.parametrize(
    "payload",
    [pytest.param(b"", id="empty"), pytest.param(bytes(20), id="part block")],
)
def test_a_cbc_request_that_is_not_whole_blocks_gets_08(payload: bytes) -> None:
    rig = build_module(auth_mode=AuthMode.OPEN, enforce=False)
    client = AppClient(encrypted_outer=False)
    negotiate(rig.module, client, ENROLLED_TOKEN)

    out = rig.module.write(
        encode(make_frame(0x00, SESSION, 0x100, payload, encrypted=True))
    )

    reply = client.open(out.frames[0])
    assert reply is not None
    assert reply.plaintext == b"\x08"


def test_a_cbc_request_with_bad_padding_gets_01() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN, enforce=False)
    client = AppClient(encrypted_outer=False)
    negotiate(rig.module, client, ENROLLED_TOKEN)
    assert client.session is not None
    unpadded = client.session.encrypt(bytes(16))[:16]

    out = rig.module.write(
        encode(make_frame(0x00, SESSION, 0x100, unpadded, encrypted=True))
    )

    reply = client.open(out.frames[0])
    assert reply is not None
    assert reply.plaintext == b"\x01"


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
    client = connected_client(rig.module)
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
    exchange(rig.module, connected_client(rig.module), 0x027, [(0xA2, b"anyone")])

    out = rig.module.push(0x421)

    assert rig.module.authorized
    assert out.frames == []


def test_legacy_confer_on_the_plain_outer_delivers_a_cbc_session_key() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN)
    client = AppClient(encrypted_outer=False)
    exchange(rig.module, client, 0x001, [])
    exchange(rig.module, client, 0x005, [(0xA5, b"\x02")])
    handshake = CbcCipher(DEFAULT_ACCOUNT[:16], TEST_SERIAL[:16])

    out = rig.module.write(
        confer_request(handshake, [(0xA1, TIMESTAMP), (0xA3, UTC_OFFSET)])
    )

    status, key = open_confer_reply(handshake, out.frames[0])
    assert status == 0
    assert len(key) == 16
    assert rig.module.authorized

    session = CbcCipher(key, TEST_SERIAL[:16])
    probe = rig.module.write(
        encode(
            make_frame(
                0x00, SESSION, 0x057, session.encrypt(b"\xa1\x01\x21"), encrypted=True
            )
        )
    )
    assert session.decrypt(decode(probe.frames[0]).payload) == ACK_REPLY


def test_legacy_confer_uses_the_connect_account_as_the_handshake_key() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN)
    client = AppClient(encrypted_outer=False)
    account = b"an-account-id-40-bytes-long-------------"
    exchange(rig.module, client, 0x001, [(0xA2, account)])
    exchange(rig.module, client, 0x005, [(0xA5, b"\x02")])
    handshake = CbcCipher(account[:16], TEST_SERIAL[:16])

    out = rig.module.write(
        confer_request(handshake, [(0xA1, TIMESTAMP), (0xA3, UTC_OFFSET)])
    )

    status, key = open_confer_reply(handshake, out.frames[0])
    assert status == 0
    assert len(key) == 16


def test_legacy_confer_on_the_encrypted_outer_delivers_a_gcm_session_key() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN)
    client = AppClient()
    exchange(rig.module, client, 0x001, [])
    exchange(rig.module, client, 0x005, [(0xA5, b"\x02")])
    handshake = CbcCipher(STATIC_KEY, STATIC_IV)

    out = rig.module.write(
        confer_request(handshake, [(0xA1, TIMESTAMP), (0xA3, UTC_OFFSET)])
    )

    status, key = open_confer_reply(STATIC_GCM, out.frames[0])
    assert status == 0
    assert len(key) == 16
    assert rig.module.authorized

    session = GcmCipher(key, STATIC_NONCE)
    probe = rig.module.write(
        encode(
            make_frame(
                0x00, SESSION, 0x057, session.encrypt(b"\xa1\x01\x21"), encrypted=True
            )
        )
    )
    assert session.decrypt(decode(probe.frames[0]).payload) == ACK_REPLY


def test_legacy_confer_without_the_encrypted_flag_gets_no_reply() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN)
    client = AppClient(encrypted_outer=False)
    exchange(rig.module, client, 0x001, [])
    exchange(rig.module, client, 0x005, [(0xA5, b"\x02")])

    out = rig.module.write(
        encode(
            make_frame(
                0x00,
                NEGOTIATION,
                0x022,
                encode_fields([(0xA1, TIMESTAMP), (0xA3, UTC_OFFSET)]),
            )
        )
    )

    assert out.frames == []
    assert not rig.module.authorized


def test_legacy_confer_with_a_missing_field_gets_no_reply() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN)
    client = AppClient(encrypted_outer=False)
    exchange(rig.module, client, 0x001, [])
    exchange(rig.module, client, 0x005, [(0xA5, b"\x02")])
    handshake = CbcCipher(DEFAULT_ACCOUNT[:16], TEST_SERIAL[:16])

    out = rig.module.write(confer_request(handshake, [(0xA1, TIMESTAMP)]))

    assert out.frames == []


def test_legacy_confer_with_malformed_fields_gets_no_reply() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN)
    client = AppClient(encrypted_outer=False)
    exchange(rig.module, client, 0x001, [])
    exchange(rig.module, client, 0x005, [(0xA5, b"\x02")])
    handshake = CbcCipher(DEFAULT_ACCOUNT[:16], TEST_SERIAL[:16])

    out = rig.module.write(
        encode(
            make_frame(
                0x00, NEGOTIATION, 0x022, handshake.encrypt(b"\xa1\x09"), encrypted=True
            )
        )
    )

    assert out.frames == []


def test_legacy_confer_with_undecryptable_ciphertext_gets_no_reply() -> None:
    rig = build_module(auth_mode=AuthMode.OPEN)
    client = AppClient(encrypted_outer=False)
    exchange(rig.module, client, 0x001, [])
    exchange(rig.module, client, 0x005, [(0xA5, b"\x02")])

    out = rig.module.write(
        encode(make_frame(0x00, NEGOTIATION, 0x022, bytes(16), encrypted=True))
    )

    assert out.frames == []
