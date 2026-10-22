# anker-solix-api maps (reference only)

Each `<pn>.json` is a hand-transcribed copy of that product's entry in anker-solix-api's `mqttmap.py`/`mqttcmdmap.py` (the source revision is in each file). These describe anker-solix-api's **MQTT** schema, not BLE.

They are reference material for hand-writing a device module in `src/anker_ble_emulator/devices/`, consulted alongside SolixBLE and real captured BLE evidence. They are not imported, not packaged, and not loaded at runtime by the emulator; nothing in `src/` reads this directory.

A field's `length` is the TLV's own length byte, copied from one recorded frame. The type byte fixes a value's width (`00` string, `01` u8, `02` u16, `03` 4 bytes, `04` a raw block, `06` a record of length-prefixed sub-fields; there is no `05`), so a value doesn't change size with its magnitude; a block's or record's length is set by the firmware build that writes it. The C Gen 2 `0421` fields have changed between firmware generations, so a field can be longer, shorter or absent in another generation's frame.

The `0490` is protobuf, not TLV: its varints are sized by magnitude, so its total length varies within one schema and firmware build, not just across them. `_0005` reassembles from 2 `ff09` fragments, `_0009` from 3: fragment count follows the frame's actual length, not the schema name alone. A recorded length is one frame's length, not the only one that schema produces.
