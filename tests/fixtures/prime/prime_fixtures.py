# Copyright (c) 2026 Shawn Stricker
"""Prime rigs: an authorized app link on the module, its commands and pushes."""

from __future__ import annotations

import zlib
from datetime import UTC, datetime
from importlib.resources import files
from typing import TYPE_CHECKING

from construct import Int32ul

from anker_ble_emulator import Outer
from anker_ble_emulator.devices.a2345 import DeviceIdentities, DeviceIds
from anker_ble_emulator.devices.prime import MSGTYPE_303
from anker_ble_emulator.frame import decode
from anker_ble_emulator.layouts import ui
from anker_ble_emulator.parameters import ParameterDict, Parameters, decode_field
from tests.fixtures.client import (
    SESSION,
    TIMESTAMP,
    AppClient,
    cmd_hex,
    negotiate,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    from anker_ble_emulator import EmulatedDevice, ManualClock
    from anker_ble_emulator.layouts import Value
    from anker_ble_emulator.module import Output

#: The device's UTC epoch once the handshake's ``0022`` sets it, both MCUs adding
#: 1 s; its RTC (local seconds, ``UTC_OFFSET`` west) then reads Sunday 23:25:15.
PRIME_EPOCH = Int32ul.parse(TIMESTAMP) + 1
#: A request's route: BLE to the MCU.
ROUTE = (0xA1, b"\x21")


def prime_client(device: EmulatedDevice, token: bytes) -> AppClient:
    """Return an app client on the device's module, negotiated and authorized.

    It connects on the device's outer: the A2345 (encrypted) needs the button,
    the A91B2 (plain) authorizes at the key exchange.
    """
    module = device.negotiated_module
    client = AppClient(encrypted_outer=device.outer == Outer.ENCRYPTED)
    module.connect()
    negotiate(module, client, token)
    device.press_button()
    return client


def command(
    device: EmulatedDevice,
    client: AppClient,
    msgtype: int,
    fields: list[tuple[int, bytes]],
) -> list[tuple[str, str]]:
    """Send a session request routed to the MCU; return each frame as (cmd, hex)."""
    request = client.request(msgtype, [ROUTE, *fields], SESSION)
    return opened(client, device.negotiated_module.write(request))


def advance(
    device: EmulatedDevice, client: AppClient, clock: ManualClock, seconds: float
) -> list[tuple[str, str]]:
    """Advance the clock, run what fell due; return its pushes as (cmd, hex)."""
    clock.advance(seconds)
    return opened(client, device.negotiated_module.check_pushes())


def unit_change(
    device: EmulatedDevice, client: AppClient, **values: Value
) -> list[tuple[str, str]]:
    """Change settings on the unit (``set_values``); return its pushes as (cmd, hex)."""
    return pushed_by(device, client, lambda: device.set_values(**values))


def pushed_by(
    device: EmulatedDevice, client: AppClient, action: Callable[[], None]
) -> list[tuple[str, str]]:
    """Run a unit-side ``action``; return what the device pushed as (cmd, hex)."""
    outputs: list[Output] = []
    device.listen(outputs.append)
    action()
    device.listen(None)
    return [frame for out in outputs for frame in opened(client, out)]


def usbc_identity(
    device: EmulatedDevice, port: int, msgtype: int = 0xA00
) -> tuple[int, int, int, int]:
    """Return USB-C ``port``'s identity: ``bd``'s VID, PID, then ``be``'s class, code.

    From the ``4a00`` snapshot, or the ``0303`` stream's ``a8``/``a9``.
    """
    module = device.negotiated_module
    if msgtype == MSGTYPE_303:
        payload = module.mcu.push(MSGTYPE_303, values=module.values).payload
        params, tags = Parameters.parse(payload), ("a8", "a9")
    else:
        params, tags = snapshot(device), ("bd", "be")
    ids = decode_field(params, tags[0], DeviceIds)
    identities = decode_field(params, tags[1], DeviceIdentities)
    return (
        ids[f"pd{port}_device_vid"],
        ids[f"pd{port}_device_pid"],
        identities[f"pd{port}_device_class"],
        identities[f"pd{port}_device_code"],
    )


def opened(client: AppClient, out: Output) -> list[tuple[str, str]]:
    """Return each frame the module sent, opened, as (cmd hex, cleartext hex)."""
    replies = (client.open(data) for data in out.frames)
    return [(cmd_hex(r.frame), r.plaintext.hex()) for r in replies if r is not None]


def snapshot(device: EmulatedDevice) -> ParameterDict:
    """Return the ``4a00`` snapshot's fields as the device would answer now."""
    module = device.negotiated_module
    reply = module.mcu.respond(0x200, values=module.values, rtc=module.rtc)[0]
    return Parameters.parse(reply.payload)


def screensaver(device: EmulatedDevice) -> ParameterDict:
    """Return the ``4a02`` screensaver read's fields as the device would answer now."""
    module = device.negotiated_module
    reply = module.mcu.respond(0x202, values=module.values, rtc=module.rtc)[0]
    return Parameters.parse(reply.payload)


def arm(seconds: int) -> tuple[int, bytes]:
    """Return ``4209``'s ``a3`` field: the countdown armed for ``seconds``."""
    return 0xA3, b"\x04\x01" + ui(4).build(seconds)


#: The two photos made for the theme tests (ImageMagick, 480 x 200 baseline
#: 4:2:0): 8 321 B and 19 131 B, neither size leaving a 16-multiple last chunk
#: at 192 bytes a chunk.
THEMES = files("tests.fixtures.prime") / "themes"
CUSTOM_PHOTO = (THEMES / "custom-test.jpg").read_bytes()
CUSTOM_PHOTO_2 = (THEMES / "custom-test-2.jpg").read_bytes()


def _segment(marker: int, body: bytes) -> bytes:
    """Return a JPEG marker segment: ``ff <marker>``, its length, its body."""
    return bytes((0xFF, marker)) + (len(body) + 2).to_bytes(2, "big") + body


def baseline_jpeg(  # noqa: PLR0913  # one knob per header rule
    size: int = 500,
    *,
    sof: int = 0xC0,
    precision: int = 8,
    components: int = 3,
    luma: int = 0x22,
    quant: int = 0x00,
    huffman: int = 0x00,
    spectral: bytes = b"\x00\x3f\x00",
    before_scan: bytes = b"",
) -> bytes:
    """Return a minimal baseline JPEG, ``size`` bytes, with one header rule varied.

    SOI, APP0, DQT, SOF, DHT, ``before_scan``, SOS, scan bytes, EOI: 480 x 200,
    luma sampling ``luma``, the chroma components 1 x 1.
    """
    sampling = (luma, 0x11, 0x11)
    frame = bytes((precision,)) + (200).to_bytes(2, "big") + (480).to_bytes(2, "big")
    frame += bytes((components,))
    frame += b"".join(bytes((i + 1, sampling[i], min(i, 1))) for i in range(components))
    scan = bytes((components,))
    scan += b"".join(
        bytes((i + 1, 0x00 if i == 0 else 0x11)) for i in range(components)
    )
    header = (
        b"\xff\xd8"
        + _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
        + _segment(0xDB, bytes((quant,)) + bytes(range(1, 65)))
        + _segment(sof, frame)
        + _segment(0xC4, bytes((huffman, 1)) + bytes(15) + b"\x00")
        + before_scan
        + _segment(0xDA, scan + spectral)
    )
    return header + bytes(size - len(header) - 2) + b"\xff\xd9"


#: The URL field the app fills every ``4220`` with (doc ``4220`` row).
APP_URL = b"1234567890123456"


def upload_prepare(
    image: bytes, theme_id: int, *, window: int, chunk: int, kind: int = 0
) -> list[tuple[int, bytes]]:
    """Return ``4220``'s fields for ``image``: kind, id, CRC32, size, window, chunks."""
    count = -(-len(image) // chunk)
    return [
        (0xA2, bytes([1, kind])),
        (0xA3, b"\x03" + ui(4).build(theme_id)),
        (0xA4, b"\x03" + ui(4).build(zlib.crc32(image))),
        (0xA5, b"\x03" + ui(4).build(len(image))),
        (0xA6, bytes([1, window])),
        (0xA7, b"\x02" + ui(2).build(chunk)),
        (0xA8, b"\x02" + ui(2).build(count)),
        (0xA9, b"\x04" + APP_URL),
    ]


def theme_select(
    image: bytes, theme_id: int, *, theme: int = 0x83, url: bytes = APP_URL
) -> list[tuple[int, bytes]]:
    """Return ``4205``'s fields for a network (``0x83``) or custom (``0x85``) theme.

    The id, the image's CRC32, the default text color, then the URL in ``a6``.
    """
    return [
        (0xA2, bytes([1, theme])),
        (0xA3, b"\x03" + ui(4).build(theme_id)),
        (0xA4, b"\x03" + ui(4).build(zlib.crc32(image))),
        (0xA5, b"\x03\xff\xff\xff\xff"),
        (0xA6, b"\x04" + url),
    ]


def upload_chunk(
    image: bytes, index: int, chunk: int, *, pad: bool = False
) -> list[tuple[int, bytes]]:
    """Return ``4221``'s fields: the index, then one ignored byte and the chunk.

    With ``pad`` a short last chunk is filled to ``chunk`` bytes with zeros.
    """
    data = image[index * chunk : (index + 1) * chunk]
    if pad:
        data = data.ljust(chunk, b"\x00")
    return [(0xA2, b"\x02" + ui(2).build(index)), (0xA3, b"\x00" + data)]


def upload_theme(  # noqa: PLR0913  # the upload's own knobs
    device: EmulatedDevice,
    client: AppClient,
    image: bytes,
    theme_id: int,
    *,
    kind: int,
    chunk: int = 192,
    pad: bool = False,
) -> list[list[tuple[str, str]]]:
    """Upload ``image`` as the app does, ``4220`` then every ``4221``; the replies."""
    count = -(-len(image) // chunk)
    prepare = upload_prepare(image, theme_id, window=10, chunk=chunk, kind=kind)
    replies = [command(device, client, 0x220, prepare)]
    replies += [
        command(device, client, 0x221, upload_chunk(image, i, chunk, pad=pad))
        for i in range(count)
    ]
    return replies


def published(device: EmulatedDevice) -> list[tuple[int, str]]:
    """Return what the device published to MQTT as (msgtype, cleartext hex)."""
    return [
        (msgtype, decode(frame).payload.hex())
        for msgtype, frame in device.mqtt_published
    ]


#: The URL a holiday theme is fetched from in the tests.
HOLIDAY_URL = b"https://example.invalid/holiday.jpg"


def local_datetime(rtc: int) -> bytes:
    """Return ``rtc`` (local seconds) as ``4212``'s 7-byte datetime.

    ``[year - 2000, month, day, hour, min, sec, unused]`` (doc ``4212`` row).
    """
    when = datetime.fromtimestamp(rtc, tz=UTC)
    fields = (when.year - 2000, when.month, when.day, when.hour, when.minute)
    return bytes((*fields, when.second, 0))


def holiday_schedule(
    theme_id: int, start: int, end: int, *, crc: int = 7, a4: int = 8
) -> list[tuple[int, bytes]]:
    """Return ``4212``'s fields: id, CRC, ``a4``, the window on the local RTC, URL."""
    return [
        (0xA2, b"\x03" + ui(4).build(theme_id)),
        (0xA3, b"\x03" + ui(4).build(crc)),
        (0xA4, b"\x03" + ui(4).build(a4)),
        (0xA5, b"\x04" + local_datetime(start)),
        (0xA6, b"\x04" + local_datetime(end)),
        (0xA7, b"\x04" + HOLIDAY_URL),
    ]


def device_clock(seconds: int) -> str:
    """Return the ``fe`` a push carries ``seconds`` after the handshake, as hex."""
    return "fe0503" + ui(4).build(PRIME_EPOCH + seconds).hex()
