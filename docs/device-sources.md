# Device sources

Where recorded frames and protocol maps exist for each product, public sources only. Packaged data is sanitized by `tools/sanitize_frames.py` ([design.md](design.md) "Recorded data").

## Packaged

| product | packaged from | other sources |
|---|---|---|
| `A1761` SOLIX C1000 | `c402`: SolixBLE `tests/test_devices.py` (serial replaced); `0830` (module v0.2.3.1, device v1.5.9): [anker-solix-api `examples/Mqtt_SB1_C1000/`](https://github.com/thomluther/anker-solix-api/tree/main/examples/Mqtt_SB1_C1000) `4EJYH8ASEV3SGLA4` | handshake: SolixBLE `tests/const.py` `NEGOTIATION_RESPONSES_SOLIX`; `0405` ×119 (the MQTT twin of `c402`); maps: anker-solix-api `A1761`, SolixBLE `C1000`. Not found: the advertisement record, `c840`, setter replies |
| `A1763` SOLIX C1000 Gen 2 | [anker-solix-api `examples/Mqtt_C1000_Gen2/`](https://github.com/thomluther/anker-solix-api/tree/main/examples/Mqtt_C1000_Gen2): 183 frames, `0421` `0900` `0857` `0889` `0891` `0892` `0901`–`0903` | handshake log: [SolixBLE#22](https://github.com/flip-dots/SolixBLE/issues/22) (`solix.txt`); advert: [SolixBLE PR#70](https://github.com/flip-dots/SolixBLE/pull/70); decrypted `c421`/`c900`: SolixBLE `tests/test_devices.py`; maps: anker-solix-api `mqttmap.py` `A1763`, SolixBLE `C1000G2` |
| `A1765` SOLIX C1000X Gen 2 | the A1763's | decoded `0421`: [anker-solix-api#342](https://github.com/thomluther/anker-solix-api/issues/342) |
| `A1783` SOLIX C2000 Gen 2 | first-party captures | MQTT log: [anker-solix-api#329](https://github.com/thomluther/anker-solix-api/issues/329) (`a1783-c2000g2-mqtt-20260819.log`); `c421`/`c900` log: [HaSolixBLE#51](https://github.com/flip-dots/HaSolixBLE/issues/51); maps: anker-solix-api `A1783`, SolixBLE `C2000G2` |
| `A1785` SOLIX C2000X Gen 2 | [anker-solix-api#329](https://github.com/thomluther/anker-solix-api/issues/329) `A1785_mqtt_dump_*.txt` (7 files): `0421` `0425` `0900` `0903` `085e` `0890` `0892` | maps: anker-solix-api `A1785` |
| `A2687` Prime Charger 160W | [SolixBLE#9](https://github.com/flip-dots/SolixBLE/issues/9) `hooklogs.zip`: Frida hooks of the Anker Charging app with the cipher calls hooked, 126 frames with their cleartext: the handshake, `4200` → `4a00`, `4205`/`4206`/`4207`/`420a` acks, `4300` ×60 on `030111` | handshake: SolixBLE `tests/const.py`; client: SolixBLE `PrimeCharger160w`. Not found: the advertisement record, `0830` |
| `A2345` Prime Charger 250W | first-party captures | negotiation and `ca00`/`4a0a`/`4a00`: [SolixBLE#17](https://github.com/flip-dots/SolixBLE/issues/17) attachments; stream: [HaSolixBLE#24](https://github.com/flip-dots/HaSolixBLE/issues/24) gists; `0200`/`0a00`: [SolixBLE PR#57](https://github.com/flip-dots/SolixBLE/pull/57) |
| `A91B2` Prime Charging Station 240W | first-party captures | maps: anker-solix-api `A91B2` |

The C Gen 2 models share one display-board build, so their identity-free replies are shared ([design.md](design.md) "Emulated products").

## Not yet packaged

| product | recorded frames | maps |
|---|---|---|
| `A17C1` Solarbank 2 E1600 Pro | `8405` ×72: [SolixBLE#1](https://github.com/flip-dots/SolixBLE/issues/1); `c405`/`4409` with the session key: [SolixBLE#28](https://github.com/flip-dots/SolixBLE/issues/28); decoded: [anker-solix-api#216](https://github.com/thomluther/anker-solix-api/issues/216) | anker-solix-api `A17C1`, SolixBLE `Solarbank2` |
| `A1722` SOLIX C300 | command values: [anker-solix-api#348](https://github.com/thomluther/anker-solix-api/issues/348); telemetry: SolixBLE `tests/test_devices.py` | anker-solix-api `A1722`, SolixBLE `C300` |
| `A1728` SOLIX C300X DC | `0830`: [anker-solix-api#188](https://github.com/thomluther/anker-solix-api/issues/188); `0402` ×50: [HaSolixBLE#2](https://github.com/flip-dots/HaSolixBLE/issues/2) | anker-solix-api `A1728`, SolixBLE `C300DC` |
| `A1781` SOLIX F2600 | `c840`: SolixBLE `tests/test_devices.py` | anker-solix-api `A1781`, SolixBLE `F2600` |
| `A1790` / `A1790P` SOLIX F3800 (Plus) | negotiation: [SolixBLE#63](https://github.com/flip-dots/SolixBLE/issues/63); A1790P `0044`/`0045`: [anker-solix-api PR#243](https://github.com/thomluther/anker-solix-api/pull/243) | anker-solix-api `A1790`/`A1790P`, SolixBLE `F3800` |
| `A17C0` Solarbank E1600 | [anker-solix-api `examples/Mqtt_SB1_C1000/`](https://github.com/thomluther/anker-solix-api/tree/main/examples/Mqtt_SB1_C1000) `CUTUTT17MUSXAJ4R`: 6 msgtypes | anker-solix-api `A17C0` |
| `AS220` SOLIX S2000 | ~800 MQTT frames: [anker-solix-api#322](https://github.com/thomluther/anker-solix-api/issues/322) | anker-solix-api `AS220` |
| `A25X7` MagGo 3-in-1, `A110B` Prime Power Bank 20K | `4300`; `A110B` negotiation: SolixBLE `tests/test_devices.py` | SolixBLE |
| `A1753`–`A1755` C800, `A1780` F2000, `A17C5` Solarbank 3 | none | anker-solix-api, SolixBLE |

anker-solix-api example records carry an anonymized serial written in after capture: frames that hold it fail their checksum (`--fix-checksum`).
