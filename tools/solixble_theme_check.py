# Copyright (c) 2026 Shawn Stricker
"""Drive SolixBLE's A2345 theme upload against the emulated A2345, padded and not.

A manual client check, run from a SolixBLE checkout that has the emulator
installed (SolixBLE #87, ``FileTransfer.pad_last_chunk``). Padded, the custom
photo shows and a repeat select sends no second ``4220``; unpadded, the photo
fails its first draw, the theme falls back to screen 1, and the repeat select
uploads again.
"""

import asyncio
import dataclasses
import sys
import time
from pathlib import Path
from unittest import mock

import SolixBLE.devices.prime_charger_250w as charger
from SolixBLE import PrimeCharger250w

from anker_ble_emulator import A2345
from anker_ble_emulator.testing import EmulatedConnection


IMAGE = (
    Path(__file__).parents[1] / "tests" / "fixtures" / "prime" / "themes"
) / "custom-test.jpg"
#: Any 20-byte client token: the emulator enrols it on the button press.
CLIENT_TOKEN = bytes(range(20)).hex()
PREPARE = bytes.fromhex("4220")
DRAW_WAIT = 12


def uploads(emulated: EmulatedConnection) -> int:
    """Count the ``4220`` prepares written so far (the clear header's cmd)."""
    return sum(1 for frame in emulated.writes if frame[7:9] == PREPARE)


async def run(pad: bool) -> None:  # noqa: FBT001  # the one switch under test
    """Upload the photo, wait for its draw, then select it again."""
    image = IMAGE.read_bytes()
    transfer = dataclasses.replace(charger.THEME_IMAGE_TRANSFER, pad_last_chunk=pad)
    emulated_device = A2345()
    with mock.patch.object(charger, "THEME_IMAGE_TRANSFER", transfer):
        async with EmulatedConnection(emulated_device) as emulated:
            device = PrimeCharger250w(
                emulated_device.ble_device, client_token=CLIENT_TOKEN
            )
            connect = asyncio.create_task(device.connect())
            await asyncio.sleep(1)
            emulated_device.press_button()
            out = sys.stdout.write
            out(f"pad={pad} connected={await connect}\n")

            start = time.monotonic()
            await device.set_display_custom_theme(image)
            elapsed = time.monotonic() - start
            out(f"  first:  {elapsed:5.2f} s, prepares sent {uploads(emulated)}\n")
            await asyncio.sleep(DRAW_WAIT)
            out(f"  after draw: display_theme={device.display_theme!r}\n")

            start = time.monotonic()
            await device.set_display_custom_theme(image)
            elapsed = time.monotonic() - start
            out(f"  repeat: {elapsed:5.2f} s, prepares sent {uploads(emulated)}\n")
            await device.disconnect()


async def main() -> None:
    """Run the padded upload, then the unpadded one."""
    for pad in (True, False):
        await run(pad)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
