# Copyright (c) 2026 Shawn Stricker
"""Emulated devices: profile defaults, options, the advertisement and recorded data."""

import inspect

import pytest
from construct import ConstructError

from anker_ble_emulator import (
    A91B2,
    A1763,
    A1765,
    A1783,
    A1785,
    A2345,
    PRODUCTS,
    EmulatedDevice,
    ModuleBuild,
    Outer,
    Path,
    Product,
    Transport,
)
from anker_ble_emulator.devices import COMPANY_ID, SERVICE_UUID, Advert


def test_a1783_defaults() -> None:
    device = A1783()

    assert device.pn == Product.A1783
    assert device.serial == "APCDKKE0000000001"
    assert device.address == "AA:12:DE:AD:BE:EF"
    assert device.transport == Transport.NEGOTIATED
    assert device.outer == Outer.ENCRYPTED
    assert device.path == Path.ECDH
    assert device.negotiated_module.config.enforce
    assert device.negotiated_module.config.serial == b"APCDKKE0000000001"


def test_product_constructor_builds_the_same_device() -> None:
    device = EmulatedDevice(Product.A1783, "APCDKKE0000000009", "02:00:00:00:00:01")

    assert device.serial == "APCDKKE0000000009"
    assert device.mac == bytes.fromhex("020000000001")
    assert device.outer == Outer.ENCRYPTED


@pytest.mark.parametrize(
    "device_class", [EmulatedDevice, A1763, A1765, A1783, A1785, A2345, A91B2]
)
def test_outer_path_and_module_are_keyword_only(
    device_class: type[EmulatedDevice],
) -> None:
    parameters = inspect.signature(device_class).parameters

    assert parameters["outer"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["path"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["module"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["transport"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_no_serial_leaves_it_out_of_the_module() -> None:
    device = A1783(serial=None)

    assert device.serial is None
    assert device.negotiated_module.config.serial is None


def test_plain_outer_drops_the_enforcement() -> None:
    device = A1783(outer=Outer.PLAIN)

    assert device.module_build is ModuleBuild.V0_3_3_0
    assert not device.negotiated_module.config.enforce


def test_outer_and_module_are_exclusive() -> None:
    with pytest.raises(TypeError, match="not both"):
        A1783(outer=Outer.PLAIN, module=ModuleBuild.V0_3_0_6)


@pytest.mark.parametrize(
    ("pn", "transport", "path", "reason"),
    [
        pytest.param(
            Product.A1783,
            Transport.LEGACY,
            None,
            "no legacy emulation profile",
            id="legacy transport",
        ),
        pytest.param(
            Product.A1783, None, Path.LEGACY, "not emulated yet", id="legacy path"
        ),
        pytest.param(
            Product.A1783,
            Transport.T2215,
            None,
            "not emulated yet",
            id="unimplemented transport",
        ),
        pytest.param(
            Product.A1780,
            Transport.NEGOTIATED,
            None,
            "no emulation profile",
            id="legacy product forced negotiated",
        ),
    ],
)
def test_unemulated_choices_are_refused(
    pn: Product, transport: Transport | None, path: Path | None, reason: str
) -> None:
    with pytest.raises(NotImplementedError, match=reason):
        EmulatedDevice(pn, transport=transport, path=path)


@pytest.mark.parametrize("mac", ["AA:12:DE:AD:BE", "AA:12:DE:AD:BE:EF:00"])
def test_mac_must_be_six_bytes(mac: str) -> None:
    with pytest.raises(ValueError, match="not 6"):
        A1783(mac=mac)


def test_advertisement_matches_the_manufacturer_record() -> None:
    advert = A1783().advertisement_data

    assert advert.local_name == "SOLIX C2000 Gen 2"
    assert advert.service_uuids == [SERVICE_UUID]
    assert advert.manufacturer_data[COMPANY_ID] == bytes.fromhex(
        "02aa12deadbeef01b11a444b4b4504"
    )


def test_advert_without_a_capability_ends_at_the_sku() -> None:
    advert = Advert(
        local_name="Anker SOLIX F3800",
        version_code=0x01,
        bind_type=0x02,
        product_type=bytes.fromhex("b106"),
        sku=b"744",
    )

    record = advert.manufacturer_data(bytes.fromhex("aa12deadbeef"))

    assert record is not None
    assert record.hex() == "01aa12deadbeef02b106373434"
    assert len(record) == 13


def test_an_advert_without_a_recorded_record_sends_none() -> None:
    advert = Advert(local_name=None)

    assert advert.manufacturer_data(bytes.fromhex("aa12deadbeef")) is None


def test_sku_length_follows_the_version_code() -> None:
    advert = Advert(
        local_name=None,
        version_code=0x01,
        bind_type=0x00,
        product_type=bytes.fromhex("b401"),
        sku=b"DKKE",
    )

    with pytest.raises(ConstructError):
        advert.manufacturer_data(bytes(6))


def test_ble_device_carries_the_device() -> None:
    device = A1783()

    ble_device = device.ble_device

    assert ble_device.details is device
    assert ble_device.address == device.address
    assert ble_device.name == "SOLIX C2000 Gen 2"


def test_recorded_replies_and_pushes_are_scripted() -> None:
    script = A1783().mcu

    assert [frame.cmd.msgtype for frame in script.respond(0x100)] == [0x900, 0x421]
    assert [frame.cmd.msgtype for frame in script.respond(0x057)] == [0x857]
    assert [frame.cmd.msgtype for frame in script.respond(0x103)] == [0x903, 0x421]
    assert script.respond(0x057)[0].payload.hex() == "00a10131"
    assert script.push(0x490).payload.endswith(b"charging_pps_series_c_0009\x00")
    assert all(
        frame.pattern.channel == 0x0F and not frame.cmd.encrypted
        for frame in script.respond(0x100)
    )


def test_recorded_identity_fields_are_the_synthetic_ones() -> None:
    script = A1783().mcu
    status = script.respond(0x100)[0].payload

    assert b"APCDKKE0000000001" in status
    assert b"APCDKJM0000000002" in status


def test_device_output_without_a_listener_goes_nowhere() -> None:
    device = A1783()

    device.press_button()
    device.push(0x421)

    assert not device.module.connected


def test_every_product_has_its_client_classes() -> None:
    assert set(PRODUCTS) == set(Product)
    assert PRODUCTS[Product.A1783].solixble_class == "C2000G2"
    assert PRODUCTS[Product.A1780].transport == Transport.LEGACY
