# anker-solix-api maps (reference only)

Each `<pn>.json` is a hand-transcribed copy of that product's entry in anker-solix-api's `mqttmap.py`/`mqttcmdmap.py` (the source revision is in each file). These describe anker-solix-api's **MQTT** schema, not BLE.

They are reference material for hand-writing a device module in `src/anker_ble_emulator/devices/`, consulted alongside SolixBLE and real captured BLE evidence. They are not imported, not packaged, and not loaded at runtime by the emulator; nothing in `src/` reads this directory.

A field's `length` is the TLV's own length byte, copied from one recorded frame. A frame sizes each value to its magnitude (a power of 0-255 W can take 1 byte, 1200 W takes 2), so the same field's length differs between frames. The C Gen 2 `0421` and `0490` are variable-length frames whose fields have also changed between firmware generations, so a field can be absent from another generation's frame.

For the same reasons, a `0490` frame's total length varies within one schema and firmware build, not just across them. `_0005` reassembles from 2 `ff09` fragments, `_0009` from 3: fragment count follows the frame's actual length, not the schema name alone. A recorded length is one frame's length, not the only one that schema produces.
