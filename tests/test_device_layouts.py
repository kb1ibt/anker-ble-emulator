# Copyright (c) 2026 Shawn Stricker
"""Devices with their layouts, and the cloud switch."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest
from bleak import BleakClient
from bleak.exc import BleakError
from construct import Int32ul

from anker_ble_emulator import (
    A17C0,
    A91B2,
    A1763,
    A1765,
    A1783,
    A1785,
    A2345,
    A2687,
    AS200,
    EmulatedBleakBackend,
    ManualClock,
)
from anker_ble_emulator.c_g2_490 import MSGTYPE_490
from anker_ble_emulator.devices import EmulatedDevice
from anker_ble_emulator.devices.solix_c_gen2 import AcOutput, Battery, Settings
from anker_ble_emulator.layouts import LayoutError
from anker_ble_emulator.mcu import GIVEN, McuScript, given, name_set
from anker_ble_emulator.parameters import (
    ParameterDict,
    Parameters,
    ScalarFieldLayout,
    TableLayout,
    decode_field,
    stamp_clock,
)
from anker_ble_emulator.products import Product
from anker_ble_emulator.timed import Step, Timed
from tests.fixtures.cgen2 import authorized_client
from tests.fixtures.cgen2 import command as cgen2_command
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle
from tests.fixtures.prime import command, device_clock, prime_client


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from anker_ble_emulator.layouts import Value


TOKEN = b"owner-token"


def test_every_device_loads_its_layout() -> None:
    assert A1783().layout is not None
    assert A2345().layout is not None


def test_a_telemetry_value_set_by_name_reaches_the_recorded_frame() -> None:
    device = A1783()

    device.set_values(battery_level=55)

    push = device.negotiated_module.mcu.push(
        0x421, values=device.negotiated_module.values
    )
    battery = decode_field(Parameters.parse(push.payload), "a5", Battery)
    assert battery.battery_level == 55
    recorded = device.negotiated_module.mcu.push(0x421)
    recorded_battery = decode_field(Parameters.parse(recorded.payload), "a5", Battery)
    assert recorded_battery.battery_level != 55


def test_a_value_set_on_the_device_reaches_every_message_that_carries_it() -> None:
    device = A1783()
    script, push_490 = device.mcu, device.mcu.push_490
    assert push_490 is not None

    device.set_values(battery_level=55)

    status, telemetry = script.respond(0x100, values=device.negotiated_module.values)
    post = script.push(0x490, values=device.negotiated_module.values)
    for frame in (status, telemetry):
        fields = Parameters.parse(frame.payload)
        assert decode_field(fields, "a5", Battery).battery_level == 55
    assert push_490.read(post.payload)["battery_level"] == 55


def test_a_settings_value_lands_in_the_settings_block() -> None:
    device = A1783()

    device.set_values(screen_off_time=300)

    push = device.mcu.push(0x421, values=device.negotiated_module.values)
    settings = decode_field(Parameters.parse(push.payload), "a4", Settings)
    assert settings.screen_off_time == 300


def test_an_mcu_without_a_layout_sends_its_frames_as_recorded() -> None:
    script = A1783().mcu
    quiet = McuScript(pushes=script.pushes)

    push = quiet.push(0x421, values={"battery_level": 55})

    assert push.payload == script.push(0x421).payload


def test_a_value_the_layout_doesnt_type_is_refused() -> None:
    device = A1783()

    with pytest.raises(LayoutError):
        device.set_values(no_such_field=1)


def test_a_read_reply_sets_its_status_or_stays_silent_and_leads_its_pushes() -> None:
    """A read's status beyond ``00``, its silence, and pushes sent before the ack.

    prime-a2345-a91b2.md "Theme upload" (``4221``: ``10``/``11``/``12``, most
    chunks unanswered, ``4311`` before the last ``4a21``) and ``4208`` (``0308``
    before ``0a08``); a clocked push carries the unit's clock, not the request's.
    """

    def read(
        request: ParameterDict, _state: Mapping[str, Value]
    ) -> ParameterDict | None:
        if "a2" not in request:
            return None
        return ParameterDict({"a2": request["a2"]}, prefix=b"\x10")

    def push(
        _request: ParameterDict,
        _before: Mapping[str, Value],
        _after: Mapping[str, Value],
    ) -> ParameterDict:
        return ParameterDict()

    class Reader(TableLayout):
        COMMANDS = MappingProxyType({0x221: lambda _request: {}})
        READ_REPLIES = MappingProxyType({0x221: read})
        FOLLOW_UPS = MappingProxyType({0x221: ((0x311, push), (0x312, push))})
        LEADING = MappingProxyType({0x221: frozenset({0x311})})
        UNCLOCKED = frozenset({0x312})

    layout = Reader()
    script = McuScript(layout=layout)

    answered = script.respond(
        0x221, request=bytes.fromhex("a10121a2020105fe0503010e5f6a"), epoch=0x6A5F0E00
    )
    silent = script.respond(0x221, request=bytes.fromhex("a10121"))

    assert [frame.cmd.msgtype for frame in answered] == [0x311, 0xA21, 0x312]
    assert answered[1].payload == bytes.fromhex("10a10131a2020105")
    assert answered[0].payload == bytes.fromhex("a10131fe0503000e5f6a")
    assert answered[2].payload == bytes.fromhex("a10131")
    assert [frame.cmd.msgtype for frame in silent] == [0x311, 0x312]
    assert layout.reply(0x221, bytes.fromhex("a10121"), {}) is None


def test_a_device_shows_what_it_published_to_mqtt() -> None:
    assert A2345().mqtt_published == []


def test_a_scalar_layout_publishes_nothing_for_a_held_push() -> None:
    assert ScalarFieldLayout().mqtt_routed(0x421, b"", {}) == ()


def test_a_layout_starts_its_timers_with_the_mcu_and_again_on_a_new_script() -> None:
    """A free-running MCU timer (the C Gen 2 c490 post) runs from the MCU's start."""

    def tick(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
        return Step(values={"_ticks": int(state.get("_ticks", 0)) + 1})

    class Ticking(TableLayout):
        def started(self) -> tuple[Timed, ...]:  # a layout hook
            return (Timed(5.0, "tick", tick),)

    clock = ManualClock()
    device = A2345(clock=clock)
    module = device.negotiated_module
    default = not TableLayout().started()

    device.use_mcu(McuScript(layout=Ticking()))
    clock.advance(5.0)
    module.check_pushes()

    assert default
    assert module.values == {"_ticks": 1}


def test_what_fell_due_runs_before_a_change_of_state() -> None:
    """An overdue event sees the state it fell due in, not the change after it."""
    seen: list[int] = []

    def tick(_values: Mapping[str, Value], state: Mapping[str, Value]) -> Step:
        seen.append(int(state["cloud"]))
        return Step(values={"_level": len(seen)})

    clock = ManualClock()
    device = A2345(clock=clock)
    module = device.negotiated_module

    changes: list[Callable[[], None]] = [
        lambda: device.set_cloud(on=True),
        lambda: device.set_values(screen_brightness=3),
        lambda: device.unit_event("not_modeled"),
    ]
    for change in changes:
        module.schedule(Timed(1.0, "tick", tick))
        clock.advance(2.0)
        change()

    assert seen == [0, 1, 1]


def test_a_legacy_device_takes_the_cloud_switch_without_a_negotiated_module() -> None:
    device = EmulatedDevice(Product.A1780)

    device.set_cloud(True)

    assert device.cloud


def test_the_state_carries_what_the_recorded_cloud_pushes_carry() -> None:
    """The A1783's cloud-only ``0503`` names its totals in the state too."""
    state = A1783().mcu.state({})

    assert {"peak_time", "mid_peak_time", "off_peak_time"} <= state.keys()


def test_a_unit_event_no_layout_models_changes_nothing() -> None:
    device = A2345()

    device.unit_event("not_modeled")

    assert device.negotiated_module.values == {}


def test_a_unit_event_carries_its_own_hidden_values() -> None:
    device = A2345()

    device.unit_event("plug", _plugged_vid=0x05AC)

    assert device.negotiated_module.values == {"_plugged_vid": 0x05AC}
    with pytest.raises(LayoutError, match="hidden"):
        device.unit_event("plug", battery_level=1)


def test_a_device_without_a_layout_or_push_490_takes_no_values() -> None:
    device = A2345()
    device.layout = None

    with pytest.raises(LayoutError, match="sends no typed field"):
        device.set_values(battery_level=1)


async def test_a_mapped_command_without_a_recording_gets_a_checked_ack() -> None:
    device = A2345(clock=ManualClock())
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        port = [(0xA1, b"\x21"), (0xA2, b"\x01\x00")]
        off = await link.send(0x207, [*port, (0xA3, b"\x01\x00")], SESSION)
        bad = await link.send(0x207, [*port, (0xA3, b"\x01\x00\x00")], SESSION)
        unmapped = await link.send(0x215, [(0xA1, b"\x21")], SESSION)

    assert [cmd_hex(reply.frame) for reply in off] == ["4a07"]
    assert off[0].plaintext.hex() == "00a10131a2020100" + device_clock(0)
    assert bad[0].plaintext == bytes.fromhex("04a10131")
    assert unmapped == []


async def test_a_value_set_by_name_reaches_the_client() -> None:
    device = A1783()
    device.set_values(battery_level=42)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        status = await link.send(0x100, [(0xA1, b"\x21")], SESSION)

    telemetry = next(reply for reply in status if cmd_hex(reply.frame) == "4421")
    fields = Parameters.parse(telemetry.plaintext)
    assert decode_field(fields, "a5", Battery).battery_level == 42


def test_replies_and_pushes_can_be_set_and_silenced() -> None:
    device = A1783()

    device.set_reply(
        0x100, (0x900, bytes.fromhex("00a10131")), (0x421, b"\xa1\x01\x31")
    )
    device.set_push(0x421, bytes.fromhex("a10131a20101"))

    assert [frame.cmd.msgtype for frame in device.mcu.respond(0x100)] == [0x900, 0x421]
    assert device.mcu.push(0x421).payload == bytes.fromhex("a10131a20101")
    device.set_reply(0x100)
    assert device.mcu.respond(0x100) == []
    prime = A2345()
    prime.set_reply(0x207, (0xA07, b"\xa1\x01\x31"))
    assert prime.mcu.respond(0x207)[0].payload == b"\xa1\x01\x31"
    device.use_mcu(McuScript())
    assert device.mcu.respond(0x057) == []
    with pytest.raises(KeyError):
        device.mcu.push(0x421)


async def test_an_accepted_setter_shows_in_the_telemetry_after_it() -> None:
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        on = await link.send(0x101, [(0xA1, b"\x21"), (0xA2, b"\x01\x01")], SESSION)
        off = await link.send(0x101, [(0xA1, b"\x21"), (0xA2, b"\x01\x00")], SESSION)
        bad = await link.send(0x101, [(0xA1, b"\x21"), (0xA2, b"\x01\x02")], SESSION)
        untyped = await link.send(0x101, [(0xA1, b"\x21"), (0xA2, b"\x01")], SESSION)

    switch = [
        decode_field(Parameters.parse(reply.plaintext), "a7", AcOutput).ac_output_status
        for reply in (*on, *off, *bad, *untyped)
        if cmd_hex(reply.frame) == "4421"
    ]
    assert [cmd_hex(reply.frame) for reply in on] == ["4901", "4421"]
    assert switch == [1, 0, 2, 1]
    assert [reply.plaintext for reply in (on[0], bad[0], untyped[0])] == [
        bytes.fromhex("00a10131")
    ] * 3


def test_the_default_mtu_matches_the_bench_253_fragment_cap() -> None:
    device = A1783()

    assert device.module.fragment_cap == 253


def test_an_explicit_mtu_raises_the_fragment_cap() -> None:
    device = A1783(mtu=517)

    assert device.module.fragment_cap == 514
    push = device.negotiated_module.mcu.push(0x421)
    assert len(push.payload) <= device.module.fragment_cap


def test_a_legacy_device_rejects_an_explicit_mtu() -> None:
    with pytest.raises(TypeError, match="mtu"):
        EmulatedDevice(Product.A1780, mtu=517)


async def test_going_on_the_cloud_drops_the_link_and_refuses_connects() -> None:
    device = A1783()
    client = BleakClient(device.ble_device, backend=EmulatedBleakBackend)
    await client.connect()

    device.set_cloud(True)
    await settle()

    assert not client.is_connected
    with pytest.raises(BleakError, match="cloud"):
        await client.connect()
    device.set_cloud(False)
    await client.connect()
    assert client.is_connected
    await client.disconnect()


@pytest.mark.parametrize("model", [A2345, A91B2])
def test_a_prime_replays_its_recorded_frames_on_its_own_clock(
    model: type[A2345 | A91B2],
) -> None:
    """``0303``, ``4a00`` (and the A2345's ``4a02``) carry the unit's clock.

    prime-a2345-a91b2.md "Commands", "Reply ``a1``": a reply's or push's ``fe``
    is the unit's RTC when sent; it ticks with the clock, not the recording.
    """
    clock = ManualClock()
    device = model(clock=clock)
    client = prime_client(device, TOKEN)
    reads = [0x200, 0x202] if model is A2345 else [0x200]

    clock.advance(7)
    early = [frame for read in reads for frame in command(device, client, read, [])]
    clock.advance(5)
    later = [frame for read in reads for frame in command(device, client, read, [])]
    stream = command(device, client, 0x20B, [])

    stamps = [
        (cmd, "fe0503" + Parameters.parse(bytes.fromhex(body))["fe"]["value"].hex())
        for cmd, body in [*early, *later, *stream]
        if cmd != "4a0b"
    ]
    replies = [f"4a{read & 0xFF:02x}" for read in reads]
    assert stamps == [
        *((cmd, device_clock(7)) for cmd in replies),
        *((cmd, device_clock(12)) for cmd in replies),
        ("4303", device_clock(12)),
    ]


def test_a_c_gen_2_replays_its_recorded_frames_on_its_own_clock() -> None:
    """``0900`` and ``0421`` carry the unit's clock, ticking with it.

    a1783-telemetry.md: ``fe`` is ``msg_timestamp``, the device clock every
    recorded frame is read by.
    """
    clock = ManualClock()
    device = A1783(clock=clock)
    client = authorized_client(device, TOKEN)
    module = device.negotiated_module

    status = cgen2_command(device, client, 0x100, [])
    first = module.epoch
    clock.advance(9)
    again = cgen2_command(device, client, 0x100, [])

    stamps = [
        (reply.frame.cmd.msgtype, Parameters.parse(reply.plaintext)["fe"]["value"])
        for reply in [*status, *again]
    ]
    assert first != 0
    assert stamps == [
        (0x900, Int32ul.build(first)),
        (0x421, Int32ul.build(first)),
        (0x900, Int32ul.build(first + 9)),
        (0x421, Int32ul.build(first + 9)),
    ]


def test_set_values_judges_its_values_after_what_fell_due() -> None:
    """An event overdue at ``set_values`` runs first, so the check sees its change.

    AC out over 0 W is refused only while AC output is off; here an overdue
    event has switched it on.
    """
    clock = ManualClock()
    device = A1783(clock=clock)

    def switch_on(_values: Mapping[str, Value], _state: Mapping[str, Value]) -> Step:
        return Step(values={"ac_output_status": 1})

    device.negotiated_module.schedule(Timed(0.5, "switch_on", switch_on))
    clock.advance(1)
    device.set_values(ac_output_power=500)

    assert device.negotiated_module.values["ac_output_power"] == 500


def test_before_any_clock_set_a_frame_carries_the_clock_counted_from_boot() -> None:
    """The stamp is the unit's clock when sent, which counts from 0 until a set."""
    clock = ManualClock()
    module = A2345(clock=clock).negotiated_module
    clock.advance(5)

    pushed = module.mcu.push(
        0x303, values=module.values, rtc=module.rtc, epoch=module.epoch
    )

    assert Parameters.parse(pushed.payload)["fe"]["value"] == Int32ul.build(5)


@pytest.mark.parametrize("model", [AS200, A17C0], ids=["AS200", "A17C0"])
def test_a_layout_that_names_fe_replays_it_as_it_names_it(
    model: type[AS200 | A17C0],
) -> None:
    """The clock stamp leaves a frame whose layout names ``fe`` byte-identical."""
    mcu = model().negotiated_module.mcu

    for msgtype in mcu.pushes:
        assert mcu.push(msgtype, epoch=0x6A000000) == mcu.push(msgtype)
    for request in mcu.replies:
        assert mcu.respond(request, epoch=0x6A000000) == mcu.respond(request)


@pytest.mark.parametrize("request_msgtype", [0x20A, 0x20B], ids=["4a0a", "4a0b"])
def test_a_replayed_frame_without_fe_gains_none(request_msgtype: int) -> None:
    """Only a recorded ``fe`` is stamped: an A2345 reply recorded without one."""
    mcu = A2345().negotiated_module.mcu

    frames = mcu.respond(request_msgtype, epoch=0x6A000000)

    assert all("fe" not in Parameters.parse(frame.payload) for frame in frames)
    assert frames == mcu.respond(request_msgtype)


def test_a_device_without_a_layout_replays_its_fe_as_recorded() -> None:
    """No layout, no stamp: the A2687's recorded ``0300`` keeps its ``fe``."""
    mcu = A2687().negotiated_module.mcu

    assert mcu.push(0x300, epoch=0x6A000000) == mcu.push(0x300)


@pytest.mark.parametrize(
    "model",
    [A2345, A91B2, A1763, A1765, A1783, A1785],
    ids=["A2345", "A91B2", "A1763", "A1765", "A1783", "A1785"],
)
def test_the_clock_stamp_moves_only_the_fe_value_bytes(
    model: type[A2345 | A91B2 | A1763 | A1765 | A1783 | A1785],
) -> None:
    """Every recorded ``fe`` frame the stamp reaches changes in its ``fe`` u32 only."""
    mcu = model().negotiated_module.mcu
    recorded = [
        frame.payload
        for frame in [
            *mcu.pushes.values(),
            *(frame for frames in mcu.replies.values() for frame in frames),
        ]
        if frame.cmd.msgtype != MSGTYPE_490 and "fe" in Parameters.parse(frame.payload)
    ]
    epoch = Int32ul.build(0x01020304)

    stamped = [stamp_clock(payload, 0x01020304) for payload in recorded]

    at = [payload.rindex(b"\xfe\x05\x03") + 3 for payload in recorded]
    assert recorded
    assert stamped == [
        payload[:start] + epoch + payload[start + 4 :]
        for payload, start in zip(recorded, at, strict=True)
    ]


def test_name_set_reads_back_what_given_joins() -> None:
    """``name_set`` is ``given``'s inverse: none unset, a non-bytes value refused."""
    joined = {GIVEN: given(["screen_brightness", "ac_input_power"])}

    assert name_set(joined, GIVEN) == {"screen_brightness", "ac_input_power"}
    assert name_set({}, GIVEN) == frozenset()
    with pytest.raises(TypeError, match="given is a bytes value"):
        name_set({GIVEN: 1}, GIVEN)
