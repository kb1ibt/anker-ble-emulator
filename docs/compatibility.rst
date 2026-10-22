Compatibility
=============

What each emulated product replicates. ✅ emulated, ❌ not yet, N/A the
product doesn't do it.

Emulated products
-----------------

=============================== ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= =========
Feature                         C300      C300 DC   C1000     C800      C1000 G2  C1000X G2 C2000 G2  C2000X G2 S2000     Alt Chgr  SB2 Pro   160W      20K       250W      3-in-1    240W      SB1       F2600
                                (A1722)   (A1726)   (A1761)   (A1753)   (A1763)   (A1765)   (A1783)   (A1785)   (AS220)   (AS200)   (A17C1)   (A2687)   (A110B)   (A2345)   (A25X7)   (A91B2)   (A17C0)   (A1781)
=============================== ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= =========
Advertisement                   ❌        ❌        ❌        ❌        ✅        ✅        ✅        ✅        ❌        ❌        ❌        ❌        ❌        ✅        ❌        ✅        ❌        ❌
Encrypted outer (GCM session)   ❌        ❌        ❌        ❌        ✅        ✅        ✅        ✅        ✅        ✅        ❌        ✅        ✅        ✅        ✅        N/A       ❌        ❌
Plain outer (CBC session)       ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ❌        ❌        ✅        ❌        ❌        ✅        ❌        ✅        ✅        ✅
ECDH key exchange               ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Legacy AES key exchange         ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌
Owner confirmation by button    ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        N/A       ✅        ✅
Module builds (``module=``)     ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Version read (``0030``)         ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ❌        ❌        ❌        ❌        ❌        ✅        ❌        ✅        ❌        ❌
Recorded module ops (``0020``…) ❌        ❌        ❌        ❌        ❌        ❌        ✅        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌
Request routing by ``a1``       ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Fragmented frames               ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Recorded status reply           ❌        ❌        ❌        ❌        ✅        ✅        ✅        ✅        ✅        ✅        ❌        ✅        ✅        ✅        ❌        ✅        ✅        ✅
Recorded telemetry pushes       ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ❌
``0490`` push                   N/A       N/A       N/A       N/A       ✅        ✅        ✅        ✅        ❌        ❌        N/A       N/A       N/A       N/A       N/A       N/A       N/A       N/A
``0490`` fields by name         N/A       N/A       N/A       N/A       ✅        ✅        ✅        ✅        ❌        ❌        N/A       N/A       N/A       N/A       N/A       N/A       N/A       N/A
Expansion battery attached      N/A       N/A       ❌        N/A       ❌        ❌        ✅        ❌        ❌        N/A       ❌        N/A       N/A       N/A       N/A       N/A       N/A       ❌
Custom replies and pushes       ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Cloud mode (no BLE link)        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
=============================== ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= =========

- **Recorded** replies and pushes are sanitized frames from real units
  (:doc:`device-sources`). The C Gen 2 models share one display-board build,
  so they answer the same commands; frames that name the unit come from each
  model's own recording.
- **``0490`` fields by name**: the ``0490`` push's fields (schema
  ``charging_pps_series_c_0009``) are set by name like the TLV telemetry's.
  It was recorded on an A1783 with an expansion attached; the models without
  one send the expansion's pack entries and SoC as zeros, as the display board
  does.
- **Custom replies and pushes**: ``device.set_reply``, ``device.set_push`` and
  ``device.use_mcu`` replace what the MCU sends, for frames no recording has.
- The A91B2 runs auth mode 0: it negotiates in the clear and authorizes at the
  key exchange, so it has no confirmation step.
- **Not recorded** for the C300, the C1000, the S2000, the Solarbank 2, the
  160W and the 20K: no advertisement record is recorded for any of them, so they
  advertise only the ``ff09`` service and no manufacturer data. The C300, C1000
  and Solarbank 2 are recorded on the plain outer and the S2000, 160W and 20K on
  the encrypted one; their module builds don't enforce either, so the emulator
  takes both. The S2000's ``4100`` subscribe draws its recorded ``c900`` and a
  ``c421`` that is the same telemetry without the status byte, as on the
  C Gen 2; its setter acks were recorded over MQTT. Whether it posts a
  ``c490`` isn't recorded, and its telemetry is sent whole, since its fragment
  size isn't recorded. The C300 and C1000 answer
  ``4040`` with their recorded ``c402`` telemetry behind a ``00`` status byte,
  as replies carry one; their ``c840`` itself isn't recorded. They have no
  layout, so a setter with no recording goes unanswered; the C1000's ``404a``
  ack is recorded. The S2000, the Solarbank 2, the
  160W and the 20K have no recorded ``0830``, so ``0030`` goes unanswered. The
  160W's and 20K's MCU frames travel on channel ``11`` (``030111``); the 20K's
  ``420a`` has no recorded answer, so it goes unanswered. The Solarbank 2 pushes its
  ``c405`` telemetry in three fragments and a ``0409`` status; it answers no
  recorded request, and its legacy (no account) handshake isn't emulated.
- **3-in-1 (A25X7)**: only its ``4300`` telemetry is recorded (three pad
  blocks, ``a2``-``a4``); the encrypted outer and the ``4200`` subscribe are
  SolixBLE's ``MagGo3in1``. Its handshake isn't recorded, so its module build,
  ``0829``, MTU cap, MCU channel and button confirmation (auth mode 2) are the
  emulator's defaults, not the device's. The ``4200`` subscribe has no recorded
  answer, so it goes unanswered; the telemetry is pushed with ``device.push``.
- **Solarbank E1600 (A17C0)**: no BLE frame or handshake is recorded. Its
  ``0405`` telemetry and ``0856`` / ``0857`` replies are an MQTT recording with
  the BLE route marker; that the BLE ``c405`` equals it is assumed. It answers
  ``4040`` with that ``0405``. Its plain outer is the Solarbank 2's (A17C1),
  assumed. The ``0050`` / ``0067`` / ``0068`` setters check their documented
  options and set the telemetry fields they name.
- **Alternator Charger (AS200)**: no BLE frame or handshake is recorded. Its
  ``0900``, ``0421``, ``0857``, ``0903`` and ``0489`` are MQTT recordings
  with the BLE route marker, and it runs the C Gen 2 display board's message
  set: ``4100`` draws ``4900`` + ``4421``, ``4057`` an ack, ``4103`` an ack
  and a ``4421`` that shows the settings it wrote. Its encrypted outer and
  button confirmation are the S2000's, assumed. Its ``0421`` is one moment
  of charging a C2000 Gen 2 through the expansion cable; the attached power
  station's fields (``a7``) are set by name like the rest. Whether it posts a
  ``c490`` isn't recorded.
- **C300 DC (A1726)**: the ``c402`` telemetry was recorded in the clear on
  device firmware v0.1.2.8; later firmware (v0.1.3.7) sends it fragmented over
  an ECDH session, which the emulator negotiates.
  The setter acks (``0043``, ``004b``, ``004f``, ``0052``, ``0057``) were
  recorded over MQTT; no status request is recorded, so ``4040`` goes
  unanswered.
- **C800 (A1753)**: only its ``0830`` is its own. Its telemetry and ``4040``
  reply are the C1000's (A1761), borrowed: the C800's recorded MQTT ``0405``
  carries fields the C1000's BLE ``c402`` doesn't, so it isn't the BLE frame,
  and a C800X runs SolixBLE's C1000 class over BLE. No setter ack is recorded,
  so setters go unanswered.
- The F2600 (A1781) answers ``4040`` with its recorded ``c840``. The recording
  starts at ``a1``, so the ``00`` status byte comes from the reply rule. No push
  or setter ack is recorded, so it pushes nothing and leaves setters unanswered.

Legacy transport products
-------------------------

``Transport.LEGACY`` speaks an unencrypted, fixed-offset protocol with no
negotiation: GATT service ``014bf5da`` (advertised as ``00001780``), write
characteristic ``7777``, notify characteristic ``8888``. See
:mod:`anker_ble_emulator.legacy`.

=============================== =========
Feature                         F2000
                                 (A1780)
=============================== =========
Telemetry poll (``0101``)       ✅
AC output control               ✅
DC/car-socket output control    ✅
Power saving mode control       ✅
Light bar mode control          ✅
StateAck (physical button)      ✅
Display mode / timeout control  ❌
AC charging power control       ❌
Timer control                   ❌
=============================== =========

- The baseline telemetry frames are a real captured unit's (serial
  anonymized), from SolixBLE PR #64. Field offsets and command bytes are
  flip-dots/SolixBLEF2000's ``f2000_alt.py``, cross-checked against both an
  HCI snoop of the official Anker app and that project's own live-hardware
  tests.
- A control command draws no reply; its effect shows on the next poll.
- ``press_button()`` emits a StateAck of the module's current AC/DC/power
  saving/light state. Which physical button press maps to which state
  change isn't confirmed on real hardware, so nothing is assumed about it.
- Display mode, display timeout, AC charging power, and timers have no
  confirmed command bytes yet, so they aren't emulated.
