anker-ble-emulator
==================

Emulated Anker Solix BLE devices (the comms module and the device MCU behind it)
for testing BLE clients. The emulator plugs in under `bleak
<https://github.com/hbldh/bleak>`_ as a client backend, so a client's real stack
(bleak, bleak-retry-connector, the client library) runs unchanged above it.

.. toctree::
   :maxdepth: 2

   compatibility
   design
   device-sources
