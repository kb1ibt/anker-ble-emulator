# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C Gen 2 line (A1763, A1765, A1783, A1785): one build, four models."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

import pytest
from bleak import BleakClient

from anker_ble_emulator import (
    A1763,
    A1765,
    A1783,
    A1785,
    EmulatedBleakBackend,
    ModuleBuild,
)
from anker_ble_emulator.devices import COMPANY_ID, solix_c_gen2
from tests.fixtures.client import SESSION, AppClient, BleakLink, cmd_hex, settle


if TYPE_CHECKING:
    from collections.abc import Callable

    from anker_ble_emulator import EmulatedDevice

    Model: TypeAlias = Callable[..., EmulatedDevice]

TOKEN = b"owner-token"
#: The A1783's recorded ``0830``: v0.3.3.0, v1.2.1.6 and its OTA type names.
A1783_VERSIONS = bytes.fromhex(
    "00a10876302e332e332e30a20876312e322e312e36a30941313738335f6c6f77"
    "a40d41313738335f6d63755f6c6f77a50f41313738335f65737033325f6c6f77"
)
MODELS = [A1763, A1765, A1783, A1785]
#: Class, advert name, manufacturer record, synthetic serial, PN in the frames.
IDENTITIES = [
    pytest.param(
        A1763,
        "SOLIX C1000 Gen 2",
        "02aa12deadbeef01b118444b393604",
        "AXDDK960000000001",
        b"\x05A1763",
        id="A1763",
    ),
    pytest.param(
        A1765,
        "SOLIX C1000X Gen 2",
        "02aa12deadbeef01b119444b393604",
        "AXDDK960000000001",
        b"\x05A1763",
        id="A1765",
    ),
    pytest.param(
        A1783,
        "SOLIX C2000 Gen 2",
        "02aa12deadbeef01b11a444b4b4504",
        "APCDKKE0000000001",
        b"\x05A1783",
        id="A1783",
    ),
    pytest.param(
        A1785,
        "SOLIX C2000X Gen 2",
        "02aa12deadbeef01b11b444b565004",
        "AXDDKVP0000000001",
        b"\x05A1783",
        id="A1785",
    ),
]


@pytest.mark.parametrize(("model", "name", "record", "serial", "pn"), IDENTITIES)
def test_each_model_advertises_its_own_identity(
    model: Model, name: str, record: str, serial: str, pn: bytes
) -> None:
    device = model()

    assert device.local_name == name
    assert device.serial == serial
    assert device.advertisement_data.manufacturer_data[COMPANY_ID].hex() == record
    assert pn in device.module.mcu.respond(0x100)[0].payload


@pytest.mark.parametrize(("model", "name", "record", "serial", "pn"), IDENTITIES)
def test_status_frames_carry_the_model_serial(
    model: Model, name: str, record: str, serial: str, pn: bytes
) -> None:
    script = model().module.mcu

    for frame in [*script.respond(0x100), script.push(0x421)]:
        assert serial.encode() in frame.payload


@pytest.mark.parametrize("model", MODELS)
def test_every_model_answers_the_line_s_commands(model: Model) -> None:
    script = model().module.mcu

    for request, replies in solix_c_gen2.REPLIES.items():
        assert [frame.cmd.msgtype for frame in script.respond(request)] == list(
            replies
        ), f"{request:03x}"


@pytest.mark.parametrize("model", MODELS)
def test_recorded_frames_are_routed_to_ble(model: Model) -> None:
    device = model()
    script = device.module.mcu
    frames = [
        frame for request in solix_c_gen2.REPLIES for frame in script.respond(request)
    ]
    frames += [script.push(msgtype) for msgtype in device.profile.pushes]

    assert all(
        frame.payload.startswith((b"\xa1\x01\x31", b"\x00\xa1\x01\x31"))
        for frame in frames
    )


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("build", list(ModuleBuild))
def test_only_module_v0_3_3_0_enforces(model: Model, build: ModuleBuild) -> None:
    device = model(module=build)

    assert device.module_build is build
    assert device.module.config.enforce is (build is ModuleBuild.V0_3_3_0)
    assert model().module_build is ModuleBuild.V0_3_3_0


async def test_a1763_on_module_v0_3_0_6_negotiates_in_clear() -> None:
    device = A1763(module=ModuleBuild.V0_3_0_6)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient(encrypted_outer=False))
        await link.start()
        await link.negotiate(TOKEN)

        authorized = device.module.authorized
        status = await link.send(0x100, [(0xA1, b"\x21")], SESSION)
        setter = await link.send(0x101, [(0xA1, b"\x21")], SESSION)

    assert authorized
    assert [cmd_hex(reply.frame) for reply in status] == ["4900", "4421"]
    assert [cmd_hex(reply.frame) for reply in setter] == ["4901", "4421"]
    assert setter[0].plaintext == bytes.fromhex("00a10131")


async def test_a1785_grants_by_button_then_answers_the_backup_plan() -> None:
    device = A1785()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        replies = await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        plan = await link.send(0x05E, [(0xA1, b"\x21")], SESSION)

    assert replies[0x827].status == 0x09
    assert [cmd_hex(reply.frame) for reply in plan] == ["485e", "4421"]


async def test_a1783_version_read_matches_the_recorded_reply() -> None:
    device = A1783()
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(client, AppClient())
        await link.start()
        await link.negotiate(TOKEN)
        device.press_button()
        await settle()
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    assert [cmd_hex(reply.frame) for reply in version] == ["4830"]
    assert version[0].plaintext == A1783_VERSIONS


@pytest.mark.parametrize("build", list(ModuleBuild))
def test_version_read_reports_the_chosen_module_build(build: ModuleBuild) -> None:
    versions = A1785(module=build).module.config.versions

    assert versions is not None
    assert versions.module == build.value.encode()
    assert versions.model == b"A1783_low"


def test_recorded_module_replies_come_only_with_their_build() -> None:
    assert set(A1783().module.config.session_replies) == {
        0x020,
        0x028,
        0x02E,
        0x02F,
        0x036,
        0x038,
    }
    assert A1783(module=ModuleBuild.V0_3_0_6).module.config.session_replies == {}
    assert A1763().module.config.session_replies == {}
