# Copyright (c) 2026 Shawn Stricker
"""The comms module: negotiation, link state, authorization and the MCU relay."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag

from .crypto import STATIC_GCM, DeviceKeyPair, session_cbc, session_gcm
from .frame import (
    CHANNEL_APP,
    CHANNEL_NEGOTIATION,
    CHANNEL_SESSION,
    COMPOSER_SEND,
    RESPONSE,
    FrameError,
    Reassembler,
    decode,
    encode,
    fragment,
    make_frame,
    reply_frame,
)
from .messages import (
    AUTH_PENDING_REPLY,
    BIND_REPLY,
    CAPABILITY_REPLY,
    CONNECT_REPLY,
    DEVICE_INFO_REPLY,
    PUBLIC_KEY_REPLY,
    ROUTE_BLE,
    ROUTE_HTTPS_LOG,
    ROUTE_LAYOUT,
    ROUTE_MCU,
    STATUS_REPLY,
    VERSION_REPLY,
    parse_request,
)
from .tlv import FieldError, decode_fields


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from construct import Container

    from .clock import Clock
    from .crypto import Cipher
    from .frame import Frame
    from .layouts import Value
    from .mcu import McuScript
    from .messages import Fields

_LOGGER = logging.getLogger(__name__)

#: App-channel opcodes below this are the module's own; the rest go to the MCU.
MCU_OPCODE_MIN = 0x40
#: The arm-grant ``4827``.
MSGTYPE_GRANT = RESPONSE | 0x027
#: ``0030``: the module's version read.
MSGTYPE_VERSIONS = 0x030

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
class Versions:
    """What ``0830`` reports, ``a1`` to ``a5``.

    Attributes:
        module: The module firmware (``v0.3.3.0``).
        device: The device MCU firmware.
        model: The model, or its OTA type on the C Gen 2 line (``A1783_low``).
        mcu: The MCU component name.
        esp32: The module component name.

    """

    module: bytes
    device: bytes
    model: bytes
    mcu: bytes
    esp32: bytes


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
        session_replies: Recorded cleartext replies to the module's own session
            ops (opcodes below ``0x40``), by request msgtype.
        versions: What ``0830`` reports; None leaves ``0030`` unanswered.

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
    session_replies: Mapping[int, bytes] = field(default_factory=dict)
    versions: Versions | None = None


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
    """One opened negotiation request on a link: its frame and typed fields."""

    link: _Link
    frame: Frame
    fields: Fields


@dataclass(frozen=True)
class _Reply:
    """A reply payload built from its message layout."""

    payload: bytes
    disconnect: bool = False


def _status(status: int, *, disconnect: bool = False) -> _Reply:
    return _Reply(STATUS_REPLY.build({"status": status}), disconnect)


def _request_route(plaintext: bytes) -> Container[Any] | None:
    """Return a session request's ``a1`` route; None if it has none."""
    try:
        route = decode_fields(plaintext).get(0xA1)
    except FieldError:
        return None
    if route is None or len(route) != ROUTE_LAYOUT.sizeof():
        return None
    return ROUTE_LAYOUT.parse(route)


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
        #: Telemetry values set by name, by the msgtype of the frames they go into.
        self.values: dict[int, dict[str, Value]] = {}
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
            whole = link.reassembler.feed(decode(data))
        except FrameError:
            _LOGGER.warning("Dropped malformed write %s", data.hex())
            return Output()
        if whole is None:
            return Output()
        if whole.pattern.channel == CHANNEL_NEGOTIATION:
            return self._negotiate(link, whole)
        if whole.pattern.channel in {CHANNEL_SESSION, CHANNEL_APP}:
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
        grant = make_frame(
            COMPOSER_SEND,
            CHANNEL_NEGOTIATION,
            MSGTYPE_GRANT,
            link.session.encrypt(STATUS_REPLY.build({"status": STATUS_OK})),
            encrypted=True,
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
        return self._relay(link, [self.mcu.push(msgtype, values=self.values)])

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
        return [encode(part) for part in fragment(frame, self.config.fragment_cap)]

    def _negotiate(self, link: _Link, frame: Frame) -> Output:
        cipher: Cipher | None = None
        plaintext = frame.payload
        if frame.cmd.encrypted:
            cipher = link.session or STATIC_GCM
            try:
                plaintext = cipher.decrypt(frame.payload)
            except (InvalidTag, ValueError):
                _LOGGER.warning("Dropped undecryptable %03x", frame.cmd.msgtype)
                return Output()
        try:
            fields = parse_request(frame.cmd.msgtype, plaintext)
        except FieldError:
            _LOGGER.warning("Dropped %03x with malformed fields", frame.cmd.msgtype)
            return Output()
        result = self._dispatch(_Request(link, frame, fields))
        if isinstance(result, Output):
            return result
        payload = cipher.encrypt(result.payload) if cipher else result.payload
        return Output(
            self._encode(reply_frame(frame, payload)),
            disconnect=result.disconnect,
        )

    def _dispatch(self, request: _Request) -> _Reply | Output:
        """Run the module's handler for a negotiation opcode; none for others."""
        handlers: dict[int, Callable[[_Request], _Reply | Output]] = {
            0x001: self._connect,
            0x003: self._capabilities,
            0x029: lambda _request: self._device_info(),
            0x005: self._set_capabilities,
            0x021: self._public_key,
            0x022: self._clock,
            0x023: self._bind,
            0x027: self._authenticate,
        }
        handler = handlers.get(request.frame.cmd.msgtype)
        return Output() if handler is None else handler(request)

    def _connect(self, request: _Request) -> _Reply | Output:
        encrypted = request.frame.cmd.encrypted
        if (
            not encrypted
            and self.config.enforce
            and self.config.auth_mode != AuthMode.OPEN
        ):
            return Output(disconnect=True)
        request.link.gcm_connect = encrypted
        request.link.capability_mode = CapabilityMode.NONE
        return _Reply(
            CONNECT_REPLY.build({"status": STATUS_OK, "connect_type": CONNECT_TYPE})
        )

    def _capabilities(self, request: _Request) -> _Reply:
        fields = request.fields
        if fields.app_encrypt is None or fields.mtu is None:
            return _status(STATUS_PARAMETER)
        request.link.capability_mode = CapabilityMode.LEGACY
        return _Reply(
            CAPABILITY_REPLY.build(
                {
                    "status": STATUS_OK,
                    "base_capability": BASE_CAPABILITY,
                    "mtu": min(fields.mtu, self.config.fragment_cap),
                    "advanced_capability": self.config.advanced_capability,
                    "stage2_a4": STAGE2_A4,
                    "auth_method": (
                        self.config.auth_mode
                        if self.config.reports_auth_method
                        else None
                    ),
                },
            ),
        )

    def _device_info(self) -> _Reply:
        return _Reply(
            DEVICE_INFO_REPLY.build(
                {
                    "status": STATUS_OK,
                    "info_type": DEVICE_INFO_TYPE,
                    "chip": self.config.chip,
                    "lib_version": self.config.lib_version,
                    "serial": self.config.serial,
                    "mac": self.config.mac,
                },
            ),
        )

    def _set_capabilities(self, request: _Request) -> _Reply:
        link = request.link
        method = request.fields.method or 0
        if method == 0:
            link.capability_mode = CapabilityMode.NONE
            return _status(STATUS_OK)
        if not method & METHOD_ANY:
            return _status(STATUS_FAIL)
        if (
            self.config.enforce
            and self.config.auth_mode != AuthMode.OPEN
            and not method & METHOD_ECDH
        ):
            return _status(STATUS_FAIL)
        link.capability_mode = (
            CapabilityMode.ECDH if method & METHOD_ECDH else CapabilityMode.LEGACY
        )
        return _status(STATUS_OK)

    def _public_key(self, request: _Request) -> _Reply:
        link = request.link
        if link.capability_mode != CapabilityMode.ECDH:
            return _status(STATUS_OK)
        if request.fields.point is None:
            return _status(STATUS_PARAMETER)
        key_pair = self._key_pair_factory()
        try:
            secret = key_pair.shared_secret(request.fields.point)
        except ValueError:
            return _status(STATUS_FAIL)
        link.session = session_gcm(secret) if link.gcm_connect else session_cbc(secret)
        link.ecdh_done = True
        if not link.gcm_connect:
            link.authorized = True
        return _Reply(
            PUBLIC_KEY_REPLY.build(
                {"status": STATUS_OK, "point": key_pair.public_point}
            ),
        )

    def _clock(self, request: _Request) -> _Reply | Output:
        if request.link.capability_mode != CapabilityMode.ECDH:
            return Output()
        if not request.frame.cmd.encrypted:
            return _status(STATUS_FAIL)
        if request.fields.time is None or request.fields.utc_offset is None:
            return _status(STATUS_PARAMETER)
        return _status(STATUS_OK)

    def _bind(self, request: _Request) -> _Reply:
        if request.fields.account is None:
            return _status(STATUS_PARAMETER)
        return _Reply(
            BIND_REPLY.build({"status": STATUS_OK, "serial": self.config.serial})
        )

    def _authenticate(self, request: _Request) -> _Reply:
        link = request.link
        if self.config.enforce and not (link.ecdh_done and request.frame.cmd.encrypted):
            return _status(STATUS_FAIL)
        token = request.fields.token
        if token is None or len(token) > MAX_TOKEN_LEN:
            return _status(STATUS_FAIL)
        if self.config.auth_mode == AuthMode.OPEN:
            return _status(STATUS_OK, disconnect=True)
        if self.config.auth_mode == AuthMode.TIME_LIMITED or token in self.enrolled:
            link.authorized = True
            return _status(STATUS_OK)
        link.pending_token = token
        link.auth_started_at = self.clock.now()
        return _Reply(
            AUTH_PENDING_REPLY.build(
                {
                    "status": STATUS_NEED_AUTHENTICATION,
                    "auth_timeout": self.config.auth_timeout,
                },
            ),
        )

    def _session(self, link: _Link, frame: Frame) -> Output:
        session = link.session
        if not link.authorized or session is None:
            return Output()
        plaintext = frame.payload
        if frame.cmd.encrypted:
            try:
                plaintext = session.decrypt(frame.payload)
            except (InvalidTag, ValueError):
                _LOGGER.warning("Dropped undecryptable %03x", frame.cmd.msgtype)
                return Output()
        msgtype = frame.cmd.msgtype
        if msgtype & ~RESPONSE >= MCU_OPCODE_MIN:
            if frame.pattern.channel == CHANNEL_APP:
                return Output()
            return self._route(link, frame, plaintext)
        reply = self.config.session_replies.get(msgtype)
        versions = self.config.versions
        if msgtype == MSGTYPE_VERSIONS and versions is not None:
            reply = VERSION_REPLY.build({"status": STATUS_OK, **asdict(versions)})
        if reply is None:
            return Output()
        return Output(self._encode(reply_frame(frame, session.encrypt(reply))))

    def _route(self, link: _Link, frame: Frame, plaintext: bytes) -> Output:
        """Route a request for the MCU by its ``a1``, as the module's dispatcher does.

        Only a request from BLE to the MCU is relayed; the module answers a
        logging-channel request itself and drops every other route.
        """
        route = _request_route(plaintext)
        if route is None:
            return Output()
        if route.source == ROUTE_HTTPS_LOG and link.session is not None:
            reply = link.session.encrypt(STATUS_REPLY.build({"status": STATUS_OK}))
            return Output(self._encode(reply_frame(frame, reply)))
        if (route.destination, route.source) != (ROUTE_MCU, ROUTE_BLE):
            return Output()
        self._apply(frame.cmd.msgtype, plaintext)
        frames = self.mcu.respond(
            frame.cmd.msgtype, request=plaintext, values=self.values
        )
        return self._relay(link, frames)

    def _apply(self, msgtype: int, plaintext: bytes) -> None:
        """Show an accepted command's settings in the telemetry that carries them."""
        layout = self.mcu.layout
        if layout is None:
            return
        for name, value in layout.state_changes(msgtype, plaintext).items():
            for target in layout.locate(name):
                self.values.setdefault(target, {})[name] = value

    def _relay(self, link: _Link, frames: list[Frame]) -> Output:
        """Encrypt the MCU's cleartext frames for the session and send them."""
        session = link.session
        if session is None:
            return Output()
        out: list[bytes] = []
        for mcu_frame in frames:
            relayed = make_frame(
                mcu_frame.pattern.composer,
                mcu_frame.pattern.channel,
                mcu_frame.cmd.msgtype,
                session.encrypt(mcu_frame.payload),
                encrypted=True,
            )
            out.extend(self._encode(relayed))
        return Output(out)
