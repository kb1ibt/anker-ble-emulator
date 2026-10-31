# Copyright (c) 2026 Shawn Stricker
"""The comms module: negotiation, link state, authorization and the MCU relay."""

from __future__ import annotations

import heapq
import itertools
import logging
import secrets
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from cryptography.exceptions import InvalidTag

from .crypto import (
    AES_BLOCK,
    DEFAULT_ACCOUNT,
    STATIC_GCM,
    STATIC_IV,
    STATIC_KEY,
    STATIC_NONCE,
    CbcCipher,
    DeviceKeyPair,
    GcmCipher,
    session_cbc,
    session_gcm,
)
from .frame import (
    CHANNEL_APP,
    CHANNEL_NEGOTIATION,
    CHANNEL_SESSION,
    CHANNEL_WIFI,
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
from .mcu import CLOUD, COMMAND, EPOCH, GIVEN, LINK, RTC, UNIT_EVENT, given, mcu_frame
from .messages import (
    AUTH_PENDING_REPLY,
    BIND_REPLY,
    CAPABILITY_REPLY,
    CONFER_KEY_REPLY,
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
from .posix_tz import PosixTz
from .timed import Step, Timed
from .tlv import FieldError, decode_fields


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from construct import Container

    from .clock import Clock
    from .crypto import Cipher
    from .frame import Frame
    from .layouts import LayoutProtocol, Value
    from .live import LiveValues
    from .mcu import Burst, McuScript
    from .messages import Fields

_LOGGER = logging.getLogger(__name__)

#: App-channel opcodes below this are the module's own; the rest go to the MCU.
MCU_OPCODE_MIN = 0x40
#: The arm-grant ``4827``.
MSGTYPE_GRANT = RESPONSE | 0x027
#: ``0030``: the module's version read.
MSGTYPE_VERSIONS = 0x030
MSGTYPE_CONNECT = 0x001
MSGTYPE_KEY_EXCHANGE = 0x021
#: ``0022``: the clock set in ECDH mode, the legacy getAesKey confer in modes 1/2.
MSGTYPE_CONFER = 0x022

STATUS_OK = 0x00
STATUS_FAIL = 0x01
STATUS_ACCOUNT = 0x02
STATUS_PARAMETER = 0x04
#: The dispatcher's reply to an empty payload or a CBC one not whole blocks.
STATUS_LENGTH = 0x08
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
#: How often the MCU's timed pushes are checked: the Prime MCU's 100 ms tick.
PUSH_PERIOD = 0.1
#: A factory reset's link drop follows its ack by the MCU's 10-tick debounce.
FACTORY_RESET_DELAY = 10 * PUSH_PERIOD
#: The MCU's RTC is a 32-bit seconds counter.
RTC_MODULUS = 1 << 32
#: ``set_utc_time_to_mcu`` forwards no UTC below this (2023-11-25 08:13:20).
RTC_SYNC_MIN = 0x6561ACA0
#: The offsets an MCU that keeps blanks ignores: 0 and ``0xffffffff`` (-1).
BLANK_OFFSETS = frozenset({0, -1})
#: ``0022 a5`` that ``aws_timezone_set`` rewrites from ``a3``.
BARE_GMT = "GMT0"
#: The TZ timer (a1783-control.md "Clock"): first 100 ms after ``0022``, then
#: every 30 min, every 300 s within 30 min of a DST change, 5 s within 300 s.
TZ_FIRST = 0.1
TZ_FAR = 1800
TZ_NEAR = 300
TZ_CLOSE = 5
#: Session ops the WiFi handler takes, on fid ``0x0f`` and ``0x13`` only.
WIFI_OPS = range(0x022, 0x026)


def _handled_on(op: int, channel: int) -> bool:
    """Return whether the module answers or relays session op ``op`` on ``channel``.

    The relay serves fid ``0x0f``; the module's own ops answer on ``0x0f`` and
    ``0x11``, except the WiFi handler's, which answer on ``0x0f`` and ``0x13``.
    """
    if op >= MCU_OPCODE_MIN:
        return channel == CHANNEL_SESSION
    if op in WIFI_OPS:
        return channel in {CHANNEL_SESSION, CHANNEL_WIFI}
    return channel in {CHANNEL_SESSION, CHANNEL_APP}


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
        model: The model, or its OTA type on the C Gen 2 line (``A1783_low``);
            None where the build reports no component names.
        mcu: The MCU component name.
        esp32: The module component name.

    """

    module: bytes
    device: bytes
    model: bytes | None = None
    mcu: bytes | None = None
    esp32: bytes | None = None


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
        serial_tail: How many of the serial's last characters ``0829 a5``
            carries after the MAC.
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
    serial_tail: int = 0
    session_replies: Mapping[int, bytes] = field(default_factory=dict)
    versions: Versions | None = None


@dataclass
class Output:
    """Frames to notify, in order, and whether the module drops the link."""

    frames: list[bytes] = field(default_factory=list)
    disconnect: bool = False


class DeviceModule(Protocol):
    """What ``EmulatedDevice``/``EmulatedBleakBackend`` need from a module."""

    @property
    def connected(self) -> bool:
        """Whether a link is up."""

    @property
    def fragment_cap(self) -> int:
        """The largest frame the link carries."""

    def connect(self) -> None:
        """Bring a link up with fresh state."""

    def disconnect(self) -> None:
        """Drop the link."""

    def write(self, data: bytes) -> Output:
        """Handle one write from the client."""

    def check_timers(self) -> Output:
        """Run the module's own timers once, if it has any."""

    def check_pushes(self) -> Output:
        """Send the MCU's timed pushes that are due, if it has any."""

    def press_button(self) -> Output:
        """Simulate a press of the device's physical button."""


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


@dataclass(order=True)
class _Queued:
    """A scheduled event: when it falls due, then its scheduling order."""

    due: float
    order: int
    event: Timed = field(compare=False)


def _drop(_values: Mapping[str, Value], _state: Mapping[str, Value]) -> Step:
    """Drop the link: a factory reset's, once its debounce runs out."""
    return Step(disconnect=True)


def _burst(burst: Burst, left: int) -> tuple[Timed, ...]:
    """Return the burst's next push while ``left`` are still owed; none after."""
    if left <= 0:
        return ()
    push = Step(pushes=(burst.push,), then=_burst(burst, left - 1))
    return (Timed(burst.period, "burst", lambda _values, _state: push, link=True),)


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
        #: The device's telemetry values by name, set in every frame that carries them.
        self.values: dict[str, Value] = {}
        #: Draws the live values once a period while set (``vary_live_values``).
        self.live: LiveValues | None = None
        #: Values set on the unit (``set_values``): a draw leaves them.
        self.pinned: set[str] = set()
        self._key_pair_factory = key_pair_factory
        self._link: _Link | None = None
        #: Bound and on WiFi to the cloud (``EmulatedDevice.set_cloud``).
        self.cloud = False
        #: What the MCU published to MQTT, in order: (msgtype, plain ``ff09`` frame).
        self.published: list[tuple[int, bytes]] = []
        #: The scheduled events, soonest first.
        self._queue: list[_Queued] = []
        self._order = itertools.count()
        #: When the MCU's clock was last set, the UTC it was set to, and the
        #: offset west of UTC that puts the RTC on local time.
        self._clock_set: tuple[float, int, int] = (clock.now(), 0, 0)
        #: The module's own clock: when it was set, and the UTC it was set to.
        self._module_clock: tuple[float, int] = (clock.now(), 0)
        #: The ``0023`` account: RAM only, so it outlives a link but not a reboot.
        self._account = b""
        #: The port's key and IV, zeros at boot: an encrypted frame with no
        #: session opens under them, CBC unless the link connected encrypted.
        #: A new link keeps them.
        self._port_context = (bytes(AES_BLOCK), bytes(AES_BLOCK))
        self.start_mcu()

    @property
    def connected(self) -> bool:
        """Whether a link is up."""
        return self._link is not None

    @property
    def authorized(self) -> bool:
        """Whether the current link may reach the MCU."""
        return self._link is not None and self._link.authorized

    @property
    def fragment_cap(self) -> int:
        """The largest frame the link carries (ATT MTU - 3)."""
        return self.config.fragment_cap

    @property
    def rtc(self) -> int:
        """The MCU's RTC: local seconds, counting from 0 until a clock set."""
        return self._rtc_at(self.clock.now())

    @property
    def epoch(self) -> int:
        """The UTC epoch the device stamps a push's ``fe`` with; 0 until a clock set."""
        return self._epoch_at(self.clock.now())

    def _epoch_at(self, now: float) -> int:
        """Return the UTC epoch at device-clock instant ``now``."""
        set_at, utc, _ = self._clock_set
        return (utc + int(now - set_at)) % RTC_MODULUS

    def _rtc_at(self, now: float) -> int:
        """Return the local RTC at device-clock instant ``now``."""
        return (self._epoch_at(now) - self._clock_set[2]) % RTC_MODULUS

    def connect(self) -> None:
        """Bring a link up with fresh state, after what fell due without one."""
        self._run_due()
        self._link = _Link(connected_at=self.clock.now())

    def disconnect(self) -> None:
        """Drop the link and the events that end with it, after what fell due on it.

        Those due events run with the link still up, so their values are right
        and their MQTT copies publish; their BLE frames go only to a caller that
        sends them first (``check_pushes``, as the bleak backend does).
        """
        self._run_due()
        self._link = None
        self._queue = [queued for queued in self._queue if not queued.event.link]
        heapq.heapify(self._queue)

    def schedule(self, *events: Timed, start: float | None = None) -> None:
        """Queue ``events``, each replacing any queued one with its key.

        Args:
            events: The events, each due its delay after ``start``.
            start: When their delays count from; now if None.

        """
        keys = {event.key for event in events}
        self._queue = [queued for queued in self._queue if queued.event.key not in keys]
        heapq.heapify(self._queue)
        base = self.clock.now() if start is None else start
        for event in events:
            heapq.heappush(
                self._queue, _Queued(base + event.delay, next(self._order), event)
            )

    def start_mcu(self) -> None:
        """Queue the timed events the MCU's layout runs from its start."""
        layout = self.mcu.layout
        if layout is not None:
            self.schedule(*layout.started())

    def start_timed(  # noqa: PLR0913  # the change, its clock and its cause
        self,
        before: Mapping[str, Value],
        clock: tuple[int, int] | None = None,
        *,
        command: int | None = None,
        unit_event: str | None = None,
        at: float | None = None,
        names: Iterable[str] = (),
    ) -> None:
        """Queue the timed events the layout starts for a change of state.

        Args:
            before: The set values before the change.
            clock: The (RTC, epoch) before the change; None if they didn't change.
            command: The request msgtype that made the change; None for one
                made on the unit or a clock set.
            unit_event: The on-unit event that made it, by name; None for none.
            at: The device-clock instant of the change; now if None.
            names: The names the change set directly, under ``GIVEN``.

        """
        layout = self.mcu.layout
        if layout is None:
            return
        now = self.clock.now() if at is None else at
        rtc, epoch = (
            (self._rtc_at(now), self._epoch_at(now)) if clock is None else clock
        )
        prior = {**self.mcu.state(before), RTC: rtc, EPOCH: epoch}
        after = self._state(now)
        if command is not None:
            after[COMMAND] = command
        if unit_event is not None:
            after[UNIT_EVENT] = unit_event.encode()
        if set_names := given(names):
            after[GIVEN] = set_names
        self.schedule(*layout.timed(prior, after), start=now)

    def apply_values(
        self,
        values: Mapping[str, Value],
        layout: LayoutProtocol | None,
        at: float | None = None,
    ) -> None:
        """Set ``values`` and what ``layout`` says they imply; queue what they start.

        Args:
            values: The values, by field name.
            layout: The layout whose implied values follow; None for none, which
                starts nothing either.
            at: The device-clock instant of the change; now if None.

        """
        before = dict(self.values)
        self.values.update(values)
        if layout is not None:
            self.values.update(layout.implied(values))
            self.start_timed(before, at=at, names=values)

    def refresh_live(self, at: float | None = None) -> bool:
        """Draw the live values if a period has passed since the last draw.

        What the draw starts is queued from ``at``, ahead of what was due then.

        Args:
            at: The device-clock instant of the frame about to be built; now if None.

        Returns:
            Whether it drew.

        """
        live = self.live
        if live is None:
            return False
        now = self.clock.now() if at is None else at
        if live.due(now) is None:
            return False
        layout = self.mcu.layout
        derive = None if layout is None else layout.derive
        drawn = live.draw(self._state(now), self.pinned, now, derive)
        self.apply_values(drawn, layout, at=now)
        return True

    def _state(self, at: float | None = None) -> dict[str, Value]:
        """Return the device's whole state: values, set ones, the clock, the links.

        Args:
            at: The device-clock instant the clock is read at; now if None.

        """
        now = self.clock.now() if at is None else at
        return {
            **self.mcu.state(self.values),
            RTC: self._rtc_at(now),
            EPOCH: self._epoch_at(now),
            CLOUD: int(self.cloud),
            LINK: int(self.authorized),
        }

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
        if whole.pattern.channel in {CHANNEL_SESSION, CHANNEL_APP, CHANNEL_WIFI}:
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

        Nothing goes out while the layout holds the push back.

        Args:
            msgtype: The push's 12-bit message type.

        """
        ahead, disconnect = self._run_due() if self.refresh_live() else ([], False)
        frames = self._route_pushes(
            [self.mcu.push(msgtype, values=self.values, rtc=self.rtc)]
        )
        link = self._link
        if link is None or not link.authorized:
            return Output()
        out = self._relay(link, [*ahead, *frames])
        return Output(out.frames, disconnect=disconnect)

    def _route_pushes(self, frames: list[Frame]) -> list[Frame]:
        """Return the frames that go out on BLE; publish the held pushes' MQTT copies.

        The layout's ``sends`` keeps a push off BLE (a Prime on WiFi), and its
        ``mqtt_routed`` gives what the MCU publishes for it instead. A reply
        always goes out.
        """
        layout = self.mcu.layout
        if layout is None:
            return frames
        ble = []
        for frame in frames:
            msgtype = frame.cmd.msgtype
            if msgtype & RESPONSE or layout.sends(msgtype, self.values):
                ble.append(frame)
            else:
                self._publish(layout.mqtt_routed(msgtype, frame.payload, self.values))
        return ble

    def _publish(self, messages: Iterable[tuple[int, bytes]]) -> None:
        """Add ``messages`` to what the device published, as plain ``ff09`` frames."""
        self.published.extend(
            (msgtype, encode(mcu_frame(msgtype, payload, self.mcu.channel)))
            for msgtype, payload in messages
        )

    def check_pushes(self) -> Output:
        """Run the events due by now; send their pushes on an authorized link."""
        frames, disconnect = self._run_due()
        link = self._link
        if link is None:
            return Output()
        return Output(self._relay(link, frames).frames, disconnect=disconnect)

    def _run_due(self) -> tuple[list[Frame], bool]:
        """Run every event due by now, in order; return its BLE pushes and any drop.

        Each sets its values; one a jump bypassed sends and publishes nothing, and
        one with no authorized link publishes to MQTT but sends nothing on BLE.
        """
        frames: list[Frame] = []
        disconnect = False
        link = self._link
        while self._queue and self._queue[0].due <= self.clock.now():
            queued = heapq.heappop(self._queue)
            if self.refresh_live(queued.due):
                self._requeue(queued)
                continue
            step = queued.event.action(dict(self.values), self._state(queued.due))
            self.values.update(step.values)
            if self.mcu.layout is not None:
                self.values.update(self.mcu.layout.implied(step.values))
            for name in step.unset:
                self.values.pop(name, None)
            self.schedule(*step.then, start=queued.due)
            if self.clock.bypassed(queued.due):
                continue
            rtc = self._rtc_at(queued.due)
            ble = self._route_pushes(
                [
                    *(
                        self.mcu.push(msgtype, values=self.values, rtc=rtc)
                        for msgtype in step.pushes
                    ),
                    *(
                        mcu_frame(msgtype, payload, self.mcu.channel)
                        for msgtype, payload in step.frames
                    ),
                ]
            )
            self._publish(step.published)
            if link is None or not link.authorized:
                continue
            disconnect = disconnect or step.disconnect
            frames.extend(ble)
        return frames, disconnect

    def _requeue(self, queued: _Queued) -> None:
        """Queue ``queued`` again behind what a draw started, unless it was replaced."""
        if all(other.event.key != queued.event.key for other in self._queue):
            heapq.heappush(
                self._queue, _Queued(queued.due, next(self._order), queued.event)
            )

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

    def _link_cipher(self, link: _Link) -> Cipher:
        """Return the cipher an encrypted frame meets: the session, else the port's."""
        if link.session is not None:
            return link.session
        if link.gcm_connect:
            return STATIC_GCM
        return CbcCipher(*self._port_context)

    def _open(self, cipher: Cipher, frame: Frame) -> bytes | Output:
        """Decrypt a frame as the dispatcher does, or return what it answers instead.

        An empty payload, or a CBC one that isn't whole blocks, gets ``08``; one
        that fails to decrypt gets ``01``; both sealed under ``cipher``, and the
        link stays up. A negotiation ``0021`` drops either failure, ``0022`` a
        failed decrypt.
        """
        payload = frame.payload
        if not payload or (isinstance(cipher, CbcCipher) and len(payload) % AES_BLOCK):
            status = STATUS_LENGTH
        else:
            try:
                return cipher.decrypt(payload)
            except (InvalidTag, ValueError):
                status = STATUS_FAIL
        msgtype = frame.cmd.msgtype
        if frame.pattern.channel == CHANNEL_NEGOTIATION and (
            msgtype == MSGTYPE_KEY_EXCHANGE
            or (msgtype == MSGTYPE_CONFER and status == STATUS_FAIL)
        ):
            _LOGGER.warning("Dropped undecryptable %03x", msgtype)
            return Output()
        reply = cipher.encrypt(STATUS_REPLY.build({"status": status}))
        return Output(self._encode(reply_frame(frame, reply)))

    def _negotiate(self, link: _Link, frame: Frame) -> Output:
        msgtype = frame.cmd.msgtype
        if msgtype == MSGTYPE_CONNECT and frame.cmd.encrypted:
            link.gcm_connect, link.session = True, None
            self._port_context = (STATIC_KEY, STATIC_IV)
        if msgtype == MSGTYPE_CONFER and link.capability_mode == CapabilityMode.LEGACY:
            return self._legacy_confer(link, frame)
        cipher: Cipher | None = None
        plaintext = frame.payload
        if frame.cmd.encrypted:
            cipher = self._link_cipher(link)
            opened = self._open(cipher, frame)
            if isinstance(opened, Output):
                return opened
            plaintext = opened
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

    def _legacy_confer(self, link: _Link, frame: Frame) -> Output:
        """Handle the legacy getAesKey exchange (``0022``, capability mode 1).

        Decrypts CBC under the port context ``_connect`` installed, never the
        session or static-GCM cipher, and authorizes the link without a
        ``0027``. The derived key replaces the port's key.
        """
        if not frame.cmd.encrypted:
            return Output()
        iv = self._port_context[1]
        request_cipher = CbcCipher(*self._port_context)
        opened = self._open(request_cipher, frame)
        if isinstance(opened, Output):
            return opened
        try:
            fields = parse_request(frame.cmd.msgtype, opened)
        except FieldError:
            _LOGGER.warning("Dropped legacy confer with malformed fields")
            return Output()
        if fields.time is None or fields.utc_offset is None:
            return Output()
        key = secrets.token_bytes(16)
        payload = CONFER_KEY_REPLY.build({"status": STATUS_OK, "key": key})
        if link.gcm_connect:
            link.session = GcmCipher(key, STATIC_NONCE)
            reply_payload = STATIC_GCM.encrypt(payload)
        else:
            link.session = CbcCipher(key, iv)
            reply_payload = request_cipher.encrypt(payload)
        self._port_context = (key, iv)
        link.authorized = True
        return Output(self._encode(reply_frame(frame, reply_payload)))

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
            0x02B: self._set_utc_time,
        }
        handler = handlers.get(request.frame.cmd.msgtype)
        return Output() if handler is None else handler(request)

    def _connect(self, request: _Request) -> _Reply | Output:
        link = request.link
        encrypted = request.frame.cmd.encrypted
        if (
            not encrypted
            and self.config.enforce
            and self.config.auth_mode != AuthMode.OPEN
        ):
            return Output(disconnect=True)
        link.gcm_connect = encrypted
        link.capability_mode = CapabilityMode.NONE
        if not encrypted:
            account = request.fields.account or DEFAULT_ACCOUNT
            serial = self.config.serial or b""
            link.session = None
            self._port_context = (
                account[:AES_BLOCK].ljust(AES_BLOCK, b"\0"),
                serial[:AES_BLOCK].ljust(AES_BLOCK, b"\0"),
            )
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
        serial, tail = self.config.serial or b"", self.config.serial_tail
        return _Reply(
            DEVICE_INFO_REPLY.build(
                {
                    "status": STATUS_OK,
                    "info_type": DEVICE_INFO_TYPE,
                    "chip": self.config.chip,
                    "lib_version": self.config.lib_version,
                    "serial": self.config.serial,
                    "mac": self.config.mac + (serial[-tail:] if tail else b""),
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
        self._port_context = (secret[:AES_BLOCK], secret[AES_BLOCK : 2 * AES_BLOCK])
        link.ecdh_done = True
        if not link.gcm_connect:
            link.authorized = True
        return _Reply(
            PUBLIC_KEY_REPLY.build(
                {"status": STATUS_OK, "point": key_pair.public_point}
            ),
        )

    def _clock(self, request: _Request) -> _Reply | Output:
        """Set the MCU's RTC to local time: ``a1`` UTC less ``a3``, seconds west.

        A UTC before ``RTC_SYNC_MIN`` is acked but never reaches the RTC.
        """
        if request.link.capability_mode != CapabilityMode.ECDH:
            return Output()
        if not request.frame.cmd.encrypted:
            return _status(STATUS_FAIL)
        time, utc_offset = request.fields.time, request.fields.utc_offset
        if time is None or utc_offset is None:
            return _status(STATUS_PARAMETER)
        self._set_module_clock(time)
        self._set_rtc(time, utc_offset)
        if request.fields.tz:
            self._set_tz(request.fields.tz, utc_offset)
        return _status(STATUS_OK)

    def _set_module_clock(self, utc: int) -> None:
        """Set the module's own clock, whatever the RTC's gate (``settimeofday``)."""
        self._module_clock = (self.clock.now(), utc)

    def _set_tz(self, raw: bytes, utc_offset: int) -> None:
        """Store ``0022 a5``'s POSIX TZ and restart its timer 100 ms later.

        A bare ``GMT0`` takes its offset from ``a3``; a string the parser
        refuses leaves the stored zone, and its timer, as they were.
        """
        text = raw.rstrip(b"\0").decode("ascii", "replace")
        try:
            tz = PosixTz(utc_offset) if text == BARE_GMT else PosixTz.parse(text)
        except ValueError:
            _LOGGER.warning("Kept the stored TZ over an unparsed %r", text)
            return
        self.schedule(Timed(TZ_FIRST, "tz_timer", partial(self._tz_tick, tz)))

    def _tz_tick(
        self, tz: PosixTz, _values: Mapping[str, Value], _state: Mapping[str, Value]
    ) -> Step:
        """Send the MCU ``tz``'s offset now, then re-arm.

        Every 30 min; 300 s within 30 min of a DST change, 5 s within 300 s. A
        zero offset is sent as -1, which an MCU that keeps blanks ignores.
        """
        set_at, utc = self._module_clock
        elapsed = int(self.clock.now() - set_at)
        now = utc + elapsed
        west = tz.offset_west(now)
        self._set_rtc(now, west or -1, at=set_at + elapsed)
        change = tz.next_change(now)
        until = TZ_FAR if change is None else change - now
        delay = TZ_FAR if until > TZ_FAR else TZ_NEAR if until > TZ_NEAR else TZ_CLOSE
        return Step(then=(Timed(delay, "tz_timer", partial(self._tz_tick, tz)),))

    def _set_utc_time(self, request: _Request) -> _Reply:
        """Set the RTC from ``a1`` UTC and the ``a3`` zone, for the bound account.

        ``a2`` passes if the stored account starts with it. The zone's sign
        reaches the hours only, so a zone west of UTC lands as many hours east.
        """
        fields = request.fields
        if fields.time is None or fields.account is None or fields.timezone is None:
            return _status(STATUS_PARAMETER)
        account = fields.account
        if self._account.ljust(len(account), b"\0")[: len(account)] != account:
            return _status(STATUS_ACCOUNT)
        hours, tenths = fields.timezone.hours, fields.timezone.tenths
        east = abs(hours) * 3600 + (tenths * 360 if hours >= 0 else -tenths * 360)
        self._set_module_clock(fields.time)
        self._set_rtc(fields.time, -east)
        return _status(STATUS_OK)

    def _set_rtc(self, time: int, utc_offset: int, at: float | None = None) -> None:
        """Set the RTC to ``time`` less ``utc_offset`` seconds west.

        A UTC before ``RTC_SYNC_MIN``, or before the MCU's own floor, never
        reaches the RTC; the MCU sets its clock its lead ahead, and one that
        keeps blanks keeps its offset for a 0 or -1. A clock set is a change of
        state, so the layout re-times what depends on the RTC.

        Args:
            time: The UTC sent.
            utc_offset: The offset sent, seconds west.
            at: The device-clock instant ``time`` was read at; now if None.

        """
        if time >= max(RTC_SYNC_MIN, self.mcu.clock_floor):
            if self.mcu.clock_keeps_blank and utc_offset in BLANK_OFFSETS:
                utc_offset = self._clock_set[2]
            before = (self.rtc, self.epoch)
            utc = (time + self.mcu.clock_lead) % RTC_MODULUS
            anchor = self.clock.now() if at is None else at
            self._clock_set = (anchor, utc, utc_offset)
            self.start_timed(self.values, clock=before)

    def _bind(self, request: _Request) -> _Reply:
        if request.fields.account is None:
            return _status(STATUS_PARAMETER)
        self._account = request.fields.account
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
            opened = self._open(session, frame)
            if isinstance(opened, Output):
                return opened
            plaintext = opened
        msgtype = frame.cmd.msgtype
        if not _handled_on(msgtype & ~RESPONSE, frame.pattern.channel):
            return Output()
        if msgtype & ~RESPONSE >= MCU_OPCODE_MIN:
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
        overdue, dropped = self._run_due()
        if self.refresh_live():
            drawn, drew_drop = self._run_due()
            overdue, dropped = [*overdue, *drawn], dropped or drew_drop
        before = dict(self.values)
        changes = self._apply(frame.cmd.msgtype, plaintext)
        self.start_timed(before, command=frame.cmd.msgtype, names=changes)
        timed, disconnect = self._run_due()
        timed = [*overdue, *timed]
        disconnect = disconnect or dropped
        frames = self._route_pushes(
            self.mcu.respond(
                frame.cmd.msgtype,
                request=plaintext,
                values=self.values,
                before=before,
                rtc=self.rtc,
            )
        )
        if frame.cmd.msgtype == self.mcu.factory_reset:
            self.enrolled.clear()
            self.values.clear()
            self.schedule(Timed(FACTORY_RESET_DELAY, "factory_reset", _drop, link=True))
        burst = self.mcu.burst
        if burst is not None and frame.cmd.msgtype == burst.trigger:
            first = self.mcu.push(burst.push, values=self.values, rtc=self.rtc)
            frames.extend(self._route_pushes([first]))
            self.schedule(*_burst(burst, burst.count - 1))
        out = self._relay(link, [*frames, *timed])
        return Output(out.frames, disconnect=disconnect)

    def _apply(self, msgtype: int, plaintext: bytes) -> dict[str, Value]:
        """Show an accepted command's settings in the telemetry that carries them.

        Its side effects see the whole state a timed action does: the clock and
        the cloud flag too.

        Returns:
            The values it set.

        """
        layout = self.mcu.layout
        if layout is None:
            return {}
        changes = layout.state_changes(msgtype, plaintext, self._state())
        self.values.update(changes)
        return changes

    def _relay(self, link: _Link, frames: list[Frame]) -> Output:
        """Encrypt the MCU's cleartext frames for the session and send them."""
        session = link.session
        if session is None:
            return Output()
        out: list[bytes] = []
        for cleartext in frames:
            relayed = make_frame(
                cleartext.pattern.composer,
                cleartext.pattern.channel,
                cleartext.cmd.msgtype,
                session.encrypt(cleartext.payload),
                encrypted=True,
            )
            out.extend(self._encode(relayed))
        return Output(out)
