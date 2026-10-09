# Copyright (c) 2026 Shawn Stricker
"""Products built from anker-solix-api's maps and SolixBLE's device classes."""

import json

import pytest
from bleak import BleakClient

from anker_ble_emulator import EmulatedBleakBackend, Outer, Product
from anker_ble_emulator.devices import MAP_BUILT, PROFILES, EmulatedDevice, Profile
from anker_ble_emulator.layouts import NUMERIC_SIZES, Layout
from anker_ble_emulator.tlv import decode_fields
from tests.fixtures.client import SESSION, AppClient, BleakLink, settle
from tools.generate_profiles import DEVICES, REPORT, SOLIXBLE, generate, generated


TOKEN = b"owner-token"
#: The products a map and a SolixBLE class cover, with no recorded profile.
EXPECTED = {
    Product.A1723,
    Product.A1726,
    Product.A1728,
    Product.A1753,
    Product.A1754,
    Product.A1755,
    Product.A1781,
    Product.A1790,
    Product.A1790P,
    Product.A17C5,
}


def test_map_built_products_are_those_with_a_class_a_map_and_no_recording() -> None:
    assert set(MAP_BUILT) == EXPECTED
    assert all(PROFILES[pn].map_built for pn in MAP_BUILT)
    assert not PROFILES[Product.A1783].map_built


@pytest.mark.parametrize("pn", sorted(EXPECTED))
async def test_a_map_built_product_connects_and_sends_its_built_telemetry(
    pn: Product,
) -> None:
    device = EmulatedDevice(pn)
    profile = device.profile
    assert isinstance(profile, Profile)
    async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
        link = BleakLink(
            client, AppClient(encrypted_outer=device.outer == Outer.ENCRYPTED)
        )
        await link.start()
        await link.negotiate(TOKEN)
        status = [
            reply
            for request in profile.replies
            for reply in await link.send(request, [(0xA1, b"\x21")], SESSION)
        ]
        for msgtype in profile.pushes:
            device.push(msgtype)
            await settle()
        version = await link.send(0x030, [(0xA1, b"\x21")], SESSION)

    pushed = link.replies[-len(profile.pushes) - len(version) :][: len(profile.pushes)]
    assert [reply.frame.cmd.msgtype for reply in pushed] == list(profile.pushes)
    assert all(reply.plaintext.startswith(b"\xa1\x01\x31") for reply in pushed)
    assert all(reply.plaintext.startswith(b"\x00\xa1\x01\x31") for reply in status)
    assert version == []
    assert device.advertisement_data.manufacturer_data == {}


@pytest.mark.parametrize("pn", sorted(EXPECTED))
def test_a_map_built_product_takes_values_by_its_map_names(pn: Product) -> None:
    device = EmulatedDevice(pn)
    assert device.layout is not None
    layout = device.layout
    assert isinstance(device.profile, Profile)
    telemetry = device.profile.pushes[0]
    kinds: dict[str, set[int]] = {}
    for fields in layout.messages.values():
        for each in fields:
            if each.name:
                kinds.setdefault(each.name, set()).add(each.kind)
    field = next(
        each
        for each in layout.messages[telemetry]
        if each.name and kinds[each.name] <= set(NUMERIC_SIZES)
    )
    name = str(field.name)

    device.set_values(**{name: 1})

    payload = device.mcu.push(telemetry, values=device.negotiated_module.values).payload
    assert decode_fields(payload)[field.tag][1:] == (1).to_bytes(field.size, "little")


def test_messages_the_map_cant_type_are_left_out() -> None:
    layout = Layout.load(Product.A1728)
    assert layout is not None
    facts = {
        "outer": "encrypted",
        "telemetry": ["300"],
        "status": ["040", "999"],
        "commands": [{"method": "raw", "cmd": None, "fields": {}, "arguments": {}}],
    }

    profile = generated(facts, layout)

    assert profile.pushes == ()
    assert profile.replies == {}
    assert profile.outer == Outer.ENCRYPTED


def test_a_field_the_map_leaves_untyped_is_typed_from_solixble_s_read() -> None:
    device = EmulatedDevice(Product.A1753)

    device.set_values(ac_input_power=55, device_sn=b"A1753SYNTHETIC01")

    payload = device.mcu.push(0x402, values=device.negotiated_module.values).payload
    fields = decode_fields(payload)
    assert fields[0xA5] == b"\x03" + (55).to_bytes(4, "little")
    assert fields[0xD0] == b"\x00A1753SYNTHETIC01"


def test_the_generated_profiles_and_crosscheck_are_current() -> None:
    files, report = generate(json.loads(SOLIXBLE.read_text()), DEVICES)

    assert {path: path.read_text() for path in files} == files
    assert sorted((DEVICES / "generated").glob("*.py")) == sorted(files)
    assert REPORT.read_text() == report
