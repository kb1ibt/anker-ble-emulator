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
| MCU | `mcu.py` | `McuScript`: recorded cleartext replies by message type, and pushes |
| products | `products.py` | `Product` part numbers with their SolixBLE class and anker-solix-api category; `Transport`, `Outer`, `Path` |
| devices | `devices/` | `base.py`: `EmulatedDevice`, `Profile`, `Advert`; one module per product (`a1783.py`) holding its profile and subclass; recorded frames in `devices/data/` |
| backend | `backend.py` | `EmulatedBleakBackend(BaseBleakClient)`: GATT services from bleak's own classes, writes into the module, notifications out |
| testing | `testing.py` | `EmulatedConnection`: patches a client library's `establish_connection` (SolixBLE's by default) to return a real connected `BleakClient` on the emulator; keeps SolixBLE `MockDevice`'s names (`expect_ordered` as an optional write assertion, `refuse_after`, `disconnect`, `send_data`, `new_connection_error`, `allow_connect`, `check_assertions`, `writes`) |

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
)
class A1783(EmulatedDevice): ...
```

| parameter | values | meaning |
|---|---|---|
| `pn` | `Product` (`A1783`, `A2345`, `A91B2`, ...) | the model; selects the profile (serial length, advert, capability, auth mode, recorded MCU data) |
| `serial` | string, or `None` | the provisioned serial reported in device info. `None` emulates a module with no serial: connect falls back to `ANKER_DEFAULT_SN_1` and device info omits the field |
| `mac` | `AA:BB:CC:DD:EE:FF` | the BLE MAC reported in device info and the advert |
| `transport` | `Transport.NEGOTIATED` (service `ff09`), `T2215`, `LEGACY` (service `1780`) | the GATT transport |
| `outer` | `Outer.ENCRYPTED`, `Outer.PLAIN`, keyword-only | the negotiation outer the module expects: `4xxx` under the static GCM key, or `0xxx` in clear. `None` on a transport that doesn't negotiate |
| `path` | `Path.ECDH`, `Path.LEGACY`, keyword-only | key establishment: P-256 ECDH, or the legacy account-key AES. `None` on a transport that doesn't negotiate |

Defaults are synthetic: serials have the product's length (17 bytes on the A1783, 16 on the A2345), and the MAC is a locally administered unicast address.

## Behaviour emulated

- Frames: `ff09 | len LE | pattern 03 <composer> <channel> | cmd | payload | xor`. Replies to module opcodes echo the request pattern; device-originated frames use composer `01`.
- cmd: `0x40` encrypted link flag, `0x80` fragment flag, msgtype = the low 12 bits, response = request `| 0x800`.
- Fragmenting: frames longer than the fragment cap (ATT MTU − 3, 253 by default) split into frames of the cap, each payload led by `index << 4 | total`.
- Encrypted outer: `4001 4003 4029 4005 4021` under the static AES-128-GCM key, nonce and AAD; `4021` answers the device's fresh P-256 point; the shared secret is the raw X coordinate; the session runs AES-128-GCM with key `ss[:16]`, nonce `ss[16:28]`, the static AAD and a 16-byte tag.
- Plain outer: `0001 0003 0029 0005 0021` in clear, then AES-128-CBC with key `ss[:16]`, IV `ss[16:32]`, PKCS7.
- Authorization: `0027` with an enrolled token authorizes at once; a new token gets `09` with the confirmation window and is granted by a simulated button press (`4827 00` on `030101`). Opcodes `>= 0x40` reach the MCU only once authorized.
- Module policy: per auth mode and module build, including refusing a cleartext connect and a non-ECDH method choice.
- The authorize timer: an unauthorized link drops 30 s after connect, or `auth_timeout + 5` s after a confirmation window opens, checked every 10 s.
- Relay: post-authorization requests go to the MCU script; its cleartext replies and pushes are encrypted for the session and sent on `03010f`.

## Recorded data

MCU replies and pushes are recorded cleartext from real units, packaged as `devices/data/<pn>.json` (msgtype → cleartext hex) by `tools/sanitize_frames.py`. The tool takes the real identifiers on its command line, never stores them, and fails on any printable run it wasn't told is safe. The frames are sanitized before they enter the package: the serial, MAC, expansion serials, account tokens and device clocks are replaced with synthetic values of the same length, and the frames are re-encoded. The fixed handshake frames (`4801`, `4803`) are reproduced byte for byte; frames that carry the serial or MAC match a real capture everywhere except those fields.

## Testing

The package's own tests do not depend on any client library:

- known-answer tests: the fixed handshake frames, recorded cleartext round trips, fragment layouts;
- a client-side fixture that drives real `bleak.BleakClient` through the backend and performs the client half of the handshake.

Client libraries (SolixBLE, through its `MockDevice` adapter) test against the emulator in their own suites.

## Roadmap

1. Scaffold, CI, TestPyPI publishing.
2. Engine: frame codec, fragmenting, ciphers, module state, MCU script, injected clock.
3. A1783 encrypted ECDH profile with sanitized recorded data.
4. bleak backend.
5. SolixBLE `MockDevice` compatibility.
6. Plain outer (CBC) and the module refusals.
7. Registration and the button.
8. Legacy AES path (negotiation only).
9. First TestPyPI release.
