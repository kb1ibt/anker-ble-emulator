# Copyright (c) 2026 Shawn Stricker
"""The comms module: negotiation, link state, authorization and the MCU relay."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag

from .crypto import STATIC_GCM, DeviceKeyPair, session_cbc, session_gcm
from .frame import FLAG_ENCRYPTED, RESPONSE, Frame, FrameError, Reassembler, fragment
from .tlv import FieldError, decode_fields, response


if TYPE_CHECKING:
    from collections.abc import Callable

    from .clock import Clock
    from .crypto import Cipher
    from .mcu import McuFrame, McuScript

_LOGGER = logging.getLogger(__name__)

CHANNEL_NEGOTIATION = 0x01
CHANNEL_SESSION = 0x0F
CHANNEL_APP = 0x11
#: Pattern of frames the module composes on its own: the grant, MCU relays.
PATTERN_GRANT = bytes.fromhex("030101")
PATTERN_RELAY = bytes.fromhex("03010f")
#: App-channel opcodes below this are the module's own; the rest go to the MCU.
MCU_OPCODE_MIN = 0x40

STATUS_OK = 0x00
STATUS_FAIL = 0x01
STATUS_PARAMETER = 0x04
STATUS_NEED_AUTHENTICATION = 0x09

#: ``0803 a1``: base capability bits, ``0x02`` = AES.
BASE_CAPABILITY = 0x02
#: ``0803 a4``: a build constant.
STAGE2_A4 = 0x01
#: ``0801 a1`` and ``0829 a1``: fixed type bytes.
CONNECT_TYPE = 0x01
DEVICE_INFO_TYPE = 0x03
#: ``0005 a5`` bits: legacy AES, and the two equivalent ECDH bits.
METHOD_LEGACY = 0x02
METHOD_ECDH = 0x44
METHOD_ANY = METHOD_LEGACY | METHOD_ECDH
MAX_TOKEN_LEN = 63

#: An unauthorized link drops this long after connect.
UNAUTHORIZED_LIMIT = 30.0
#: Extra seconds after a confirmation window before the drop.
CONFIRMATION_GRACE = 5.0
#: The authorize timer's period; drops fire on its ticks.
TIMER_PERIOD = 10.0

TAG_A1, TAG_A2, TAG_A3, TAG_A4, TAG_A5 = 0xA1, 0xA2, 0xA3, 0xA4, 0xA5


class AuthMode(IntEnum):
    """The policy byte the MCU provisions and the module enforces."""

    OPEN = 0
    TIME_LIMITED = 1
    CONFIRM = 2


class CapabilityMode(IntEnum):
    """What ``0005`` chose: no encryption, legacy AES, or ECDH."""

    NONE = 0
    LEGACY = 1
    ECDH = 4


@dataclass(frozen=True)
class ModuleConfig:
    """What the module reports and enforces.

    Attributes:
        mac: The 6-byte BLE MAC (``0829 a5``).
        serial: The provisioned serial (``0829 a4``), or None.
        auth_mode: The policy byte (``0803 a5``).
        enforce: Module v0.3.3.0 policy: refuse a cleartext connect and a
            non-ECDH method when ``auth_mode`` isn't open, gate ``0027``.
        advanced_capability: ``0803 a3`` (``0x44`` ECDH-capable).
        reports_auth_method: Whether ``0803`` carries ``a5``.
        auth_timeout: Seconds of a confirmation window (``0827 09 a1``).
        fragment_cap: The largest frame the link carries (ATT MTU - 3).
        chip: ``0829 a2``.
        lib_version: ``0829 a3``.

    """

    mac: bytes
    serial: bytes | None
    auth_mode: AuthMode = AuthMode.CONFIRM
    enforce: bool = True
    advanced_capability: int = 0x44
    reports_auth_method: bool = True
    auth_timeout: int = 30
    fragment_cap: int = 253
    chip: bytes = b"ESP32"
    lib_version: bytes = b"0.0.0.3"


@dataclass
class Output:
    """Frames to notify, in order, and whether the module drops the link."""

    frames: list[bytes] = field(default_factory=list)
    disconnect: bool = False


@dataclass
class _Link:
    """State of one connection."""

    connected_at: float
    reassembler: Reassembler = field(default_factory=Reassembler)
    gcm_connect: bool = False
    capability_mode: CapabilityMode = CapabilityMode.NONE
    session: Cipher | None = None
    ecdh_done: bool = False
    authorized: bool = False
    auth_started_at: float | None = None
    pending_token: bytes | None = None


@dataclass(frozen=True)
class _Request:
    """One opened negotiation request on a link."""

    link: _Link
    frame: Frame
    fields: dict[int, bytes]


@dataclass(frozen=True)
class _Reply:
    status: int
    fields: tuple[tuple[int, bytes], ...] = ()
    disconnect: bool = False


class Module:
    """The device side of one BLE peripheral: frames in, frames out."""

    def __init__(
        self,
        config: ModuleConfig,
        mcu: McuScript,
        clock: Clock,
        key_pair_factory: Callable[[], DeviceKeyPair] = DeviceKeyPair,
    ) -> None:
        """Wire the module to its MCU script and clock.

        Args:
            config: What the module reports and enforces.
            mcu: The MCU's recorded replies and pushes.
            clock: Time for the authorize timer.
            key_pair_factory: Makes the device key pair of each key exchange.

        """
        self.config = config
        self.mcu = mcu
        self.clock = clock
        self.enrolled: set[bytes] = set()
        self._key_pair_factory = key_pair_factory
        self._link: _Link | None = None

    @property
    def connected(self) -> bool:
        """Whether a link is up."""
        return self._link is not None

    @property
    def authorized(self) -> bool:
        """Whether the current link may reach the MCU."""
        return self._link is not None and self._link.authorized

    def connect(self) -> None:
        """Bring a link up with fresh state."""
        self._link = _Link(connected_at=self.clock.now())

    def disconnect(self) -> None:
        """Drop the link."""
        self._link = None

    def write(self, data: bytes) -> Output:
        """Handle one write from the client.

        Args:
            data: The bytes written to the command characteristic.

        Returns:
            The frames to notify and whether the link drops.

        Raises:
            RuntimeError: If no link is up.

        """
        if self._link is None:
            msg = "write without a connected link"
            raise RuntimeError(msg)
        link = self._link
        try:
            whole = link.reassembler.feed(Frame.decode(data))
        except FrameError:
            _LOGGER.warning("Dropped malformed write %s", data.hex())
            return Output()
        if whole is None:
            return Output()
        if whole.channel == CHANNEL_NEGOTIATION:
            return self._negotiate(link, whole)
        if whole.channel in {CHANNEL_SESSION, CHANNEL_APP}:
            return self._session(link, whole)
        return Output()

    def press_button(self) -> Output:
        """Press the side button: grant a pending confirmation.

        Returns:
            The ``4827 00`` grant on ``030101``, or nothing if none is pending.

        """
        link = self._link
        if link is None or link.pending_token is None or link.session is None:
            return Output()
        self.enrolled.add(link.pending_token)
        link.pending_token = None
        link.authorized = True
        grant = Frame(
            PATTERN_GRANT,
            FLAG_ENCRYPTED | RESPONSE | 0x027,
            link.session.encrypt(response(STATUS_OK)),
        )
        return Output(self._encode(grant))

    def push(self, msgtype: int) -> Output:
        """Send the MCU's scripted push of ``msgtype`` if the link is authorized.

        Args:
            msgtype: The push's 12-bit message type.

        """
        link = self._link
        if link is None or not link.authorized:
            return Output()
        return self._relay(link, [self.mcu.push(msgtype)])

    def check_timers(self) -> Output:
        """Run the authorize timer once: drop an unauthorized link past its limit."""
        link = self._link
        if link is None or link.authorized:
            return Output()
        if link.auth_started_at is not None:
            limit = link.auth_started_at + self.config.auth_timeout + CONFIRMATION_GRACE
        else:
            limit = link.connected_at + UNAUTHORIZED_LIMIT
        return Output(disconnect=self.clock.now() >= limit)

    def _encode(self, frame: Frame) -> list[bytes]:
        return [part.encode() for part in fragment(frame, self.config.fragment_cap)]

    def _negotiate(self, link: _Link, frame: Frame) -> Output:
        cipher: Cipher | None = None
        plaintext = frame.payload
        if frame.encrypted:
            cipher = link.session or STATIC_GCM
            try:
                plaintext = cipher.decrypt(frame.payload)
            except (InvalidTag, ValueError):
                _LOGGER.warning("Dropped undecryptable %04x", frame.cmd)
                return Output()
        try:
            request = _Request(link, frame, decode_fields(plaintext))
        except FieldError:
            _LOGGER.warning("Dropped %04x with malformed fields", frame.cmd)
            return Output()
        result = self._dispatch(request)
        if isinstance(result, Output):
            return result
        payload = response(result.status, result.fields)
        reply = Frame(
            frame.pattern,
            frame.cmd | RESPONSE,
            cipher.encrypt(payload) if cipher is not None else payload,
        )
        return Output(self._encode(reply), disconnect=result.disconnect)

    def _dispatch(self, request: _Request) -> _Reply | Output:
        """Run the module's handler for a negotiation opcode; none for others."""
        handlers: dict[int, Callable[[_Request], _Reply | Output]] = {
            0x001: self._connect,
            0x003: self._capabilities,
            0x029: lambda _request: self._device_info(),
            0x005: self._set_capabilities,
            0x021: self._public_key,
            0x022: self._clock,
            0x027: self._authenticate,
        }
        handler = handlers.get(request.frame.msgtype)
        return Output() if handler is None else handler(request)

    def _connect(self, request: _Request) -> _Reply | Output:
        encrypted = request.frame.encrypted
        if (
            not encrypted
            and self.config.enforce
            and self.config.auth_mode != AuthMode.OPEN
        ):
            return Output(disconnect=True)
        request.link.gcm_connect = encrypted
        request.link.capability_mode = CapabilityMode.NONE
        return _Reply(STATUS_OK, ((TAG_A1, bytes([CONNECT_TYPE])),))

    def _capabilities(self, request: _Request) -> _Reply:
        fields = request.fields
        if TAG_A3 not in fields or TAG_A4 not in fields:
            return _Reply(STATUS_PARAMETER)
        mtu = min(int.from_bytes(fields[TAG_A4], "little"), self.config.fragment_cap)
        reply = [
            (TAG_A1, bytes([BASE_CAPABILITY])),
            (TAG_A2, mtu.to_bytes(2, "little")),
            (TAG_A3, bytes([self.config.advanced_capability])),
            (TAG_A4, bytes([STAGE2_A4])),
        ]
        if self.config.reports_auth_method:
            reply.append((TAG_A5, bytes([self.config.auth_mode])))
        request.link.capability_mode = CapabilityMode.LEGACY
        return _Reply(STATUS_OK, tuple(reply))

    def _device_info(self) -> _Reply:
        reply = [
            (TAG_A1, bytes([DEVICE_INFO_TYPE])),
            (TAG_A2, self.config.chip),
            (TAG_A3, self.config.lib_version),
        ]
        if self.config.serial is not None:
            reply.append((TAG_A4, self.config.serial))
        reply.append((TAG_A5, self.config.mac))
        return _Reply(STATUS_OK, tuple(reply))

    def _set_capabilities(self, request: _Request) -> _Reply:
        link = request.link
        method = request.fields.get(TAG_A5, b"\x00")[0]
        if method == 0:
            link.capability_mode = CapabilityMode.NONE
            return _Reply(STATUS_OK)
        if not method & METHOD_ANY:
            return _Reply(STATUS_FAIL)
        if (
            self.config.enforce
            and self.config.auth_mode != AuthMode.OPEN
            and not method & METHOD_ECDH
        ):
            return _Reply(STATUS_FAIL)
        link.capability_mode = (
            CapabilityMode.ECDH if method & METHOD_ECDH else CapabilityMode.LEGACY
        )
        return _Reply(STATUS_OK)

    def _public_key(self, request: _Request) -> _Reply:
        link = request.link
        if link.capability_mode != CapabilityMode.ECDH:
            return _Reply(STATUS_OK)
        if TAG_A1 not in request.fields:
            return _Reply(STATUS_PARAMETER)
        key_pair = self._key_pair_factory()
        try:
            secret = key_pair.shared_secret(request.fields[TAG_A1])
        except ValueError:
            return _Reply(STATUS_FAIL)
        link.session = session_gcm(secret) if link.gcm_connect else session_cbc(secret)
        link.ecdh_done = True
        if not link.gcm_connect:
            link.authorized = True
        return _Reply(STATUS_OK, ((TAG_A1, key_pair.public_point),))

    def _clock(self, request: _Request) -> _Reply | Output:
        if request.link.capability_mode != CapabilityMode.ECDH:
            return Output()
        if not request.frame.encrypted:
            return _Reply(STATUS_FAIL)
        if TAG_A1 not in request.fields or TAG_A3 not in request.fields:
            return _Reply(STATUS_PARAMETER)
        return _Reply(STATUS_OK)

    def _authenticate(self, request: _Request) -> _Reply:
        link = request.link
        if self.config.enforce and not (link.ecdh_done and request.frame.encrypted):
            return _Reply(STATUS_FAIL)
        token = request.fields.get(TAG_A2)
        if token is None or len(token) > MAX_TOKEN_LEN:
            return _Reply(STATUS_FAIL)
        if self.config.auth_mode == AuthMode.OPEN:
            return _Reply(STATUS_OK, disconnect=True)
        if self.config.auth_mode == AuthMode.TIME_LIMITED or token in self.enrolled:
            link.authorized = True
            return _Reply(STATUS_OK)
        link.pending_token = token
        link.auth_started_at = self.clock.now()
        return _Reply(
            STATUS_NEED_AUTHENTICATION,
            ((TAG_A1, self.config.auth_timeout.to_bytes(2, "little")),),
        )

    def _session(self, link: _Link, frame: Frame) -> Output:
        if frame.msgtype & ~RESPONSE < MCU_OPCODE_MIN:
            return Output()
        if not link.authorized or link.session is None:
            return Output()
        if frame.encrypted:
            try:
                link.session.decrypt(frame.payload)
            except (InvalidTag, ValueError):
                _LOGGER.warning("Dropped undecryptable %04x", frame.cmd)
                return Output()
        return self._relay(link, self.mcu.respond(frame.msgtype))

    def _relay(self, link: _Link, frames: list[McuFrame]) -> Output:
        """Encrypt MCU frames for the session and send them on ``03010f``."""
        session = link.session
        if session is None:
            return Output()
        out: list[bytes] = []
        for mcu_frame in frames:
            relayed = Frame(
                PATTERN_RELAY,
                FLAG_ENCRYPTED | mcu_frame.msgtype,
                session.encrypt(mcu_frame.cleartext),
            )
            out.extend(self._encode(relayed))
        return Output(out)
