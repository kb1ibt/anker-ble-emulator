# Copyright (c) 2026 Shawn Stricker
"""The SOLIX C Gen 2 line (A1763, A1765, A1783, A1785): one display-board build.

The display board answers the same commands on every model, so the MCU replies
are shared; frames that name the unit (``0421``, ``0900``, ``0490``, ``0425``)
come from each model's own recording.
"""

#: Recordings of the line's identity-free MCU replies and pushes.
DATA = "solix_c_gen2.json"

#: Reply msgtypes by request msgtype; every setter is followed by a ``0421``.
REPLIES = {
    0x057: (0x857,),
    0x05E: (0x85E, 0x421),
    0x063: (0x863,),
    0x089: (0x889,),
    0x090: (0x890, 0x421),
    0x091: (0x891, 0x421),
    0x092: (0x892, 0x421),
    0x100: (0x900, 0x421),
    0x101: (0x901, 0x421),
    0x102: (0x902, 0x421),
    0x103: (0x903, 0x421),
    0x104: (0x904, 0x421),
}

#: Pushes every model can send.
PUSHES = (0x421, 0x489)

#: The display-board firmware ``0830`` reports.
DEVICE_VERSION = "v1.2.1.6"


def version_names(pn: str) -> tuple[str, str, str]:
    """Return ``0830`` ``a3``-``a5``: the OTA type names for ``pn``."""
    return f"{pn}_low", f"{pn}_mcu_low", f"{pn}_esp32_low"
