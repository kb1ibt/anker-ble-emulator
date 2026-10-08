Compatibility
=============

What each emulated product replicates. ✅ emulated, ❌ not yet, N/A the
product doesn't do it.

Emulated products
-----------------

=============================== ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= =========
Feature                         C300      C1000     C1000 G2  C1000X G2 C2000 G2  C2000X G2 S2000     SB2 Pro   160W      250W      240W
                                (A1722)   (A1761)   (A1763)   (A1765)   (A1783)   (A1785)   (AS220)   (A17C1)   (A2687)   (A2345)   (A91B2)
=============================== ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= =========
Advertisement                   ❌        ❌        ✅        ✅        ✅        ✅        ❌        ❌        ❌        ✅        ✅
Encrypted outer (GCM session)   ❌        ❌        ✅        ✅        ✅        ✅        ✅        ❌        ✅        ✅        N/A
Plain outer (CBC session)       ✅        ✅        ✅        ✅        ✅        ✅        ❌        ✅        ❌        ✅        ✅
ECDH key exchange               ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Legacy AES key exchange         ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌        ❌
Owner confirmation by button    ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        N/A
Module builds (``module=``)     ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Version read (``0030``)         ✅        ✅        ✅        ✅        ✅        ✅        ❌        ❌        ❌        ✅        ✅
Recorded module ops (``0020``…) ❌        ❌        ❌        ❌        ✅        ❌        ❌        ❌        ❌        ❌        ❌
Request routing by ``a1``       ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Fragmented frames               ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Recorded status reply           ❌        ❌        ✅        ✅        ✅        ✅        ✅        ❌        ✅        ✅        ✅
Recorded telemetry pushes       ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Summary push (``c490``)         N/A       N/A       ✅        ✅        ✅        ✅        ❌        N/A       N/A       N/A       N/A
Summary fields by name          N/A       N/A       ✅        ✅        ✅        ✅        ❌        N/A       N/A       N/A       N/A
Expansion battery attached      N/A       ❌        ❌        ❌        ✅        ❌        ❌        ❌        N/A       N/A       N/A
Custom replies and pushes       ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
Cloud mode (no BLE link)        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅        ✅
=============================== ========= ========= ========= ========= ========= ========= ========= ========= ========= ========= =========

- **Recorded** replies and pushes are sanitized frames from real units
  (:doc:`device-sources`). The C Gen 2 models share one display-board build,
  so they answer the same commands; frames that name the unit come from each
  model's own recording.
- **Summary fields by name**: the ``c490`` summary's fields (schema
  ``charging_pps_series_c_0009``) are set by name like the TLV telemetry's.
  It was recorded on an A1783 with an expansion attached; the models without
  one send the expansion's pack entries and SoC as zeros, as the display board
  does.
- **Custom replies and pushes**: ``device.set_reply``, ``device.set_push`` and
  ``device.use_mcu`` replace what the MCU sends, for frames no recording has.
- The A91B2 runs auth mode 0: it negotiates in the clear and authorizes at the
  key exchange, so it has no confirmation step.
- **Not recorded** for the C300, the C1000, the S2000, the Solarbank 2 and the
  160W: no advertisement record is recorded for any of them, so they advertise
  only the ``ff09`` service and no manufacturer data. The C300, C1000 and
  Solarbank 2 are recorded on the plain outer and the S2000 and 160W on the
  encrypted one; their module builds don't enforce either, so the emulator
  takes both. The S2000's ``4100`` subscribe draws its recorded ``c900`` and a
  ``c421`` that is the same telemetry without the status byte, as on the
  C Gen 2; its setter acks were recorded over MQTT. Whether it posts a
  ``c490`` isn't recorded, and its telemetry is sent whole, since its fragment
  size isn't recorded. The C300 and C1000 answer
  ``4040`` with their recorded ``c402`` telemetry behind a ``00`` status byte,
  as replies carry one; their ``c840`` itself isn't recorded. Their setters get
  the map's checked ack (the C1000's ``404a`` ack is recorded); whether they
  answer a refused value ``04`` isn't recorded. The S2000, the Solarbank 2 and
  the 160W have no recorded ``0830``, so ``0030`` goes unanswered. The 160W's MCU
  frames travel on channel ``11`` (``030111``). The Solarbank 2 pushes its
  ``c405`` telemetry in three fragments and a ``0409`` status; it answers no
  recorded request, and its legacy (no account) handshake isn't emulated.

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

Map-built products
------------------

Products with a SolixBLE device class and an anker-solix-api map but no
recording get a profile generated from both (``tools/generate_profiles.py``,
into ``devices/generated/``): the class's outer, status request and commands,
and telemetry built from the map's typed fields under each message the class
listens for. Fields the map names but doesn't type are typed from where the
SolixBLE class decodes them. Nothing about these products is recorded: no
advertisement record, no ``0830``, no frame. :doc:`solixble-crosscheck` checks
every mapped product's SolixBLE decode positions and command links against its
map.

Each SolixBLE device class connects to its emulated product and decodes every
property, except the C800's: its built ``0402`` (236 B) fits one frame, while
SolixBLE's ``C800`` takes ``c402`` only as a fragment run whose first fragment
fills the MTU, as the recorded C300 and C1000 ``c402`` do. The C800's field
widths aren't recorded, so its telemetry is shorter than the device's.

.. include:: _generated/map_built.rst

Commands and telemetry
----------------------

Measured against the commands each product is known to have, generated from the
package at build time. MCU commands (``0x40`` and up, relayed to the device's
MCU) are its firmware's command table, with anything anker-solix-api maps or a
recording answers. Module commands (below ``0x40``) are the session requests the
comms module answers itself, per module build: 14 on the C-series module, 6 on
the Prime module, counted on each product's default build. The handshake
commands (``0001``-``0029``) are all emulated.

.. include:: _generated/command_coverage.rst

- **Answered**: from a recording, or, for a command anker-solix-api maps that
  no recording answers, by an ack: ``00`` when its values are ones the map
  accepts and ``04`` when not.
- **Values checked**: the map gives the command's accepted values. A setting
  outside them is never applied. The Prime MCUs answer it ``04``; the C Gen 2
  display board acks every setter ``00`` (``fid0f_ack_reply``), applied or not.
- **Change telemetry**: an accepted command sets the telemetry fields the map
  links it to (``state_name``) in every message that carries them. Settings the
  map scales by a divider or ties to another setting stay unlinked.
- **Telemetry fields typed**: share of the product's mapped telemetry fields
  whose type is known, so they can be set by name
  (``device.set_values(battery_soc=55)``, in every message that carries the
  field) and built.

Layouts
-------

Every product anker-solix-api maps has a layout. A field's type comes from
recorded frames of that message, from the map itself, or from the same name in
other recorded maps; an untyped field is left out of built telemetry. Products
without an emulation profile are not emulated yet; their layouts show how much
of their telemetry could be built.

.. include:: _generated/layout_coverage.rst
