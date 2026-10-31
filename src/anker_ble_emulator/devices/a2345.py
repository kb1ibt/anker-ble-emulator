# Copyright (c) 2026 Shawn Stricker
"""Prime Charger 250W (A2345)."""

from __future__ import annotations

import zlib
from datetime import UTC, datetime
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING

from construct import (
    Array,
    BitsInteger,
    BitStruct,
    Bytes,
    ExprValidator,
    Flag,
    GreedyBytes,
    Int8ul,
    Int32sl,
    Padding,
    Struct,
)
from cryptography.hazmat.primitives.ciphers import Cipher as AesCipher
from cryptography.hazmat.primitives.ciphers import algorithms, modes

from anker_ble_emulator.layouts import (
    STATUS_ACCEPTED,
    TYPE_BIN,
    TYPE_SILE,
    TYPE_UI,
    TYPE_VAR,
    ui,
)
from anker_ble_emulator.live import LiveSource
from anker_ble_emulator.mcu import COMMAND, RTC, UNIT_EVENT
from anker_ble_emulator.module import AuthMode
from anker_ble_emulator.parameters import (
    FieldGroup,
    FieldSpec,
    ParameterContainer,
    ParameterDict,
    Parameters,
    TableLayout,
    container,
    decode_field,
    decode_request,
    leaf_zeros,
    one_field,
)
from anker_ble_emulator.products import ModuleBuild, Outer, Path, Product
from anker_ble_emulator.timed import Step, Timed
from anker_ble_emulator.validators import OptionsValidator

from .base import Advert, ProductDevice, Profile, register
from .prime import (
    COUNTRY_PERIOD,
    MSGTYPE_303,
    MSGTYPE_A00,
    PORT_ECHOES,
    PORT_GROUPS,
    PUSH_ROUTES,
    REALTIME_BURST,
    SCHEDULE_HEAD,
    SCHEDULE_HEAD_FIELDS,
    STATS_PERIOD,
    SWITCH,
    TIMED_TASK_LEADS,
    WIFI_UP,
    ac_port_countdown_task,
    ac_port_timed_task,
    countdown_push,
    countdown_timers,
    drop_queued,
    every,
    mqtt_routed,
    reaches_ble,
    schedule_timers,
    set_screen_timeout,
    stats_push,
    timed_push,
    timed_task_conflict,
    timed_task_push,
    untimed_push,
    utc_hours,
    wifi_events,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from anker_ble_emulator.layouts import Value


# --- Telemetry: 0303 and 0a00 ------------------------------------------------

SCHEDULE_FIELDS = (*SCHEDULE_HEAD_FIELDS, "port_priority")
Schedule = Struct(*SCHEDULE_HEAD, "port_priority" / ui(1))
#: 0a00-only per-port switch, tasks and priority. pd1-pd4 each have their own
#: slot; the two USB-A ports share a single slot ("4a00 snapshot" doc: "per
#: port (usbc 1-4, usba)" -- a genuine asymmetry, not a modeling gap).
SCHEDULES = FieldGroup(
    Schedule,
    leaf_zeros(*SCHEDULE_FIELDS),
    {"pd1": "aa", "pd2": "ab", "pd3": "ac", "pd4": "ad", "usb": "ae"},
)

#: ``af``: screensaver switch and theme, 0a00-only. The 0x80|index convention
#: in the 4205 set command matches the switch (bit7) + theme (low bits); the
#: holiday switch is a separate bit, set only by 420f (PiField/docs/re/
#: prime-a2345-a91b2.md "Commands" 4205/420f).
Theme = BitStruct(
    "screen_saver_switch" / Flag,
    "holiday_screensaver_switch" / Flag,
    "screen_saver_theme" / BitsInteger(6),
    "screen_saver_theme_id" / BitsInteger(32, swapped=True),
)
#: ``4a02``, the ``4202`` read: ``a2`` the screensaver switch, holiday switch
#: and theme (bit 7 / 6 / low 6), ``a3`` the theme id (doc "Commands" ``4202``),
#: ``af``'s own names, so one value sets both; ``a6`` the custom screensaver's
#: Time Display, which ``4222`` sets (the app's Custom Clock page, doc ``4222``).
MSGTYPE_A02 = 0xA02
ScreensaverRead = BitStruct(
    "screen_saver_switch" / Flag,
    "holiday_screensaver_switch" / Flag,
    "screen_saver_theme" / BitsInteger(6),
)
SCREENSAVER_READ_FIELDS: dict[str, FieldSpec] = {
    "a2": (
        ScreensaverRead,
        0x01,
        leaf_zeros(
            "screen_saver_switch", "holiday_screensaver_switch", "screen_saver_theme"
        ),
    ),
    "a3": one_field("screen_saver_theme_id", ui(4), 0x04),
    "a6": one_field("time_display", ui(1), 0x01),
}
#: ``b9``: screensaver active-hours window, 0a00-only; the app names none of it.
SCREENSAVER_FIELDS = (
    "screensaver_start_hour",
    "screensaver_start_minute",
    "screensaver_end_hour",
    "screensaver_end_minute",
    "screensaver_weekdays",
)
Screensaver = Struct(*(name / ui(1) for name in SCREENSAVER_FIELDS))
#: ``b8``: the custom (mode 5) charge profile, 7 B, as ``030d a3`` carries it.
CustomChargeProfile = Struct("custom_charge_profile" / Bytes(7))
#: The USB-C ports with a PD controller, which read a device's USB-PD identity.
USBC_PORTS = ("pd1", "pd2", "pd3", "pd4")
#: ``bd`` (``0303 a8``): each USB-C port's device VID and PID from USB-PD
#: Discover Identity; ``{fffa, fffb}`` with none (doc ``bd`` row).
ID_FIELDS = tuple(f"{port}_device_{f}" for port in USBC_PORTS for f in ("vid", "pid"))
DeviceIds = Struct(*(name / ui(2) for name in ID_FIELDS))
#: ``be`` (``0303 a9``): each USB-C port's resolved {class, code}; ``ffff`` none.
IDENTITY_FIELDS = tuple(
    f"{port}_device_{f}" for port in USBC_PORTS for f in ("class", "code")
)
DeviceIdentities = Struct(*(name / ui(2) for name in IDENTITY_FIELDS))
IDENTITY_SPECS: dict[str, FieldSpec] = {
    "ids": (DeviceIds, 0x04, leaf_zeros(*ID_FIELDS)),
    "identities": (DeviceIdentities, 0x04, leaf_zeros(*IDENTITY_FIELDS)),
}
NO_ID = (0xFFFA, 0xFFFB)
NONE = 0xFFFF
#: ``be``'s class by the device's VID (table ``0x00072214``, doc "AI device
#: identity (``be``)"); any other VID is ``ffff``.
VENDOR_CLASSES = {
    0x05AC: 1,
    0x04E8: 2,
    0x2717: 3,
    0x12D1: 4,
    0x291A: 10,
    0x339B: 11,
    0x03F0: 12,
    0x413C: 13,
    0x17EF: 14,
    0x045E: 15,
    0x2A7E: 16,
    0x0B05: 17,
    0x0DB0: 18,
    0x1532: 19,
}
#: ``be``'s code by the device's PID: each PID's default (``variant ffff``) row
#: of table ``0x00072748`` (doc "PID → code"); any other PID is ``ffff``.
PID_CODES = {
    0x7504: 1, 0x7506: 5, 0x750B: 6, 0x7509: 7, 0x7507: 8, 0x750A: 11,
    0x750E: 12, 0x750D: 15, 0x750F: 16, 0x7510: 17, 0x7511: 18, 0x7512: 19,
    0x7513: 20, 0x7515: 21, 0x751C: 24, 0x7517: 25, 0x751B: 29, 0x7518: 30,
    0x7519: 31, 0x751A: 31, 0x7111: 34, 0x7112: 35, 0x711B: 36, 0x7108: 37,
    0x710D: 38, 0x7114: 39, 0x7115: 40, 0x711C: 41, 0x711D: 42, 0x710C: 43,
    0x7116: 44, 0x7102: 45, 0x7109: 48, 0x710E: 51, 0x7117: 54, 0x711E: 57,
    0x7302: 60, 0x7307: 61, 0x7304: 62, 0x7306: 63, 0x730B: 64, 0x7318: 65,
    0x731B: 66, 0x7310: 67, 0x7314: 68, 0x731C: 69, 0x730E: 70, 0x7308: 71,
    0x730C: 72, 0x7313: 73, 0x7319: 74, 0x731A: 77, 0x7309: 78, 0x730D: 79,
    0x7312: 80, 0x7315: 81, 0x7700: 84,
    0x110A: 1, 0x110B: 2, 0x1341: 3, 0x1665: 4, 0x1669: 5, 0x1903: 6, 0x25B1: 7,
    0x1340: 8, 0x25F7: 9, 0x2687: 10, 0x2345: 11, 0x91B2: 12, 0x1807: 13,
    0xA5A6: 1, 0xA513: 2, 0xA550: 3, 0xA54D: 4, 0xA549: 5,
}  # fmt: skip
#: The same table's variant rows: PID -> {variant: code}. With class 1, the row
#: whose variant is the port's extended identity word wins over the default.
PID_VARIANTS = {
    0x7515: {0x008C: 22, 0x00B4: 23},
    0x7517: {0x008C: 26, 0x00B4: 27},
    0x7519: {0x008C: 32, 0x00A0: 32, 0x00BE: 33},
    0x751A: {0x008C: 32, 0x00A0: 32, 0x00BE: 33},
    0x7102: {0x0118: 46, 0x0168: 47},
    0x7109: {0x0118: 49, 0x0190: 50},
    0x710E: {0x0118: 52, 0x012C: 52, 0x0190: 53},
    0x7117: {0x012C: 55, 0x017C: 56},
    0x711E: {0x012C: 58, 0x017C: 59},
}
#: The class whose PIDs have variant rows (``usbc_identity_resolve``).
VARIANT_CLASS = 1
#: The doc's one worked identity that resolves to none at all (``bd`` row).
UNRESOLVED = frozenset({(0x05AC, 0x7800)})
#: The 0a00-only single blocks, and its plain one-byte (type 0x01) scalars.
SNAPSHOT_FIELDS: dict[str, FieldSpec] = {
    "a2": one_field("firmware_version", ui(2), 0x02),
    "bd": IDENTITY_SPECS["ids"],
    "be": IDENTITY_SPECS["identities"],
    "af": (
        Theme,
        0x04,
        leaf_zeros(
            "screen_saver_switch",
            "holiday_screensaver_switch",
            "screen_saver_theme",
            "screen_saver_theme_id",
        ),
    ),
    "b8": (CustomChargeProfile, 0x04, leaf_zeros(custom_charge_profile=bytes(7))),
    "b9": (Screensaver, 0x04, leaf_zeros(*SCREENSAVER_FIELDS)),
    **{
        tag: one_field(name, ui(1), 0x01)
        for tag, name in {
            "b0": "screen_timeout",
            "b1": "charging_mode",
            "b3": "screen_brightness",
            "b4": "button_rotate_direction",
            "b5": "time_format",
            "bb": "compatibility_switch",
            "bf": "equipment_identification_switch",
        }.items()
    },
}


# --- Commands (fid 0x0f) ------------------------------------------------------
# PiField/docs/re/prime-a2345-a91b2.md "Commands (fid 0x0f, table 0x00072250,
# 36 slots)": every command below has a captured or firmware-documented
# payload shape. 4201 (factory reset) and 4202 (a read) have no handler here:
# a recording or the command table answers them, and the profile's
# factory_reset runs 4201's reset. 4206 mode 5 (custom charge profile) isn't
# modeled.

#: Port index 0-3 = pd1-pd4; 4 = usb (combined, per the doc's own "open" note
#: on that mapping) -- shared by 4207/4208/4209's port selector.
PORT_BY_SELECT = {0: "pd1", 1: "pd2", 2: "pd3", 3: "pd4", 4: "usb"}
PORT_SELECT = OptionsValidator(ui(1), *PORT_BY_SELECT)
#: The ports whose countdown disarm (or expiry) zeroes the remaining time: all.
DISARM_CLEARS = frozenset(PORT_BY_SELECT.values())
CHARGING_MODE = OptionsValidator(ui(1), 0, 1, 2, 3, 4)
#: ``4205 a2``: the screensaver switch (bit 7) over the theme (bits 0-5).
ScreensaverRequest = BitStruct(
    "screen_saver_switch" / Flag, Padding(1), "screen_saver_theme" / BitsInteger(6)
)
#: ``420c a2``: the 4-bit pd1-pd4 designation mask.
PRIORITY_MASK = ExprValidator(ui(1), lambda obj, _: obj <= 0x0F)  # noqa: PLR2004
#: ``420a a2``: the country, two characters after the type byte.
COUNTRY = ExprValidator(GreedyBytes, lambda obj, _: len(obj) == 2)  # noqa: PLR2004
#: ``420a a3``: the owner, 40 bytes (the app's) or a 36-character UUID.
OWNER = ExprValidator(GreedyBytes, lambda obj, _: len(obj) in {36, 40})
#: The owner the last ``420a`` stored, carried in no frame. A fresh device has
#: none, so its first ``420a`` counts as a change.
STORED_OWNER = "_owner"
#: The country the last ``420a`` stored (``0x2000b438``), carried in no frame.
STORED_COUNTRY = "_country"
#: ``af``'s custom-upload theme, which an owner change puts back to 0.
CUSTOM_THEME = 5
#: The custom charging mode, which an owner change leaves.
CUSTOM_MODE = 5
#: The last mode below 5 the device changed to (``DAT_2000af62``, copied on
#: every such change), carried in no frame; the factory default is 1, AI Power.
LAST_MODE = "_last_mode"
DEFAULT_MODE = 1
#: ``af``'s themes shown from an image: network (3) and custom (5); ``4220``'s
#: kind for each (0 network JPEG, 2 custom) is the screen it switches to.
IMAGE_THEMES = frozenset({3, CUSTOM_THEME})
THEME_BY_KIND = {0: 3, 2: CUSTOM_THEME}
#: A network or custom ``4205`` needs a URL at least this long.
MIN_URL = 16
#: ``4311 a3``: the image stored and shown, or wanted from the app (WiFi down);
#: with WiFi up the device fetches it itself (2, then 3 at its first chunk);
#: 0 answers a ``4205`` while a fetch is busy (doc "``4311`` image state").
IMAGE_BUSY = 0
IMAGE_SHOWN = 1
IMAGE_FETCHING = 2
IMAGE_FIRST_CHUNK = 3
IMAGE_WANTED = 4
#: The self-fetch's states land this long after its ``4205``, as recorded
#: (2026-09-08 16:17:43 MQTT: 0.38 s, 1.73 s, 8.6 s).
FETCH_STARTED = 0.38
FETCH_FIRST_CHUNK = 1.73
FETCH_DONE = 8.6
#: The theme being fetched, carried in no frame: its kind, id and CRC.
FETCH_THEME = "_fetch_theme"
FETCH_ID = "_fetch_id"
FETCH_CRC = "_fetch_crc"
#: The upload's state, carried in no frame: the theme a ``4205`` staged, the
#: ``4220`` it opened, the next index and running CRC32, and the answer the last
#: ``4221`` drew (a ``4a21`` status, or none).
STAGED_THEME_ID = "_theme_staged_id"
UPLOAD_KIND = "_upload_kind"
UPLOAD_WANT = "_upload_crc_wanted"
UPLOAD_SIZE = "_upload_size"
UPLOAD_WINDOW = "_upload_window"
UPLOAD_CHUNK = "_upload_chunk"
UPLOAD_COUNT = "_upload_count"
UPLOAD_NEXT = "_upload_next"
UPLOAD_CRC = "_upload_crc"
UPLOAD_STATUS = "_upload_status"
UPLOAD_ANSWER_INDEX = "_upload_answer_index"
#: The upload's bytes as flash holds them so far, and, for a custom photo, the
#: last cipher block, which chains into the next chunk (doc "Theme upload").
UPLOAD_IMAGE = "_upload_image"
UPLOAD_IV = "_upload_iv"
#: Kind 2, a custom photo, is AES-128-CBC encrypted on the device: one stream
#: per ``4220``, each chunk over its length rounded up to 16 (zeros past it),
#: flash keeping only the received length (doc "Theme upload", :483-485).
CUSTOM_KIND = 2
CUSTOM_KEY = bytes.fromhex("789b783678ec78e278a37834781878aa")
CUSTOM_IV = bytes.fromhex("e16ee1d8e1b3e18be18ee1d0e160e1aa")
AES_BLOCK = 16
#: Erased flash, what a read past the stored bytes finds.
ERASED = 0xFF
#: The draw (doc "Theme image display"): a stored size of 1 to 65 534 B, else
#: the slot is erased and screens 3 / 5 fall back to screen 1. The bench's
#: ``af`` reads index 1 after it, the photo's id kept (2026-10-30 ``c1``).
MAX_DRAWN = 0xFFFE
FALLBACK_THEME = 1
#: Each draw the screen asks for, carried in no frame: a count the timed hook
#: runs ``_draw`` on.
DRAWS = "_screen_draws"
#: ``4a21`` statuses (doc "Theme upload"); ``NO_ANSWER`` sends nothing.
UPLOAD_OK = 0x00
UPLOAD_DONE = 0x10
UPLOAD_BAD_CRC = 0x11
UPLOAD_RESEND = 0x12
NO_ANSWER = -1
#: ``b1`` Port Priority, the mode ``420c`` enters, and the per-port priority bytes.
PORT_PRIORITY_MODE = 2
UNSET = 0
NORMAL = 1
DESIGNATED = 2


def _set_screen_brightness(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screen brightness: no confirmed clamp, stores raw."""
    return decode_request(request, {"a2": ("screen_brightness", ui(1))})


def _set_screensaver(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screensaver switch, its theme and the ``a3`` theme id.

    Switching it off ignores the theme (the stored one stays) and clears the
    holiday switch. A network or custom theme waits for its image
    (``_theme_staging``).
    """
    packed = decode_field(request, "a2", ScreensaverRequest)
    if not packed.screen_saver_switch:
        return {"screen_saver_switch": False, "holiday_screensaver_switch": False}
    if packed.screen_saver_theme in IMAGE_THEMES:
        return {}
    return {
        "screen_saver_switch": True,
        "screen_saver_theme": packed.screen_saver_theme,
    } | decode_request(request, {"a3": ("screen_saver_theme_id", ui(4))})


def _theme_request(request: ParameterDict) -> tuple[int, int, int] | None:
    """Return a network or custom ``4205``'s (theme, id, CRC), if the MCU takes it.

    It needs ``a3`` an id and ``a4`` a CRC, both non-zero, ``a5``, and a URL of
    at least 16 characters in ``a7`` or ``a6`` (doc "Theme upload").
    """
    packed = decode_field(request, "a2", ScreensaverRequest)
    if not packed.screen_saver_switch or packed.screen_saver_theme not in IMAGE_THEMES:
        return None
    url = request.get("a7") or request.get("a6")
    if any(tag not in request for tag in ("a3", "a4", "a5")) or url is None:
        return None
    theme_id = decode_field(request, "a3", ui(4))
    crc = decode_field(request, "a4", ui(4))
    if not theme_id or not crc or len(url.get("value") or b"") < MIN_URL:
        return None
    return packed.screen_saver_theme, theme_id, crc


def _shown(theme: int, theme_id: int) -> dict[str, Value]:
    """Return the screensaver showing ``theme_id`` as ``theme``: 3 or 5."""
    return {
        "screen_saver_switch": True,
        "screen_saver_theme": theme,
        "screen_saver_theme_id": theme_id,
    }


def _stored(theme_id: int) -> str:
    """Return the hidden name of the CRC of ``theme_id``'s stored image."""
    return f"_theme_crc_{theme_id}"


def _slot(theme_id: int, part: str) -> str:
    """Return the hidden name of ``theme_id``'s slot ``part``: size, kind, image."""
    return f"_theme_{part}_{theme_id}"


def _slot_names(theme_id: int) -> tuple[str, ...]:
    """Return every hidden name of ``theme_id``'s slot: its record and its bytes."""
    return (_stored(theme_id), *(_slot(theme_id, p) for p in ("size", "kind", "image")))


def _draw_asked(state: Mapping[str, Value]) -> dict[str, Value]:
    """Return the values that ask the screen to draw what it shows."""
    return {DRAWS: int(state.get(DRAWS, 0)) + 1}


def _theme_staging(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Show a stored network or custom theme; fetch or stage any other.

    With WiFi up the device fetches the image itself; with it down the app
    uploads it. Nothing changes while a fetch is busy.
    """
    taken = _theme_request(request)
    if taken is None or state.get(FETCH_ID):
        return {}
    theme, theme_id, crc = taken
    if state.get(_stored(theme_id)) == crc:
        return _shown(theme, theme_id) | _draw_asked(state)
    if state.get(WIFI_UP):
        return {FETCH_THEME: theme, FETCH_ID: theme_id, FETCH_CRC: crc}
    return {STAGED_THEME_ID: theme_id}


def _theme_state(theme_id: int, image_state: int, theme: int) -> ParameterDict:
    """``4311``: the theme id, its image state and its kind (3 network, 5 custom)."""
    return ParameterDict(
        {
            "a2": container("a2", TYPE_BIN, ui(4).build(theme_id)),
            "a3": container("a3", TYPE_UI, ui(1).build(image_state)),
            "a4": container("a4", TYPE_UI, ui(1).build(theme)),
        }
    )


def _theme_selected(
    request: ParameterDict,
    before: Mapping[str, Value],
    after: Mapping[str, Value],
) -> ParameterDict | None:
    """``4311`` after a network or custom ``4205``.

    1 shown, 4 the image wanted, 0 while a fetch is busy; none when a fetch
    starts, whose states follow on their own (``_theme_fetch``).
    """
    taken = _theme_request(request)
    if taken is None:
        return None
    theme, theme_id, crc = taken
    if before.get(FETCH_ID):
        return _theme_state(theme_id, IMAGE_BUSY, theme)
    if after.get(_stored(theme_id)) == crc:
        return _theme_state(theme_id, IMAGE_SHOWN, theme)
    if after.get(FETCH_ID):
        return None
    return _theme_state(theme_id, IMAGE_WANTED, theme)


def _theme_fetch(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return a self-fetch's first state report, if a ``4205`` started one."""
    if after.get(FETCH_ID) and not before.get(FETCH_ID):
        return (Timed(FETCH_STARTED, "theme_fetch", _fetch_started),)
    return ()


def _fetch_report(state: Mapping[str, Value], image_state: int) -> tuple[int, bytes]:
    """``0311`` for the image being fetched; it carries no clock."""
    fields = _theme_state(int(state[FETCH_ID]), image_state, int(state[FETCH_THEME]))
    return untimed_push(0x311, fields)


def _fetch_started(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``0311`` 2: the device fetches the URL itself."""
    then = Timed(FETCH_FIRST_CHUNK - FETCH_STARTED, "theme_fetch", _fetch_chunked)
    return Step(frames=(_fetch_report(state, IMAGE_FETCHING),), then=(then,))


def _fetch_chunked(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``0311`` 3: the first fetched chunk arrived."""
    then = Timed(FETCH_DONE - FETCH_FIRST_CHUNK, "theme_fetch", _fetched)
    return Step(frames=(_fetch_report(state, IMAGE_FIRST_CHUNK),), then=(then,))


def _fetched(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``0311`` 1: the last chunk stored the image, and the theme shows.

    The emulator fetches no bytes, so the image always passes its CRC.
    """
    theme, theme_id = int(state[FETCH_THEME]), int(state[FETCH_ID])
    return Step(
        values={_stored(theme_id): state[FETCH_CRC]} | _shown(theme, theme_id),
        unset=(FETCH_THEME, FETCH_ID, FETCH_CRC),
        frames=(_fetch_report(state, IMAGE_SHOWN),),
    )


def _country_utc(state: Mapping[str, Value]) -> tuple[int, bytes] | None:
    """``0312``: the ``420a`` country and the UTC offset in hours (int32).

    Sent only with the holiday switch on and a country stored (doc "Periodic
    pushes"); WiFi up is the timer's own gate.
    """
    country = state.get(STORED_COUNTRY)
    if not isinstance(country, bytes) or not country:
        return None
    if not state["holiday_screensaver_switch"]:
        return None
    fields = {
        "a2": container("a2", TYPE_BIN, country),
        "a3": container("a3", TYPE_VAR, Int32sl.build(utc_hours(state))),
    }
    return timed_push(0x312, fields, state)


#: The A2345's MQTT-only periodics while WiFi is up: ``0300`` stats (0.1 W) and
#: ``0312`` country / UTC.
PERIODICS = (
    every(STATS_PERIOD, "stats_0300", partial(stats_push, step=10)),
    every(COUNTRY_PERIOD, "country_0312", _country_utc),
)


def _prepare_upload(request: ParameterDict) -> dict[str, Value] | None:
    """Open a ``4220`` upload: its kind, size, CRC, window, chunk and count.

    ``a3`` must be present but isn't stored; a kind other than 0 or 2 changes
    nothing and gets no reply. The expected index and the CRC start at 0.
    """
    if "a3" not in request:
        return None
    kind = decode_field(request, "a2", ui(1))
    if kind not in THEME_BY_KIND:
        return {}
    return {
        UPLOAD_KIND: kind,
        UPLOAD_WANT: decode_field(request, "a4", ui(4)),
        UPLOAD_SIZE: decode_field(request, "a5", ui(4)),
        UPLOAD_WINDOW: decode_field(request, "a6", ui(1)),
        UPLOAD_CHUNK: decode_field(request, "a7", ui(2)),
        UPLOAD_COUNT: decode_field(request, "a8", ui(2)),
        UPLOAD_NEXT: 0,
        UPLOAD_CRC: 0,
        UPLOAD_IMAGE: b"",
        UPLOAD_IV: CUSTOM_IV,
    }


def _upload_prepared(
    request: ParameterDict,
    state: Mapping[str, Value],  # noqa: ARG001  # the request decides it
) -> ParameterDict | None:
    """``4a20``: ``00``, or no reply to a kind other than 0 or 2."""
    return (
        ParameterDict() if decode_field(request, "a2", ui(1)) in THEME_BY_KIND else None
    )


def _upload_chunk(request: ParameterDict) -> dict[str, Value] | None:
    """Accept a ``4221`` with an index and a chunk; ``_upload_step`` applies it."""
    if "a2" not in request or "a3" not in request:
        return None
    decode_field(request, "a2", ui(2))
    return {}


def _upload_step(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Apply one ``4221`` chunk and set the answer its reply gives.

    The expected index is written and its CRC32 extended (the last chunk over
    the bytes the size leaves); ``00`` + the next index after each window,
    ``10`` (stored and shown) or ``11`` (bad CRC) + the count on the last. Any
    other index, or one with no upload open, is dropped, but on a window's last
    slot answers ``12`` + the expected index (doc "Theme upload").
    """
    window = int(state.get(UPLOAD_WINDOW, 0))
    index = decode_field(request, "a2", ui(2))
    expected = int(state.get(UPLOAD_NEXT, 0))
    window_end = bool(window) and (index + 1) % window == 0
    if not window or index != expected:
        if window_end:
            return {UPLOAD_STATUS: UPLOAD_RESEND, UPLOAD_ANSWER_INDEX: expected}
        return {UPLOAD_STATUS: NO_ANSWER}
    count, step = int(state[UPLOAD_COUNT]), int(state[UPLOAD_CHUNK])
    received = request["a3"].get("value") or b""
    last = index + 1 == count
    data = received[: int(state[UPLOAD_SIZE]) % step or step] if last else received
    crc = zlib.crc32(data, int(state[UPLOAD_CRC]))
    kind = int(state[UPLOAD_KIND])
    written, chain = received, stored_bytes(state, UPLOAD_IV)
    if kind == CUSTOM_KIND:
        written, chain = _encrypted_chunk(received, chain)
    image = stored_bytes(state, UPLOAD_IMAGE).ljust(step * index, bytes((ERASED,)))
    image = image[: step * index] + written
    changes: dict[str, Value] = {
        UPLOAD_NEXT: index + 1,
        UPLOAD_CRC: crc,
        UPLOAD_IMAGE: image,
        UPLOAD_IV: chain,
    }
    if not last:
        status = UPLOAD_OK if window_end else NO_ANSWER
        return changes | {UPLOAD_STATUS: status, UPLOAD_ANSWER_INDEX: index + 1}
    if crc != state[UPLOAD_WANT]:
        return changes | {UPLOAD_STATUS: UPLOAD_BAD_CRC, UPLOAD_ANSWER_INDEX: count}
    theme_id = int(state.get(STAGED_THEME_ID, 0))
    slot: dict[str, Value] = {
        _stored(theme_id): crc,
        _slot(theme_id, "size"): int(state[UPLOAD_SIZE]),
        _slot(theme_id, "kind"): kind,
        _slot(theme_id, "image"): image,
    }
    return (
        changes
        | slot
        | _shown(THEME_BY_KIND[kind], theme_id)
        | _draw_asked(state)
        | {UPLOAD_STATUS: UPLOAD_DONE, UPLOAD_ANSWER_INDEX: count}
    )


def _encrypted_chunk(chunk: bytes, chain: bytes) -> tuple[bytes, bytes]:
    """Return a custom chunk as flash keeps it, and the cipher block it chains on.

    It's encrypted over its length rounded up to 16 (zeros past it), from the
    cipher block before it; flash keeps only the received length (:485).
    """
    padded = chunk + bytes(-len(chunk) % AES_BLOCK)
    encryptor = AesCipher(algorithms.AES(CUSTOM_KEY), modes.CBC(chain)).encryptor()
    sealed = encryptor.update(padded) + encryptor.finalize()
    return sealed[: len(chunk)], sealed[-AES_BLOCK:] or chain


def _drawn(kind: int, size: int, crc: int, image: bytes) -> bool:
    """Whether a stored image draws (doc "Theme image display").

    Its size is 1 to 65 534 B; a custom one is decrypted in one CBC pass from
    the fixed IV over the size rounded up to 16, flash past the stored bytes
    reading erased; then the CRC over the size, and the JPEG header.
    """
    if not 1 <= size <= MAX_DRAWN:
        return False
    span = size + -size % AES_BLOCK if kind == CUSTOM_KIND else size
    plain = image.ljust(span, bytes((ERASED,)))[:span]
    if kind == CUSTOM_KIND:
        decryptor = AesCipher(
            algorithms.AES(CUSTOM_KEY), modes.CBC(CUSTOM_IV)
        ).decryptor()
        plain = decryptor.update(plain) + decryptor.finalize()
    return zlib.crc32(plain[:size]) == crc and _jpeg_decodes(plain[:size])


#: The markers the header parse skips by their length: APP0-APP15, COM, DRI.
SKIPPED_MARKERS = frozenset({*range(0xE0, 0xF0), 0xFE, 0xDD})
MARKER, SOI, DQT, DHT, SOF0, SOS = 0xFF, 0xD8, 0xDB, 0xC4, 0xC0, 0xDA
#: SOF0's luma sampling the decoder takes: 4:4:4, 4:2:2, 4:2:0.
LUMA_SAMPLING = frozenset({0x11, 0x21, 0x22})
JPEG_COMPONENTS = 3
JPEG_PRECISION = 8
MAX_QUANT_ID = 2
MAX_HUFFMAN_ID = 1
QUANT_TABLE = 64
HUFFMAN_COUNTS = 16
SCAN_SPECTRAL = b"\x00\x3f\x00"


def _jpeg_decodes(data: bytes) -> bool:  # one return per rule
    """Whether the hardware decoder takes ``data``'s header (``jpeg_header_parse``).

    From byte 0: SOI; APPn, COM and DRI skipped by length; DQT, DHT, SOF0 and
    SOS checked; any other marker before SOS fails, fill bytes and RSTn too.
    """
    if data[:2] != bytes((MARKER, SOI)):
        return False
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != MARKER:
            return False
        marker = data[pos + 1]
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if pos + 2 + length > len(data):
            return False
        body = data[pos + 4 : pos + 2 + length]
        if marker == SOS:
            return _scan_decodes(body)
        if marker not in SKIPPED_MARKERS and not _segment_decodes(marker, body):
            return False
        pos += 2 + length
    return False


def _segment_decodes(marker: int, body: bytes) -> bool:
    """Whether a DQT, DHT or SOF0 segment passes; any other marker fails."""
    if marker == DQT:
        return all(
            body[at] >> 4 == 0 and body[at] & 0x0F <= MAX_QUANT_ID
            for at in range(0, len(body), 1 + QUANT_TABLE)
        )
    if marker == DHT:
        at = 0
        while at < len(body):
            if body[at] & 0x0F > MAX_HUFFMAN_ID:
                return False
            at += 1 + HUFFMAN_COUNTS + sum(body[at + 1 : at + 1 + HUFFMAN_COUNTS])
        return True
    if marker == SOF0:
        return (
            body[0] == JPEG_PRECISION
            and body[5] == JPEG_COMPONENTS
            and body[7] in LUMA_SAMPLING
        )
    return False


def _scan_decodes(body: bytes) -> bool:
    """Whether SOS is one scan of 3 components with Ss/Se/Ah-Al ``00 3f 00``."""
    count = body[0]
    return count == JPEG_COMPONENTS and body[1 + 2 * count : 4 + 2 * count] == (
        SCAN_SPECTRAL
    )


def _draw_events(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return the screen's draw, when a change asked for one."""
    if after.get(DRAWS) != before.get(DRAWS):
        return (Timed(0, "screen_draw", _draw),)
    return ()


def _draw(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Draw the theme shown: a stored image that fails is erased, screen 1.

    An image whose bytes the emulator doesn't hold (fetched, or recorded)
    draws as shown.
    """
    theme_id = int(state["screen_saver_theme_id"])
    image = state.get(_slot(theme_id, "image"))
    if state["screen_saver_theme"] not in IMAGE_THEMES or not isinstance(image, bytes):
        return Step()
    kind, size = (
        int(state[_slot(theme_id, "kind")]),
        int(state[_slot(theme_id, "size")]),
    )
    if _drawn(kind, size, int(state[_stored(theme_id)]), image):
        return Step()
    return Step(
        values={"screen_saver_theme": FALLBACK_THEME}, unset=_slot_names(theme_id)
    )


def _upload_answer(
    request: ParameterDict,  # noqa: ARG001  # the chunk's step set the answer
    state: Mapping[str, Value],
) -> ParameterDict | None:
    """``4a21``: the status and index the chunk's step set, or no reply."""
    status = int(state[UPLOAD_STATUS])
    if status == NO_ANSWER:
        return None
    index = container("a2", TYPE_SILE, ui(2).build(state[UPLOAD_ANSWER_INDEX]))
    return ParameterDict({"a2": index}, prefix=bytes([status]))


def _upload_done(
    request: ParameterDict,  # noqa: ARG001  # the chunk's step decided it
    before: Mapping[str, Value],  # noqa: ARG001
    after: Mapping[str, Value],
) -> ParameterDict | None:
    """``4311`` 1, before the last chunk's ``4a21 10``: the image stored and shown.

    It names the upload's own id and kind, whatever its first draw then does.
    """
    if after[UPLOAD_STATUS] != UPLOAD_DONE:
        return None
    return _theme_state(
        int(after.get(STAGED_THEME_ID, 0)),
        IMAGE_SHOWN,
        THEME_BY_KIND[int(after[UPLOAD_KIND])],
    )


def _set_holiday_screensaver(request: ParameterDict) -> dict[str, Value] | None:
    """Return the holiday screensaver switch if ``a2`` is 0 or 1."""
    return {"holiday_screensaver_switch": bool(decode_field(request, "a2", SWITCH))}


#: The holiday slot's record (``0xff0000``), carried in no frame: the theme id
#: (0 = none), the file CRC32, ``a4`` (stored, its role unread), the window's
#: start and end as sent, the URL, and the ``420a`` country when it landed.
HOLIDAY_ID = "_holiday_id"
HOLIDAY_START = "_holiday_start"
HOLIDAY_END = "_holiday_end"
HOLIDAY_URL = "_holiday_url"
HOLIDAY_COUNTRY = "_holiday_country"
HOLIDAY_FIELDS = {
    "a2": (HOLIDAY_ID, ui(4)),
    "a3": ("_holiday_crc", ui(4)),
    "a4": ("_holiday_a4", ui(4)),
    "a5": (HOLIDAY_START, Bytes(7)),
    "a6": (HOLIDAY_END, Bytes(7)),
    "a7": (HOLIDAY_URL, GreedyBytes),
}
#: The erased slot: every record value back to nothing.
HOLIDAY_ERASED: dict[str, Value] = {
    HOLIDAY_ID: 0,
    "_holiday_crc": 0,
    "_holiday_a4": 0,
    HOLIDAY_START: b"",
    HOLIDAY_END: b"",
    HOLIDAY_URL: b"",
    HOLIDAY_COUNTRY: b"",
}
#: ``4212``'s window bounds: ``[year - 2000, month, day, hour, min, sec, -]``,
#: local time (doc ``4212`` row).
HolidayTime = Struct(
    *(name / Int8ul for name in ("year", "month", "day", "hour", "minute", "second")),
    Padding(1),
)
#: The fetch in flight, carried in no frame: its ``4212`` request, and a count
#: that starts each one.
HOLIDAY_FETCH = "_holiday_fetch"
HOLIDAY_FETCHES = "_holiday_fetches"
YEAR_BASE = 2000


def _holiday_schedule_taken(
    request: ParameterDict,  # noqa: ARG001  # applied with the state below
) -> dict[str, Value]:
    """Take any ``4212``: the handler never replies or rejects one.

    ``_holiday_schedule`` applies it, with the WiFi state it needs.
    """
    return {}


def _holiday_schedule(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """``4212``: ``a2 = 0`` erases the holiday slot; any other id fetches its image.

    The image is fetched over WiFi at once; with WiFi down the request is
    dropped, nothing stored. A request missing a field changes nothing.
    """
    if "a2" not in request:
        return {}
    if not decode_field(request, "a2", ui(4)):
        return dict(HOLIDAY_ERASED)
    if any(tag not in request for tag in HOLIDAY_FIELDS) or not state.get(WIFI_UP):
        return {}
    return {
        HOLIDAY_FETCH: Parameters.build(request),
        HOLIDAY_FETCHES: int(state.get(HOLIDAY_FETCHES, 0)) + 1,
    }


def _holiday_fetch(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return the holiday image landing, if a ``4212`` started its fetch."""
    if after.get(HOLIDAY_FETCHES) != before.get(HOLIDAY_FETCHES):
        return (Timed(FETCH_DONE, "holiday_fetch", _holiday_landed),)
    return ()


def _holiday_landed(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Land the holiday image: the slot record takes the request and the country.

    No message marks the landing, so the recorded network self-fetch's 8.6 s
    stands in; the emulator fetches no bytes, so the CRC always matches. No
    ``0311`` (kind 1 pushes none, doc "Theme upload").
    """
    fetch = stored_bytes(state, HOLIDAY_FETCH)
    record = decode_request(Parameters.parse(fetch), HOLIDAY_FIELDS)
    country = state.get(STORED_COUNTRY, b"")
    return Step(values={**record, HOLIDAY_COUNTRY: country}, unset=(HOLIDAY_FETCH,))


def stored_bytes(values: Mapping[str, Value], name: str) -> bytes:
    """Return the bytes value ``name``.

    Raises:
        TypeError: If it isn't bytes.

    """
    value = values[name]
    if not isinstance(value, bytes):
        msg = f"{name} is a bytes value"
        raise TypeError(msg)
    return value


def _holiday_time(values: Mapping[str, Value], name: str) -> tuple[int, ...]:
    """Return window bound ``name`` as (year - 2000, month, day, hour, min, sec)."""
    parsed = HolidayTime.parse(stored_bytes(values, name))
    return tuple(
        parsed[name] for name in ("year", "month", "day", "hour", "minute", "second")
    )


def _holiday_showing(switch: bool, values: Mapping[str, Value]) -> bool:  # noqa: FBT001
    """Whether the holiday theme shows (``holiday_theme_active``).

    The holiday ``switch`` on, a record whose country is the current ``420a``
    one, and the local RTC inside [start, end].
    """
    if not switch or not values.get(HOLIDAY_ID):
        return False
    if values.get(HOLIDAY_COUNTRY) != values.get(STORED_COUNTRY):
        return False
    now = datetime.fromtimestamp(int(values[RTC]), tz=UTC)
    clock = (
        now.year - YEAR_BASE,
        now.month,
        now.day,
        now.hour,
        now.minute,
        now.second,
    )
    start = _holiday_time(values, HOLIDAY_START)
    return start <= clock <= _holiday_time(values, HOLIDAY_END)


def _set_region_owner(request: ParameterDict) -> dict[str, Value] | None:
    """Return the country (uppercased) and owner to store, if both fit.

    ``a2`` is a country of two characters, ``a3`` an owner (doc "``420a``").
    """
    if "a2" not in request or "a3" not in request:
        return None
    return {
        STORED_COUNTRY: decode_field(request, "a2", COUNTRY).upper(),
        STORED_OWNER: decode_field(request, "a3", OWNER),
    }


def _owner_change(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Return what a different owner in ``420a`` resets.

    A custom-upload theme goes back to 0, and custom charging mode 5 to the last
    mode below 5 that was active (``charging_mode_set(DAT_2000af62)``, doc
    "``420a``").
    """
    if state.get(STORED_OWNER, b"") == decode_field(request, "a3", OWNER):
        return {}
    changes = _leave_custom_mode(state)
    if state["screen_saver_theme"] == CUSTOM_THEME:
        changes["screen_saver_theme"] = 0
    return changes


def _leave_custom_mode(state: Mapping[str, Value]) -> dict[str, Value]:
    """Return custom charging mode 5's exit: the last mode below 5 that was active.

    The firmware's exits all do ``charging_mode_set(DAT_2000af62)`` (doc
    "``420a``"): ``420a`` with a new owner and ``4211 a2`` 0 here; its third, an
    unbind (``esp_op06_bind_status``), has no counterpart in the emulator.
    """
    if state["charging_mode"] != CUSTOM_MODE:
        return {}
    return {"charging_mode": state.get(LAST_MODE, DEFAULT_MODE)}


def _custom_mode_switch_off(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """``4211 a2`` 0 leaves custom mode 5; 1 changes nothing documented."""
    return {} if decode_field(request, "a2", SWITCH) else _leave_custom_mode(state)


def _remember_mode(
    mode: int,
    values: Mapping[str, Value],  # noqa: ARG001  # stores a known mode
    state: Mapping[str, Value],  # noqa: ARG001
) -> Step:
    """Store a mode below 5 the device changed to, for an exit from custom mode."""
    return Step(values={LAST_MODE: mode})


def _set_charging_mode(request: ParameterDict) -> dict[str, Value] | None:
    """Return the charging mode for modes 0-4; mode 5's profile is unmodeled."""
    mode = decode_field(request, "a2", CHARGING_MODE)
    return {"charging_mode": mode} if mode else {}


def _set_dc_relay_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Return a port's switch (its record's ``[0]``), if ``a2`` and ``a3`` fit."""
    name = PORT_BY_SELECT[decode_field(request, "a2", PORT_SELECT)]
    return {f"{name}_relay_switch": bool(decode_field(request, "a3", SWITCH))}


def _set_port_priority(request: ParameterDict) -> dict[str, Value] | None:
    """Enter Port Priority mode; each pd port's priority byte from the mask."""
    mask = decode_field(request, "a2", PRIORITY_MASK)
    changes: dict[str, Value] = {"charging_mode": PORT_PRIORITY_MODE}
    for i in range(4):
        changes[f"pd{i + 1}_port_priority"] = DESIGNATED if mask & (1 << i) else NORMAL
    return changes


def _leave_port_priority(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Clear every pd port's priority byte when a ``4206`` leaves Port Priority."""
    mode = decode_field(request, "a2", CHARGING_MODE)
    if state["charging_mode"] != PORT_PRIORITY_MODE or mode in {0, PORT_PRIORITY_MODE}:
        return {}
    return {f"pd{i + 1}_port_priority": UNSET for i in range(4)}


def _designated_ports(state: Mapping[str, Value]) -> int:
    """Return the designated-port mask: bit i set where usbc i+1 is designated."""
    return sum(
        1 << i for i in range(4) if state[f"pd{i + 1}_port_priority"] == DESIGNATED
    )


def _switch_change(
    request: ParameterDict, before: Mapping[str, Value], after: Mapping[str, Value]
) -> ParameterDict | None:
    """``0302``: a port's switch, when a ``4207`` changed it (doc "MCU pushes")."""
    name = f"{PORT_BY_SELECT[decode_field(request, 'a2', PORT_SELECT)]}_relay_switch"
    if before[name] == after[name]:
        return None
    return ParameterDict({"a2": request["a2"], "a3": request["a3"]})


#: ``030d`` follows a mode change by the fixed mode-apply settle counter, on
#: ``420c`` entry and a ``4206`` alike (~3.3 s observed; doc "MCU pushes" ``030f``).
MODE_SETTLE = 3.3
#: How long a mode change holds every port at 0 W while they renegotiate (doc
#: "Commands" ``4206``: "all drop to 0 W for ~8 s").
RENEGOTIATION = 8.0
#: The port-block fields a renegotiation zeroes, in both ``0303`` and ``4a00``.
RENEGOTIATED = tuple(
    f"{port}_{field}"
    for port in PORT_GROUPS[MSGTYPE_303].tags
    for field in ("output_status", "output_power")
)
#: Set while the ports renegotiate: a further mode change keeps the one window.
RENEGOTIATING = "_ports_renegotiating"


def _mode_report(
    values: Mapping[str, Value],  # noqa: ARG001  # built from the whole state
    state: Mapping[str, Value],
) -> Step:
    """``030d``: the mode, ``b8`` and the designated ports, once the mode settles."""
    profile = {"custom_charge_profile": state["custom_charge_profile"]}
    fields = {
        "a2": container("a2", TYPE_UI, ui(1).build(state["charging_mode"])),
        "a3": container("a3", TYPE_BIN, CustomChargeProfile.build(profile)),
        "a4": container("a4", TYPE_UI, ui(1).build(_designated_ports(state))),
    }
    return Step(frames=(timed_push(0x30D, fields, state),))


#: The on-unit screen reports and the setting each carries in ``a2`` (doc "Screen
#: reports"). They carry no ``a3``: a reading of the doc's bracketed ``a3``,
#: since none is recorded.
SCREEN_REPORTS = {
    0x30A: "screen_brightness",
    0x309: "screen_timeout",
    0x310: "button_rotate_direction",
}
#: What ``030b`` reports: ``af[0]`` (switch, holiday, theme) and the theme id.
THEME_FIELDS = (
    "screen_saver_switch",
    "holiday_screensaver_switch",
    "screen_saver_theme",
    "screen_saver_theme_id",
)


def _screen_reports(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return a report of each screen setting changed on the unit.

    A change from an app command reports nothing (doc "Screen reports": "sent
    when a setting changes on the unit"; collector.mqtt-102026-2.log.gz).
    """
    if COMMAND in after:
        return ()
    events = [
        Timed(
            0, f"screen_report_{msgtype:03x}", partial(_setting_report, msgtype, name)
        )
        for msgtype, name in SCREEN_REPORTS.items()
        if before[name] != after[name]
    ]
    if any(before[name] != after[name] for name in THEME_FIELDS):
        events.append(Timed(0, "screen_report_30b", _theme_report))
    return tuple(events)


def _setting_report(
    msgtype: int,
    name: str,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``030a`` / ``0309`` / ``0310``: the setting's value in ``a2``."""
    value = container("a2", TYPE_UI, ui(1).build(state[name]))
    return Step(frames=(timed_push(msgtype, {"a2": value}, state),))


def _theme_report(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``030b``: ``af[0]`` in ``a2``, the theme id in ``a3``, as recorded."""
    packed = ScreensaverRead.build({name: state[name] for name in THEME_FIELDS[:3]})
    fields = {
        "a2": container("a2", TYPE_UI, packed),
        "a3": container("a3", TYPE_BIN, ui(4).build(state["screen_saver_theme_id"])),
    }
    return Step(frames=(timed_push(0x30B, fields, state),))


#: The ports ``0304``'s bitmask covers, bit 0 first: assumed the ``0300``
#: active-port bitmap's order (bit 0 C1 ... bit 5 A2), set where attached.
ATTACH_PORTS = (*USBC_PORTS, "usb1", "usb2")
#: A plugged device's identity reaches ``bd`` this long after attach, and ``be``
#: one ``0303`` frame later (doc ``bd`` row: 20:05:26.6 -> 30.6 -> 31.6).
IDENTIFY = 4.0
RESOLVE = 1.0
#: ``bb`` 0 -> 1 identifies the attached devices this long after (doc "``bb``
#: Maximum Compatibility": ~6 s after the first ``0214``).
REIDENTIFY = 6.0


def _plugged(port: str, field: str) -> str:
    """Return the hidden name of the identity a device plugged into ``port`` gives."""
    return f"_{port}_plugged_{field}"


def _attach_events(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return a port change's ``0304`` and each USB-C port's identification.

    With ``bb`` 1 a plugged device is read 4 s after attach; with ``bb`` 0 it
    isn't until ``bb`` turns 1 (6 s later); a port identified before ``bb``
    turns 0 keeps its identity. A detach clears the port at once.
    """
    attached = {port: bool(after[f"{port}_output_status"]) for port in ATTACH_PORTS}
    events = []
    if any(bool(before[f"{p}_output_status"]) != attached[p] for p in ATTACH_PORTS):
        events.append(Timed(0, "port_change", _port_change))
    identify = after["compatibility_switch"] == 1
    turned_on = identify and before["compatibility_switch"] != 1
    for port in USBC_PORTS:
        key = f"{port}_identity"
        was = bool(before[f"{port}_output_status"])
        if was and not attached[port]:
            events.append(Timed(0, key, partial(_unidentified, port)))
        elif attached[port] and _plugged(port, "vid") in after and identify:
            if not was:
                events.append(Timed(IDENTIFY, key, partial(_identified, port)))
            elif turned_on:
                events.append(Timed(REIDENTIFY, key, partial(_identified, port)))
    return tuple(events)


def _port_change(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``0304``: the attached ports' bitmask, in ``a2`` typed ``03``.

    Firmware-only (``port_change_push_0304``); no ``0304`` is recorded, so its
    shape is the doc's ``0304`` row.
    """
    mask = sum(
        1 << bit
        for bit, port in enumerate(ATTACH_PORTS)
        if state[f"{port}_output_status"]
    )
    fields = {"a2": container("a2", TYPE_VAR, ui(2).build(mask))}
    return Step(frames=(timed_push(0x304, fields, state),))


def _identified(
    port: str,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``bd``: the plugged device's VID and PID, then ``be`` one frame on."""
    ids = {f"{port}_device_{f}": state[_plugged(port, f)] for f in ("vid", "pid")}
    resolve = Timed(RESOLVE, f"{port}_identity", partial(_resolved, port))
    return Step(values=ids, then=(resolve,))


def _resolved(
    port: str,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """``be``: the class by the VID, the code by the PID's row.

    The default row, or with class 1 the variant row matching the port's
    extended identity word. ``05ac:7800`` resolves to none, the doc's one
    worked case of it (``bd`` row).
    """
    vid, pid = int(state[f"{port}_device_vid"]), int(state[f"{port}_device_pid"])
    ext = int(state.get(_plugged(port, "ext"), NONE))
    vendor = VENDOR_CLASSES.get(vid, NONE)
    code = PID_CODES.get(pid, NONE)
    if vendor == VARIANT_CLASS and ext not in {0, NONE}:
        code = PID_VARIANTS.get(pid, {}).get(ext, code)
    identity = (NONE, NONE) if (vid, pid) in UNRESOLVED else (vendor, code)
    return Step(
        values={
            f"{port}_device_class": identity[0],
            f"{port}_device_code": identity[1],
        }
    )


def _unidentified(
    port: str,
    values: Mapping[str, Value],  # noqa: ARG001  # clears a known port
    state: Mapping[str, Value],  # noqa: ARG001
) -> Step:
    """Clear a detached port's ``bd`` and ``be`` back to no device."""
    return Step(
        values={
            f"{port}_device_vid": NO_ID[0],
            f"{port}_device_pid": NO_ID[1],
            f"{port}_device_class": NONE,
            f"{port}_device_code": NONE,
        }
    )


#: Secret Features' ``0305 a2`` codes (doc "Secret Features"): the ``420d``
#: prompt, Feature Expert, Power Expert, Device Expert, Feature 1 (screen
#: switches) and Feature 2 (plugs), serviced in this order, one a second.
SECRET_PROMPT = 0
FEATURE_EXPERT = 1
POWER_EXPERT = 2
DEVICE_EXPERT = 3
FEATURE_SCREEN = 4
FEATURE_PLUGS = 5
SERVICE_ORDER = (
    SECRET_PROMPT,
    FEATURE_EXPERT,
    POWER_EXPERT,
    DEVICE_EXPERT,
    FEATURE_SCREEN,
    FEATURE_PLUGS,
)
#: Power Expert: 3 USB-C ports in a high-voltage class above 18 000 mV. The
#: class is 0 for 21 s after the active-port count changes, and while fewer
#: than 2 are active (``usbc_hv_class_update`` 0x000110f8).
POWER_PORTS, HIGH_VOLTAGE, HV_SETTLE = 3, 18000, 21.0
HV_SETTLED, POWER_COUNT = "_usbc_hv_settled", "_power_expert_ports"
#: A trigger inside its feature's cooldown is dropped, not queued.
COOLDOWNS = {DEVICE_EXPERT: 24 * 3600.0, FEATURE_SCREEN: 60.0, FEATURE_PLUGS: 300.0}
#: Feature 1: 20 main-tile focus events within 10 s, a sliding window.
SCREEN_SWITCHES, SCREEN_WINDOW = 20, 10.0
#: Feature 2: 10 attach rises of one USB-C port within 60 s of the first.
PLUGS, PLUG_WINDOW = 10, 60.0
#: Feature Expert: every 10th counted designation since boot.
DESIGNATIONS = 10
#: The counters, all RAM-only and carried in no frame.
SCREEN_COUNT, SCREEN_SERIAL = "_screen_switches", "_screen_switch_serial"
PROMPTS = "_secret_prompts"
DESIGNATION_COUNT = "_designations"
SERVICING = "_secret_servicing"


def _pending(code: int) -> str:
    """Return the hidden flag of a Secret Feature waiting to be sent."""
    return f"_secret_pending_{code}"


def _cooling(code: int) -> str:
    """Return the hidden flag of a Secret Feature inside its cooldown."""
    return f"_secret_cooling_{code}"


def _plug_count(port: str) -> str:
    """Return the hidden count of ``port``'s attach rises in its window."""
    return f"_{port}_plug_rises"


def _triggered(
    code: int, state: Mapping[str, Value]
) -> tuple[dict[str, Value], tuple[Timed, ...]]:
    """Return a Secret Feature trigger's values and events: pending, unless cooling.

    It starts the feature's cooldown, and the service if none is running.
    """
    if state.get(_cooling(code)):
        return {}, ()
    values: dict[str, Value] = {_pending(code): True}
    events = []
    if code in COOLDOWNS:
        values[_cooling(code)] = True
        events.append(
            Timed(COOLDOWNS[code], f"secret_cooldown_{code}", partial(_cooled, code))
        )
    if not state.get(SERVICING):
        values[SERVICING] = True
        events.append(Timed(0, "secret_service", _service))
    return values, tuple(events)


def _cooled(
    code: int,
    values: Mapping[str, Value],  # noqa: ARG001  # ends a known cooldown
    state: Mapping[str, Value],  # noqa: ARG001
) -> Step:
    """End a Secret Feature's cooldown."""
    return Step(unset=(_cooling(code),))


def _service(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Send one pending Secret Feature's ``0305``, then look again a second on.

    ``secret_features_tick`` services one a second while no overlay is shown;
    the emulator models no overlay. An idle service sends at once; after a send
    it stays busy a second, so a trigger in that second waits for the next.
    """
    waiting = [code for code in SERVICE_ORDER if state.get(_pending(code))]
    if not waiting:
        return Step(unset=(SERVICING,))
    code = waiting[0]
    fields = {"a2": container("a2", TYPE_UI, ui(1).build(code))}
    return Step(
        unset=(_pending(code),),
        frames=(timed_push(0x305, fields, state),),
        then=(Timed(1.0, "secret_service", _service),),
    )


def _trigger(
    code: int,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Trigger Secret Feature ``code``."""
    changes, events = _triggered(code, state)
    return Step(values=changes, then=events)


def _screen_switched(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Count a main-tile focus for 10 s; the 20th in the window triggers Feature 1.

    Each focus ages out on its own, so the count is the sliding window's (doc
    "Secret Features": the 20th-newest within 10 000 ms of the newest).
    """
    count = int(state.get(SCREEN_COUNT, 0)) + 1
    serial = int(state.get(SCREEN_SERIAL, 0))
    aged = Timed(SCREEN_WINDOW, f"screen_switch_{serial}", _screen_switch_aged)
    changes: dict[str, Value] = {SCREEN_COUNT: count, SCREEN_SERIAL: serial + 1}
    events: tuple[Timed, ...] = (aged,)
    if count >= SCREEN_SWITCHES:
        fired, more = _triggered(FEATURE_SCREEN, state)
        changes |= fired
        events += more
    return Step(values=changes, then=events)


def _screen_switch_aged(
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Let a main-tile focus fall out of the 10 s window."""
    return Step(values={SCREEN_COUNT: int(state[SCREEN_COUNT]) - 1})


def _plug_rise(
    port: str,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Count a USB-C port's attach rise; the 10th within 60 s triggers Feature 2.

    The window is fixed from the first rise and clears the count when it ends;
    the 10th clears it too (doc "Secret Features").
    """
    count = int(state.get(_plug_count(port), 0)) + 1
    window = f"{port}_plug_window"
    events: tuple[Timed, ...] = ()
    if count == 1:
        events = (Timed(PLUG_WINDOW, window, partial(_plug_window_ended, port)),)
    if count < PLUGS:
        return Step(values={_plug_count(port): count}, then=events)
    fired, more = _triggered(FEATURE_PLUGS, state)
    return Step(
        values={_plug_count(port): 0} | fired,
        then=(Timed(0, window, drop_queued), *more),
    )


def _plug_window_ended(
    port: str,
    values: Mapping[str, Value],  # noqa: ARG001  # clears a known count
    state: Mapping[str, Value],  # noqa: ARG001
) -> Step:
    """Clear a port's attach rises when its 60 s window ends."""
    return Step(values={_plug_count(port): 0})


def _designated(
    designations: int,
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Count designations; every 10th triggers Feature Expert (no cooldown)."""
    count = int(state.get(DESIGNATION_COUNT, 0)) + designations
    if count < DESIGNATIONS:
        return Step(values={DESIGNATION_COUNT: count})
    fired, events = _triggered(FEATURE_EXPERT, state)
    return Step(values={DESIGNATION_COUNT: count - DESIGNATIONS} | fired, then=events)


def _power_ports(state: Mapping[str, Value], *, settled: bool) -> int:
    """Return the USB-C ports Power Expert counts: high-voltage class, > 18 V.

    A port's class is non-zero once settled with 2 or more active when its peak
    is above 18 000 mV, which a voltage above 18 000 mV already implies; the PD
    codes that also raise it (record +0x18) aren't modeled.
    """
    active = [port for port in USBC_PORTS if state[f"{port}_output_status"]]
    if not settled or len(active) < 2:  # noqa: PLR2004  # the class needs 2 active
        return 0
    return sum(int(state[f"{port}_output_voltage"]) > HIGH_VOLTAGE for port in active)


def _power_check(
    settled: bool,  # noqa: FBT001  # bound by partial
    values: Mapping[str, Value],  # noqa: ARG001  # reads the whole state
    state: Mapping[str, Value],
) -> Step:
    """Recount Power Expert's ports; a rise to 3 or more triggers it (no cooldown)."""
    count = _power_ports(state, settled=settled)
    changes: dict[str, Value] = {HV_SETTLED: settled, POWER_COUNT: count}
    events: tuple[Timed, ...] = ()
    if count >= POWER_PORTS > int(state.get(POWER_COUNT, 0)):
        fired, events = _triggered(POWER_EXPERT, state)
        changes |= fired
    return Step(values=changes, then=events)


def _power_expert(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return Power Expert's recount.

    A change in the active USB-C port count zeroes the class for 21 s; a
    voltage change while settled recounts at once.
    """
    active = [f"{port}_output_status" for port in USBC_PORTS]
    if sum(bool(before[n]) for n in active) != sum(bool(after[n]) for n in active):
        return (
            Timed(0, "usbc_hv_reset", partial(_power_check, False)),  # noqa: FBT003
            Timed(HV_SETTLE, "usbc_hv_settle", partial(_power_check, True)),  # noqa: FBT003
        )
    voltages = [f"{port}_output_voltage" for port in USBC_PORTS]
    if after.get(HV_SETTLED) and any(before[n] != after[n] for n in voltages):
        return (Timed(0, "power_expert", partial(_power_check, True)),)  # noqa: FBT003
    return ()


def _counted_designations(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> int:
    """Return the designations a ``420c`` counts toward Feature Expert.

    One per set bit, in port order, but none made with 2 or more ports already
    designated (``port_priority_set``, doc "Secret Features"). The on-device
    mode menu and the re-apply on entering Port Priority aren't modeled.
    """
    already = sum(before[f"{p}_port_priority"] == DESIGNATED for p in USBC_PORTS)
    counted = 0
    for port in USBC_PORTS:
        if after[f"{port}_port_priority"] == DESIGNATED:
            if already < 2:  # noqa: PLR2004  # the doc's "2 or more already"
                counted += 1
            already += 1
    return counted


def _secret_features(
    before: Mapping[str, Value], after: Mapping[str, Value]
) -> tuple[Timed, ...]:
    """Return the Secret Feature inputs a change makes (doc "Secret Features").

    A nonzero ``420d``, a screen switch (``unit_event("switch_screen")``), a
    USB-C attach rise, a ``420c``'s designations, and all six ports turning
    active.
    """
    events = []
    if after.get(PROMPTS) != before.get(PROMPTS):
        events.append(Timed(0, "secret_prompt", partial(_trigger, SECRET_PROMPT)))
    if after.get(UNIT_EVENT) == b"switch_screen":
        events.append(Timed(0, "screen_switch", _screen_switched))
    for port in USBC_PORTS:
        status = f"{port}_output_status"
        if after[status] and not before[status]:
            events.append(Timed(0, f"{port}_plug_rise", partial(_plug_rise, port)))
    if after.get(COMMAND) == 0x20C:  # noqa: PLR2004  # 420c, the designations
        counted = _counted_designations(before, after)
        if counted:
            events.append(Timed(0, "designations", partial(_designated, counted)))
    active = [f"{port}_output_status" for port in ATTACH_PORTS]
    if all(after[name] for name in active) and not all(before[name] for name in active):
        events.append(Timed(0, "device_expert", partial(_trigger, DEVICE_EXPERT)))
    return tuple(events)


def _renegotiate(
    values: Mapping[str, Value],
    state: Mapping[str, Value],  # noqa: ARG001  # keeps only what was set
) -> Step:
    """Zero every port block, then give each back what it had when the window ends."""
    prior = {name: values[name] for name in RENEGOTIATED if name in values}
    return Step(
        values={**dict.fromkeys(RENEGOTIATED, 0), RENEGOTIATING: True},
        then=(Timed(RENEGOTIATION, "renegotiation", partial(_renegotiated, prior)),),
    )


def _renegotiated(
    prior: Mapping[str, Value],
    values: Mapping[str, Value],
    state: Mapping[str, Value],  # noqa: ARG001  # restores only what was set
) -> Step:
    """Restore each zeroed field: its set value back, else its recording.

    A field set during the window keeps that value.
    """
    zeroed = [name for name in RENEGOTIATED if values.get(name) == 0]
    return Step(
        values={name: prior[name] for name in zeroed if name in prior},
        unset=(RENEGOTIATING, *(name for name in zeroed if name not in prior)),
    )


def _designated_ports_change(
    request: ParameterDict,  # noqa: ARG001  # a change watcher: state only
    before: Mapping[str, Value],
    after: Mapping[str, Value],
) -> ParameterDict | None:
    """``030f``: the designated-port mask, when it changed."""
    mask = _designated_ports(after)
    if _designated_ports(before) == mask:
        return None
    return ParameterDict({"a3": container("a3", TYPE_UI, ui(1).build(mask))})


def _set_button_rotate_direction(request: ParameterDict) -> dict[str, Value] | None:
    """Return the knob direction if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("button_rotate_direction", SWITCH)})


def _set_time_format(request: ParameterDict) -> dict[str, Value] | None:
    """Return the clock time format if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("time_format", SWITCH)})


def _set_custom_charge_mode_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Accept the custom charge-mode switch (0 or 1); 0's exit is a side effect."""
    decode_field(request, "a2", SWITCH)
    return {}


def _set_screensaver_window(request: ParameterDict) -> dict[str, Value] | None:
    """Return the screensaver active-hours window."""
    parsed = decode_field(request, "a2", Screensaver)
    return {name: parsed[name] for name in SCREENSAVER_FIELDS}


def _set_compatibility_switch(request: ParameterDict) -> dict[str, Value] | None:
    """Return the compatibility switch if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("compatibility_switch", SWITCH)})


def _set_equipment_identification_switch(
    request: ParameterDict,
) -> dict[str, Value] | None:
    """Return the equipment-identification switch if ``a2`` is 0 or 1."""
    return decode_request(request, {"a2": ("equipment_identification_switch", SWITCH)})


def _set_easter_egg(request: ParameterDict) -> dict[str, Value] | None:
    """Accept an ``a2`` typed ``02`` with a value; refuse anything else (0x00043938)."""
    field = request.get("a2")
    if field is None or field.get("type") != TYPE_SILE or not field.get("value"):
        return None
    return {}


def _secret_prompt(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Count a ``420d`` whose first value byte is nonzero: Secret Feature 0.

    ``cmd20d_secret_features`` (0x00043938) stores only that byte (0x0001475c);
    its ``0305`` waits for the service tick, first in its order (doc "Secret
    Features"; no capture shows it beside the ack).
    """
    if not decode_field(request, "a2", ui(1)):
        return {}
    return {PROMPTS: int(state.get(PROMPTS, 0)) + 1}


def _set_time_display(request: ParameterDict) -> dict[str, Value] | None:
    """Return Time Display (``4a02 a6``) for ``a2`` 0 or 1; other values change nothing.

    The handler (0x0004ff84) refuses only a request without ``a2``.
    """
    if "a2" not in request:
        return None
    switch = decode_field(request, "a2", ui(1))
    return {"time_display": switch} if switch in {0, 1} else {}


def _time_display_redraw(
    request: ParameterDict, state: Mapping[str, Value]
) -> dict[str, Value]:
    """Redraw on a change of Time Display, and only while the screen is 5.

    ``time_display_set``: the custom photo with the time shown or hidden (doc
    ``4222``); on screen 1 it sets the flag and draws nothing.
    """
    switch = _set_time_display(request)
    if not switch or switch["time_display"] == state["time_display"]:
        return {}
    if state["screen_saver_theme"] != CUSTOM_THEME:
        return {}
    return _draw_asked(state)


def _get_usbc_status(
    request: ParameterDict,  # noqa: ARG001  # the handler reads no request field
) -> dict[str, Value] | None:
    """Accept: ``4218`` reads the USB-C power budgets and sets nothing."""
    return {}


_IDLE_OR_ONE = (140, 100, 100, 100)
#: Each USB-C port's offered power-sharing budget in W, pd1-pd4, by the active
#: pd1-pd4 bitmap: (no USB-A active, a USB-A active). Baseline (ungrouped)
#: profile of 0x000114b0 (prime-a2345-a91b2.md "4218 field5 power-sharing budget").
POWER_SHARING_BUDGET: dict[int, tuple[tuple[int, ...], tuple[int, ...]]] = {
    **dict.fromkeys((0, 1, 2, 4, 8), (_IDLE_OR_ONE, _IDLE_OR_ONE)),
    0x3: ((140, 100, 45, 45), (100, 100, 30, 30)),
    0x5: ((140, 65, 100, 45), (100, 90, 100, 30)),
    0x6: ((140, 100, 100, 45), (100, 100, 100, 30)),
    0x7: ((140, 65, 45, 30), (100, 90, 30, 30)),
    0x9: ((140, 65, 65, 100), (100, 90, 90, 100)),
    0xA: ((140, 100, 100, 100), (100, 100, 90, 100)),
    0xB: ((140, 65, 30, 45), (100, 90, 45, 30)),
    0xC: ((140, 100, 100, 100), (100, 100, 100, 100)),
    0xD: ((140, 100, 65, 45), (100, 65, 90, 30)),
    0xE: ((100, 100, 100, 45), (70, 100, 90, 30)),
    0xF: ((100, 100, 30, 20), (70, 65, 45, 45)),
}
#: ``4a18 a2``: pd1-pd4's budgets, then their ``0303`` output statuses.
UsbcStatus = Struct(
    "power_sharing_budget" / Array(4, ui(1)), "output_status" / Array(4, ui(1))
)


def _usbc_status_read(
    request: ParameterDict,  # noqa: ARG001  # the reply carries no request field
    state: Mapping[str, Value],
) -> ParameterDict:
    """``4a18``: each pd port's power budget, then its ``0303`` output status.

    ``a2`` is 8 raw bytes with no type byte (0x0001daf0).
    """
    statuses = [state[f"pd{i + 1}_output_status"] for i in range(4)]
    bitmap = sum(1 << i for i, status in enumerate(statuses) if status)
    usba = any(state[f"usb{i}_output_status"] for i in (1, 2))
    value = UsbcStatus.build(
        {
            "power_sharing_budget": POWER_SHARING_BUDGET[bitmap][usba],
            "output_status": statuses,
        }
    )
    return ParameterDict(
        {"a2": ParameterContainer(key=b"\xa2", type=None, value=value)}
    )


#: The MCU's fid 0x0f table, 0x200-0x223, and the ops below 0x200 it handles
#: inline (prime-a2345-a91b2.md "Commands"): every real handler acks ``00``
#: where nothing recorded or modeled answers, an op past the table too. No
#: reply: the no-op stubs, ``4212`` (the theme schedule's handler sends none)
#: and the inline BLE OTA ops ``4063``-``4067``. Any other op below 0x200 is
#: dropped.
COMMAND_TABLE: dict[int, int | None] = {
    **dict.fromkeys([0x059, 0x05D, 0x072, *range(0x200, 0x224)], STATUS_ACCEPTED),
    **dict.fromkeys([*range(0x063, 0x068), 0x212, 0x215, 0x216, 0x217]),
    **dict.fromkeys(range(0x219, 0x220)),
}


class A2345Layout(TableLayout):
    """A2345's own command/telemetry code; satisfies ``LayoutProtocol``."""

    COMMANDS = MappingProxyType(
        {
            0x203: set_screen_timeout,
            0x204: _set_screen_brightness,
            0x205: _set_screensaver,
            0x206: _set_charging_mode,
            0x207: _set_dc_relay_switch,
            0x208: ac_port_timed_task(PORT_BY_SELECT),
            0x209: ac_port_countdown_task(PORT_BY_SELECT, disarm_clears=DISARM_CLEARS),
            0x20A: _set_region_owner,
            0x20C: _set_port_priority,
            0x20D: _set_easter_egg,
            0x20E: _set_button_rotate_direction,
            0x20F: _set_holiday_screensaver,
            0x212: _holiday_schedule_taken,
            0x210: _set_time_format,
            0x211: _set_custom_charge_mode_switch,
            0x213: _set_screensaver_window,
            0x214: _set_compatibility_switch,
            0x218: _get_usbc_status,
            0x220: _prepare_upload,
            0x221: _upload_chunk,
            0x222: _set_time_display,
            0x223: _set_equipment_identification_switch,
        }
    )
    ECHOES = MappingProxyType(PORT_ECHOES)
    #: ``4212``'s and ``4222``'s handlers send no reply.
    SILENT = frozenset({0x212, 0x222})
    #: The ``0308`` before ``4208``'s ack, ``4311`` 1 before the last ``4a21``.
    LEADING = MappingProxyType({**TIMED_TASK_LEADS, 0x221: frozenset({0x311})})
    #: ``4311`` carries no clock (doc "``4311`` theme image state").
    UNCLOCKED = frozenset({0x311})
    READ_REPLIES = MappingProxyType(
        {
            0x218: _usbc_status_read,
            0x220: _upload_prepared,
            0x221: _upload_answer,
        }
    )
    SIDE_EFFECTS = MappingProxyType(
        {
            0x205: _theme_staging,
            0x206: _leave_port_priority,
            0x208: timed_task_conflict(PORT_BY_SELECT),
            0x20A: _owner_change,
            0x20D: _secret_prompt,
            0x211: _custom_mode_switch_off,
            0x212: _holiday_schedule,
            0x221: _upload_step,
            0x222: _time_display_redraw,
        }
    )
    #: The MCU's change watchers, by the commands that can trip them (doc "MCU
    #: pushes"): ``0302`` a switch, ``0307`` a countdown arming or disarming,
    #: ``0308`` a collision-disabled half, ``030f`` the designated ports and
    #: ``0311`` a theme image's state; ``030d`` and the Secret Features'
    #: ``0305`` are timed (``timed``).
    FOLLOW_UPS = MappingProxyType(
        {
            0x205: ((0x311, _theme_selected),),
            0x206: ((0x30F, _designated_ports_change),),
            0x207: ((0x302, _switch_change),),
            0x208: ((0x308, timed_task_push(PORT_BY_SELECT)),),
            0x209: ((0x307, countdown_push(PORT_BY_SELECT, remaining_first=False)),),
            0x20C: ((0x30F, _designated_ports_change),),
            0x221: ((0x311, _upload_done),),
        }
    )
    FIELDS = MappingProxyType(
        {
            MSGTYPE_303: {
                "a8": IDENTITY_SPECS["ids"],
                "a9": IDENTITY_SPECS["identities"],
            },
            MSGTYPE_A00: SNAPSHOT_FIELDS,
            MSGTYPE_A02: SCREENSAVER_READ_FIELDS,
        }
    )
    GROUPS = MappingProxyType(
        {
            MSGTYPE_303: (PORT_GROUPS[MSGTYPE_303],),
            MSGTYPE_A00: (PORT_GROUPS[MSGTYPE_A00], SCHEDULES),
        }
    )

    def names(self, msgtype: int) -> frozenset[str]:
        """Return the names a frame reads; ``4a02`` adds the holiday record's."""
        names = super().names(msgtype)
        if msgtype == MSGTYPE_A02:
            return names | {*HOLIDAY_ERASED, STORED_COUNTRY}
        return names

    def finish(
        self, msgtype: int, params: ParameterDict, values: Mapping[str, Value]
    ) -> None:
        """``4a02`` with the holiday theme showing: ``a4`` its URL, then ``a5 01``.

        doc "``4a02`` screensaver reply": ``a5 02 01 01``, type ``01``, value 1.
        """
        if msgtype != MSGTYPE_A02:
            return
        switch = decode_field(params, "a2", ScreensaverRead).holiday_screensaver_switch
        if not _holiday_showing(switch, values):
            return
        url = stored_bytes(values, HOLIDAY_URL)
        fields = dict(params)
        params.clear()
        for tag, field in fields.items():
            if tag == "a4":
                params["a4"] = container("a4", TYPE_BIN, url)
                params["a5"] = container("a5", TYPE_UI, b"\x01")
            else:
                params[tag] = field

    def sends(self, msgtype: int, values: Mapping[str, Value]) -> bool:
        """Whether a push reaches the BLE link: with WiFi up, most go to MQTT."""
        return reaches_ble(PUSH_ROUTES, msgtype, values)

    def mqtt_routed(
        self, msgtype: int, payload: bytes, values: Mapping[str, Value]
    ) -> tuple[tuple[int, bytes], ...]:
        """Return a push's MQTT copies in the unit's link state (doc "Push routing")."""
        return mqtt_routed(PUSH_ROUTES, msgtype, payload, values)

    def timed(
        self, before: Mapping[str, Value], after: Mapping[str, Value]
    ) -> tuple[Timed, ...]:
        """Return the events a change starts.

        Those are countdowns, timed tasks, screen reports and a mode change's. A
        mode change sends ``030d`` once it settles and zeroes the ports while
        they renegotiate. The zero is documented for ``4206`` (doc "Commands");
        it is assumed for ``420c`` entry too, the same mode-apply path (doc "MCU
        pushes" ``030f``).
        """
        events = (
            *countdown_timers(
                PORT_BY_SELECT,
                before,
                after,
                disarm_clears=DISARM_CLEARS,
                remaining_first=False,
            ),
            *schedule_timers(PORT_BY_SELECT, before, after),
            *_screen_reports(before, after),
            *_attach_events(before, after),
            *_secret_features(before, after),
            *_power_expert(before, after),
            *_theme_fetch(before, after),
            *_holiday_fetch(before, after),
            *_draw_events(before, after),
            *wifi_events(before, after, PERIODICS),
        )
        if before["charging_mode"] == after["charging_mode"]:
            return events
        mode = int(after["charging_mode"])
        if mode < CUSTOM_MODE:
            remember = partial(_remember_mode, mode)
            events = (*events, Timed(0, "last_mode", remember))
        report = Timed(MODE_SETTLE, "charging_mode_report", _mode_report)
        if after.get(RENEGOTIATING):
            return (*events, report)
        return (*events, report, Timed(0, "renegotiation", _renegotiate))


#: ``esp_op26_set_clock`` (0x00047f70) takes a UTC from 2024-04-30 16:00:00 on,
#: and sets the clock one second past it (doc fid 0x10 op ``0x26``).
CLOCK_FLOOR = 0x66311580
CLOCK_LEAD = 1

PROFILE = Profile(
    serial="AQLQJB0000000001",
    outer=Outer.ENCRYPTED,
    path=Path.ECDH,
    module_build=ModuleBuild.V0_2_9_7,
    auth_mode=AuthMode.CONFIRM,
    advert=Advert(
        local_name=None,
        version_code=0x01,
        bind_type=0x00,
        product_type=bytes.fromhex("b402"),
        sku=b"QJB",
        capability=0x04,
        prime_name=True,
    ),
    data=("a2345.json",),
    replies={
        0x200: (0xA00,),
        0x202: (MSGTYPE_A02,),
        0x20A: (0xA0A,),
        0x20B: (0xA0B,),
    },
    pushes=(0x303,),
    device_version="v2.1.1.6",
    version_names=("A2345", "A2345_mcu", "A2345_esp32"),
    layout=A2345Layout(),
    burst=REALTIME_BURST,
    live=LiveSource("a2345_live.json", REALTIME_BURST.period),
    known_commands=COMMAND_TABLE,
    past_table_status=STATUS_ACCEPTED,
    factory_reset=0x201,
    clock_floor=CLOCK_FLOOR,
    clock_lead=CLOCK_LEAD,
)
register(Product.A2345, PROFILE)


class A2345(ProductDevice):
    """Prime Charger 250W: encrypted outer, ECDH, owner confirmation by button.

    Its module build (v0.2.9.7) also accepts the plain outer.
    """

    PRODUCT = Product.A2345

    def set_wifi(self, up: bool) -> None:  # noqa: FBT001  # a switch
        """Bring the unit's WiFi up or down (link-state bit 1, doc "Push routing").

        With it up most pushes go to MQTT, not the BLE link; coming up sends
        ``0301``.
        """
        self.unit_event("wifi_up" if up else "wifi_down", **{WIFI_UP: up})

    def plug(self, port: int, vid: int, pid: int, ext: int | None = None) -> None:
        """Plug a device into USB-C ``port`` (1-4): its attach flag sets.

        The port's PD controller then reads the device's USB-PD identity,
        ``vid`` and ``pid``, into ``bd`` and ``be`` (doc ``bd`` row). ``ext`` is
        the extended identity word a revision 4 or later controller reads (no
        frame carries it); it picks an Apple PID's variant row in ``be``.
        """
        name = f"pd{port}"
        self.unit_event(
            f"plug_{name}",
            **{
                _plugged(name, "vid"): vid,
                _plugged(name, "pid"): pid,
                _plugged(name, "ext"): NONE if ext is None else ext,
            },
        )
        self._unit_values({f"{name}_output_status": 1}, pin=False)

    def unplug(self, port: int) -> None:
        """Unplug USB-C ``port`` (1-4): its attach flag clears and it draws nothing."""
        name = f"pd{port}"
        self._unit_values(
            {f"{name}_output_status": 0, f"{name}_output_power": 0}, pin=False
        )
