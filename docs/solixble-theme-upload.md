---
orphan: true
---

# SolixBLE theme upload against the emulator

SolixBLE's A2345 custom-theme upload (SolixBLE #87, `FileTransfer.pad_last_chunk`), as it was run against the emulator while building #87: the record of how the upload works and how to replicate it, not a maintained tool. Run it with a SolixBLE checkout's Python and the emulator importable (installed, or `PYTHONPATH=<emulator>/src`), with `custom-test.jpg` (`tests/fixtures/prime/themes/`) beside it. Padded, the photo shows and a repeat select sends no second `4220`; unpadded, the first draw fails, the theme falls back to screen 1, and the repeat select uploads again (as on the bench, 2026-10-30). It takes about 30 s on the real clock.

```python
"""Drive SolixBLE's 250W theme upload (#87) against the emulated A2345."""

import asyncio
import dataclasses
import sys
import time
from pathlib import Path
from unittest import mock

from anker_ble_emulator import A2345
from anker_ble_emulator.testing import EmulatedConnection

import SolixBLE.devices.prime_charger_250w as charger
from SolixBLE import PrimeCharger250w

HERE = Path(__file__).parent
TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f9011223344"


def uploads(emulated: EmulatedConnection) -> int:
    """Count the 4220 prepares written so far (cmd bytes 4220 in the clear header)."""
    return sum(1 for frame in emulated.writes if frame[7:9] == bytes.fromhex("4220"))


async def run(pad: bool) -> None:
    image = (HERE / "custom-test.jpg").read_bytes()
    transfer = dataclasses.replace(charger.THEME_IMAGE_TRANSFER, pad_last_chunk=pad)
    emulated_device = A2345()
    with mock.patch.object(charger, "THEME_IMAGE_TRANSFER", transfer):
        async with EmulatedConnection(emulated_device) as emulated:
            device = PrimeCharger250w(emulated_device.ble_device, client_token=TOKEN)
            connect = asyncio.create_task(device.connect())
            await asyncio.sleep(1)
            emulated_device.press_button()
            print(f"pad={pad} connected={await connect}")

            start = time.monotonic()
            await device.set_display_custom_theme(image)
            print(f"  first:  {time.monotonic() - start:5.2f} s, prepares sent {uploads(emulated)}")
            await asyncio.sleep(12)
            print(f"  after draw: display_theme={device.display_theme!r}")

            start = time.monotonic()
            await device.set_display_custom_theme(image)
            print(f"  repeat: {time.monotonic() - start:5.2f} s, prepares sent {uploads(emulated)}")
            await device.disconnect()


async def main() -> None:
    for pad in (True, False):
        await run(pad)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```
