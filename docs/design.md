# anker-ble-emulator design

An emulated Anker Solix BLE device (the comms module and the device MCU behind it) for testing BLE clients. It plugs in under [bleak](https://github.com/hbldh/bleak) as a client backend, so the client's real stack (bleak, bleak-retry-connector, the client library) runs unchanged above it. There is no radio and no OS Bluetooth stack.

## Layers

| layer | module | role |
|---|---|---|
| frame | `frame.py` | `ff09` frame codec: length, pattern, cmd, payload, XOR checksum; fragmenting and reassembly at the fragment cap |
| fields | `tlv.py` | negotiation TLVs (`tag len value`, tags from `a1`) |
| messages | `messages.py` | one typed layout per negotiation reply (`0801`, `0803`, `0829`, `0821`, `0827 09`, status-only); request fields typed per msgtype (`0003` MTU u16, `0005` method, `0022` time u32 / offset int32 / TZ, `0027` token) |
| crypto | `crypto.py` | static GCM, session GCM, session CBC, P-256 device key pair |
| module | `module.py` | the comms module: negotiation handlers, link state, authorization policy, the authorize timer, the relay to and from the MCU |
| MCU | `mcu.py` | `McuScript`: recorded cleartext replies by message type, and pushes; acks for mapped commands with no recording |
| layouts | `layouts.py` | `Layout`: a product's telemetry fields (tag, type, length, named parts) and commands (accepted values), from anker-solix-api's maps; builds telemetry, sets values by name, checks commands; packaged in `maps/` |
| products | `products.py` | `Product` part numbers with their SolixBLE class and anker-solix-api category; `Transport`, `Outer`, `Path` |
| devices | `devices/` | `base.py`: `EmulatedDevice`, `Profile`, `Advert`; one module per product holding its profile and subclass; `solix_c_gen2.py`: the C Gen 2 line's shared command set; recorded frames in `devices/data/` |
| backend | `backend.py` | `EmulatedBleakBackend(BaseBleakClient)`: GATT services from bleak's own classes (service `8c850001`, write `8c850002`, notify `8c850003`; `ff09` is only advertised), writes into the module, notifications out |
| testing | `testing.py` | `EmulatedConnection`: patches a client library's `establish_connection` (SolixBLE's by default) to return a real connected `BleakClient` on the emulator; keeps SolixBLE `MockDevice`'s names (`expect_ordered` as an optional write assertion, `refuse_after`, `disconnect`, `send_data`, `new_connection_error`, `allow_connect`, `check_assertions`, `writes`) |

## Emulated products

| product | advert (name, productType, sku) | outer | auth mode | default module build | device fw | session | recorded MCU replies |
|---|---|---|---|---|---|---|---|
| `A1722` SOLIX C300 | not recorded | plain | 2 (button) | v0.2.9.8 | v1.0.5.7 | CBC | `4040` → `4840` (the `c402` telemetry behind a `00` status); push `c402`; mapped setters get the map's ack |
| `A1761` SOLIX C1000 | not recorded | plain | 2 (button) | v0.2.3.1 | v1.5.9 | CBC | `4040` → `4840` (the `c402` telemetry behind a `00` status); push `c402`; mapped setters get the map's ack |
| `A1763` SOLIX C1000 Gen 2 | `SOLIX C1000 Gen 2`, `b118`, `DK96` | encrypted | 2 (button) | v0.3.3.0 | v1.2.1.6 | GCM | the C Gen 2 set; pushes `c421`, `4489`, `c490` |
| `A1765` SOLIX C1000X Gen 2 | `SOLIX C1000X Gen 2`, `b119`, `DK96` | encrypted | 2 (button) | v0.3.3.0 | v1.2.1.6 | GCM | the A1763's |
| `A1783` SOLIX C2000 Gen 2 | `SOLIX C2000 Gen 2`, `b11a`, `DKKE` | encrypted | 2 (button) | v0.3.3.0 | v1.2.1.6 | GCM | the C Gen 2 set; pushes `c421`, `4489`, `c490`, `4425` |
| `A1785` SOLIX C2000X Gen 2 | `SOLIX C2000X Gen 2`, `b11b`, `DKVP` | encrypted | 2 (button) | v0.3.3.0 | v1.2.1.6 | GCM | the C Gen 2 set; pushes `c421`, `4489`, `c490`, `4425` |
| `AS220` SOLIX S2000 | not recorded | encrypted | 2 (button) | `ESP32 0.0.0.3` (`0829`; no `0830`) | not recorded | GCM | `4100` subscribe → `4900` + `4421` (C Gen 2 telemetry layout); `4057`/`405e`/`4090`/`4093`/`4101`/`4103` → `00 a1 01 31` |
| `A17C1` Solarbank 2 E1600 Pro | not recorded | plain | 2 (button) | `ESP32 0.0.0.3` (`0829`; no `0830`) | not recorded | CBC | push `c405` (511 B, three fragments) and `4409`; no recorded request |
| `A17C0` Solarbank E1600 | not recorded | plain (assumed, the A17C1's) | 2 (button) | unrecorded (`0829` as the A17C1's) | not recorded | CBC | none recorded: `4040` → `4405` and push `4405`, built from the map's recorded-typed fields; mapped setters get the map's ack |
| `A17C3` Solarbank 2 E1600 Plus | not recorded | plain (assumed, the A17C1's) | 2 (button) | unrecorded (`0829` as the A17C1's) | not recorded | CBC | none recorded: push `4405` with the map's fields typed as the A17C1's recorded `c405`; mapped setters get the map's ack |
| `A2687` Prime Charger 160W | name `Charging`, rest not recorded | encrypted | 2 (button) | `Charging` v0.0.5.0 (`0829`; no `0830`) | not recorded | GCM | `4200` → `4a00`; `4205`/`4206`/`4207`/`420a` → `4a05`/`4a06`/`4a07`/`4a0a`; push `4300`; all on `030111`; `0829 a5` = MAC + the serial's last 11 characters; MTU cap 297 |
| `A110B` Prime Power Bank 20K | not recorded | encrypted | 2 (button) | `Charging` v0.0.5.1 (`0829`; no `0830`) | not recorded | GCM | `4200` → `4a00`; push `4300`; both on `030111`; `420a` unanswered (none recorded); `0829 a5` = MAC + the serial's last 11 characters; MTU cap 297 |
| `A25X7` MagGo 3-in-1 Wireless Charger | not recorded | encrypted | not recorded (2 assumed) | not recorded (`unrecorded`; default `0829`) | not recorded | GCM | push `4300` (three pad blocks `a2`-`a4`); `4200` subscribe unanswered (none recorded); MCU channel and MTU cap not recorded (defaults) |
| `A2345` Prime Charger 250W | `A2345_<MAC tail>`, `b402`, `QJB` | encrypted | 2 (button) | v0.2.9.7 | v2.1.1.6 | GCM | `4200` → `ca00` (fragmented); `420a` → `4a0a`; `420b` → `4a0b` + `4303`; push `4303` |
| `A91B2` Prime Charging Station 240W | none, `b401`, `JTB` | plain | 0 | v0.2.9.7 | v1.1.2.4 | CBC | `4200` → `4a00` (250 B, whole); `420a` → `4a0a`; `420b` → `4a0b` + `4303`; push `4303` |

The C Gen 2 models run one display-board build, which picks the model from its factory record, so the MCU answers the same commands on all four: `4100` → `c900` + `c421`; `4057` → `4857`; `405e` → `485e` + `c421`; `4063` → `4863`; `4089` → `4889`; `4090`/`4091`/`4092` → `4890`/`4891`/`4892` + `c421`; `4101`–`4104` → `4901`–`4904` + `c421`. Frames that name the unit (`0421`, `0900`, `0490`, `0425`) come from each model's own recording, and the X models report their base model's PN in them (the A1785's say `A1783`). The identity-free replies are shared (`solix_c_gen2.json`).

Module session ops below `0x40` are the ESP32's own, answered on `03 00 0f` once the link is authorized. `0030` → `0830` is built on every build from the module build, the device firmware and the product's names (C Gen 2: `<pn>_low`, `<pn>_mcu_low`, `<pn>_esp32_low`; Prime: `<pn>`, `<pn>_mcu`, `<pn>_esp32`). The A1783's recorded `0020`, `0028`, `002e`, `002f`, `0036` and `0038` replies come only with v0.3.3.0, the build they were recorded on.

Every wire parse and build is a [construct](https://construct.readthedocs.io) layout: the frame (`PATTERN_LAYOUT`, `COMMAND_LAYOUT` with the link flags and 12-bit msgtype, `FRAME_LAYOUT`, `FRAGMENT_LAYOUT`), the fields and messages, the key material (`GCM_KEYS_LAYOUT`, `CBC_KEYS_LAYOUT` over the shared secret), the P-256 point (`POINT_LAYOUT`) and the advert record (`ADVERT_LAYOUT`). Frames and parsed messages are construct `Container`s; plain classes hold only settings and link state, which have no wire form.

The module and MCU layers are synchronous and transport-free: a write in, a list of frames out, each with a delay. Time is injected, so timers are testable without sleeping.

## The device

```python
EmulatedDevice(
    pn: Product,
    serial: str | None = <product default>,
    mac: str = "AA:12:DE:AD:BE:EF",
    transport: Transport = <product default>,
    *,
    outer: Outer | None = <product default>,
    path: Path | None = <product default>,
    module: ModuleBuild | None = <product default>,
)
class A1783(EmulatedDevice): ...
```

| parameter | values | meaning |
|---|---|---|
| `pn` | `Product` (`A1783`, `A2345`, `A91B2`, ...) | the model; selects the profile (serial length, advert, capability, auth mode, recorded MCU data) |
| `serial` | string, or `None` | the provisioned serial reported in device info. `None` emulates a module with no serial: connect falls back to `ANKER_DEFAULT_SN_1` and device info omits the field |
| `mac` | `AA:BB:CC:DD:EE:FF` | the BLE MAC reported in device info and the advert |
| `transport` | `Transport.NEGOTIATED` (advertises `ff09`, GATT service `8c850001`), `T2215`, `LEGACY` (service `1780`) | the GATT transport |
| `outer` | `Outer.ENCRYPTED`, `Outer.PLAIN`, keyword-only, exclusive with `module` | the negotiation outer the module expects: `4xxx` under the static GCM key, or `0xxx` in clear. `PLAIN` makes the module accept a cleartext connect whatever its build. `None` on a transport that doesn't negotiate |
| `path` | `Path.ECDH`, `Path.LEGACY`, keyword-only | key establishment: P-256 ECDH, or the legacy account-key AES. `None` on a transport that doesn't negotiate |
| `module` | `ModuleBuild.V0_2_9_7`, `V0_3_0_6`, `V0_3_3_0`, keyword-only, exclusive with `outer` | the comms module's firmware: what `0830` reports, which recorded module replies load, and whether the module enforces the auth mode (v0.3.3.0 only) |

Defaults are synthetic: serials have the product's length (17 bytes on the A1783, 16 on the A2345), and the MAC is a locally administered unicast address.

## Behaviour emulated

- Frames: `ff09 | len LE | pattern 03 <composer> <channel> | cmd | payload | xor`. Replies to module opcodes echo the request pattern; device-originated frames use composer `01`.
- cmd: `0x40` encrypted link flag, `0x80` fragment flag, msgtype = the low 12 bits, response = request `| 0x800`.
- Fragmenting: frames longer than the fragment cap (ATT MTU − 3, 253 by default) split into frames of the cap, each payload led by `index << 4 | total`.
- Encrypted outer: `4001 4003 4029 4005 4021` under the static AES-128-GCM key, nonce and AAD; `4021` answers the device's fresh P-256 point; the shared secret is the raw X coordinate; the session runs AES-128-GCM with key `ss[:16]`, nonce `ss[16:28]`, the static AAD and a 16-byte tag.
- Plain outer: `0001 0003 0029 0005 0021` in clear, then AES-128-CBC with key `ss[:16]`, IV `ss[16:32]`, PKCS7.
- Authorization: `0027` with an enrolled token authorizes at once; a new token gets `09` with the confirmation window and is granted by a simulated button press (`4827 00` on `030101`). Opcodes `>= 0x40` reach the MCU only once authorized.
- Module policy: v0.3.3.0 enforces the auth mode on the encrypted outer: it drops a cleartext `0001`, refuses a non-ECDH `0005` method, and answers `0027` only after an encrypted ECDH. Earlier builds (v0.2.9.7, v0.3.0.6) accept either connect frame.
- The authorize timer: an unauthorized link drops 30 s after connect, or `auth_timeout + 5` s after a confirmation window opens, checked every 10 s.
- Routing: a session request's `a1` is its route, destination in the high nibble and source in the low (`21`: BLE to the MCU). The module answers its own ops (below `0x40`) on the arrival port whatever the `a1`. For the MCU it relays only a request routed from BLE to the MCU on fid `0x0f`; it drops one with no route, an unknown source or an unhandled destination, and answers a logging-channel (source `a`) request itself with status `00`. A request on fid `0x11` (`030011`) draws no reply. Every MCU frame on the BLE link is routed `a1 31`.
- Relay: post-authorization requests go to the MCU script; its cleartext replies and pushes are encrypted for the session and sent on `03010f`. A mapped command with no recorded reply gets `<status> a1 01 31`: `00` when its values are ones the map accepts, `04` when not.
- Cloud: `set_cloud(True)` puts the device on WiFi to the cloud, where it has no BLE link (the MCU then routes its frames `a1 34` to MQTT): the open link drops and connecting fails until `set_cloud(False)`.
- Values: `set_values(name=value)` sets a telemetry field on the device: every later frame that carries the name, recorded or built, sends it. TLV names are anker-solix-api's; the C Gen 2 `c490` summary's are listed in `devices/data/c490_fields.json` (path → name), and the five it shares with `0421`/`0900` (`battery_soc`, `output_power_total`, `ac_output_power`, `dc_input_power_total`, `remaining_time_hours`) are the same reading in both.
- Summary: the `c490` payload is `a1 01 31`, `a2` (2-byte length, type `04`, a nanopb message), then the schema name in `a3`. `summary.py` walks the message and rewrites named leaves (varint, zigzag varint, fixed bytes at their recorded length), so an unset summary is the recording byte for byte. `.14` and `.15` always hold two pack entries; on a model with no expansion (`Profile.expansion`) the second entries and the expansion's SoC (`.23.1#1`) start at zero.
- Commands shown in telemetry: where the map links a command's setting to a telemetry field (`state_name`, through a converter's table where it has one), an accepted command sets that field in every message that carries it, before the reply and the telemetry that follows are built. A rejected command sets nothing. Its reply's status is `04` on an MCU that rejects (the Prime MCUs), whether or not the reply is recorded; the C Gen 2 display board acks every setter `00` (`fid0f_ack_reply`), so there a rejected setting is acked and left unapplied, as the A1783 does with a setting it can't read (`a2 01 01`, no type byte).
- MCU: `device.mcu` is the script; `use_mcu(script)` replaces it (`McuScript()` for a quiet MCU), `set_reply(request, (msgtype, cleartext), ...)` sets a request's answer (none for silence), and `set_push(msgtype, cleartext)` sets a push.

## Recorded data

MCU replies and pushes are recorded cleartext from real units, packaged as `devices/data/<pn>.json` (msgtype → cleartext hex) by `tools/sanitize_frames.py`; a profile lists its files, and the first that holds a msgtype supplies it. The tool reads collector logs and MQTT records (anker-solix-api `.ndjson` examples, `mqtt_monitor` dumps). An MQTT frame is the same frame the MCU sends to BLE with another routing marker in its leading `a1` (`34` or `32` for `31`), which the tool sets back to `31`; `--fix-checksum` recomputes the checksum of records whose serial was anonymized after capture. Sources are listed in [device-sources.md](device-sources.md). The tool takes the real identifiers on its command line, never stores them, and fails on any printable run it wasn't told is safe. The frames are sanitized before they enter the package: the serial, MAC, expansion serials, account tokens and device clocks are replaced with synthetic values of the same length, and the frames are re-encoded. The fixed handshake frames (`4801`, `4803`) are reproduced byte for byte; frames that carry the serial or MAC match a real capture everywhere except those fields.

## Layouts

`maps/<pn>.json` is built by `tools/import_maps.py` from anker-solix-api's `mqttmap.py` (the source revision is in each file). The maps name each field, but most leave its type to the frame, so the tool walks recorded frames (collector logs, MQTT records, frame lists), counts each field's type byte and length, and writes the type a field has in recordings (`from: recorded`), in the map itself (`map`), or under the same name in other recorded maps where every recording agrees (`name`). A field with none of these stays untyped and is left out of built frames. A frame with no msgtype (a decoded payload from a client's tests) is matched to the map that holds nearly all its tags, or, for a known product, whose tags it nearly all holds; this is how BLE `c402` (gen-1 power stations) types the MQTT `0405` map, whose fields it shares tag for tag. Layouts hold only structure: tags, names, types, lengths and accepted command values. A map's sub-field (`bytes`) keys are decimal byte offsets into the value after its type byte.

## Testing

The package's own tests do not depend on any client library:

- known-answer tests: the fixed handshake frames, recorded cleartext round trips, fragment layouts;
- a client-side fixture that drives real `bleak.BleakClient` through the backend and performs the client half of the handshake.

Client libraries (SolixBLE, through its `MockDevice` adapter) test against the emulator in their own suites.

## Releases

The version is the git tag (hatch-vcs).

| trigger | TestPyPI | PyPI | GitHub |
|---|---|---|---|
| tag `vX.Y.Z` | yes | yes | release; then every pre-release and its tag is deleted |
| tag `vX.Y.Z-(alpha\|beta\|rc\|dev)N` | yes | no | pre-release |
| push to `main` (CI green) | no | no | the rolling `dev` pre-release is replaced |

Any other tag is ignored, and the build fails unless the built version equals the tag's (`v0.2.0-rc1` → `0.2.0rc1`).

## Roadmap

1. Scaffold, CI, TestPyPI publishing.
2. Engine: frame codec, fragmenting, ciphers, module state, MCU script, injected clock.
3. A1783 encrypted ECDH profile with sanitized recorded data; then the A2345 and A91B2.
4. bleak backend.
5. SolixBLE `MockDevice` compatibility.
6. Plain outer (CBC) and the module refusals.
7. Registration and the button.
8. Legacy AES path (negotiation only).
9. First TestPyPI release.
