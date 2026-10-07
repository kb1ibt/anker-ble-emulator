Compatibility
=============

What each emulated product replicates. ✅ emulated, ❌ not yet, N/A the
product doesn't do it.

Emulated products
-----------------

=============================== ======== ========= ======== ========= ======== ========
Feature                         C1000 G2 C1000X G2 C2000 G2 C2000X G2 250W     240W
                                (A1763)  (A1765)   (A1783)  (A1785)   (A2345)  (A91B2)
=============================== ======== ========= ======== ========= ======== ========
Advertisement                   ✅       ✅        ✅       ✅        ✅       ✅
Encrypted outer (GCM session)   ✅       ✅        ✅       ✅        ✅       N/A
Plain outer (CBC session)       ✅       ✅        ✅       ✅        ✅       ✅
ECDH key exchange               ✅       ✅        ✅       ✅        ✅       ✅
Legacy AES key exchange         ❌       ❌        ❌       ❌        ❌       ❌
Owner confirmation by button    ✅       ✅        ✅       ✅        ✅       N/A
Module builds (``module=``)     ✅       ✅        ✅       ✅        ✅       ✅
Version read (``0030``)         ✅       ✅        ✅       ✅        ✅       ✅
Recorded module ops (``0020``…) ❌       ❌        ✅       ❌        ❌       ❌
Request routing by ``a1``       ✅       ✅        ✅       ✅        ✅       ✅
Fragmented frames               ✅       ✅        ✅       ✅        ✅       ✅
Recorded status reply           ✅       ✅        ✅       ✅        ✅       ✅
Recorded telemetry pushes       ✅       ✅        ✅       ✅        ✅       ✅
Summary push (``c490``)         ❌       ❌        ✅       ❌        N/A      N/A
Recorded command replies        ✅       ✅        ✅       ✅        ✅       ✅
Mapped command acks             ✅       ✅        ✅       ✅        ✅       ✅
Command value checks            ✅       ✅        ✅       ✅        ✅       ✅
Telemetry values by name        ✅       ✅        ✅       ✅        ✅       ✅
Commands shown in telemetry     ❌       ❌        ❌       ❌        ❌       ❌
Cloud mode (no BLE link)        ✅       ✅        ✅       ✅        ✅       ✅
=============================== ======== ========= ======== ========= ======== ========

- **Recorded** replies and pushes are sanitized frames from real units
  (:doc:`device-sources`). The C Gen 2 models share one display-board build,
  so they answer the same commands; frames that name the unit come from each
  model's own recording.
- **Mapped** commands are anker-solix-api's: one with no recorded reply gets an
  ack, ``00`` when its values are ones the map accepts and ``04`` when not.
- **Values by name** use anker-solix-api's field names
  (``device.set_values(0x421, battery_soc=55)``).
- The A91B2 runs auth mode 0: it negotiates in the clear and authorizes at the
  key exchange, so it has no confirmation step.

Layouts
-------

Every product anker-solix-api maps has a layout. A field's type comes from
recorded frames of that message, from the map itself, or from the same name in
other recorded maps; an untyped field is left out of built telemetry. Products
without an emulation profile are not emulated yet; their layouts show how much
of their telemetry could be built.

.. include:: _generated/layout_coverage.rst
