# anker-ble-emulator

Emulated Anker Solix BLE devices for testing BLE clients. A device plugs in under [bleak](https://github.com/hbldh/bleak) as a client backend, so your real client stack runs against it with no radio.

```python
from bleak import BleakClient

from anker_ble_emulator import A1783, EmulatedBleakBackend

device = A1783()  # SOLIX C2000 Gen 2, synthetic serial and MAC
async with BleakClient(device.ble_device, backend=EmulatedBleakBackend) as client:
    ...  # negotiate, authorize, send commands; notifications arrive as from the device

device.press_button()  # grant a pending owner confirmation
device.push(0x421)  # make the MCU push its recorded telemetry
```

For a client library that connects through bleak-retry-connector, `EmulatedConnection` patches its `establish_connection` so the library's own connect path lands on the emulator:

```python
from anker_ble_emulator.testing import EmulatedConnection

# Patches SolixBLE.device.establish_connection for the block.
async with EmulatedConnection(A1783()) as emulated:
    ...  # the library connects, negotiates and polls as against a real device
    emulated.check_assertions()  # if you set expect_ordered(...)
```

The emulated module runs the real negotiation (static-key GCM, P-256 ECDH, the session cipher), the module's authorization policy and its authorize timer; the MCU behind it answers with recorded, sanitized frames. See [docs/design.md](docs/design.md).
