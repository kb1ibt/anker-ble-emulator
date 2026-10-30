# Copyright (c) 2026 Shawn Stricker
"""Emulated devices: identity, profile, the module and the MCU script."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum
from importlib import resources
from typing import TYPE_CHECKING, Any, ClassVar

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from construct import Bytes, ByteSwapped, Int8ub, Optional, Struct

from anker_ble_emulator.c_g2_490 import CG2_490
from anker_ble_emulator.clock import MonotonicClock
from anker_ble_emulator.frame import CHANNEL_SESSION, RESPONSE, encode
from anker_ble_emulator.layouts import LayoutError, LayoutProtocol
from anker_ble_emulator.legacy import LegacyModule, LegacyProfile
from anker_ble_emulator.mcu import Burst, McuScript, mcu_frame
from anker_ble_emulator.module import (
    PUSH_PERIOD,
    TIMER_PERIOD,
    AuthMode,
    DeviceModule,
    Module,
    ModuleConfig,
    Output,
    Versions,
)
from anker_ble_emulator.products import (
    GATT_LAYOUTS,
    PRODUCTS,
    ModuleBuild,
    Outer,
    Path,
    Product,
    Transport,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from anker_ble_emulator.clock import Clock
    from anker_ble_emulator.layouts import Value

#: A locally administered unicast MAC.
DEFAULT_MAC = "AA:12:DE:AD:BE:EF"
#: The manufacturer-data company id Anker advertises under.
COMPANY_ID = 0xFFFF
#: The negotiated transport's advertised service; every transport's layout
#: (served and advertised service, command and telemetry characteristics)
#: lives in ``anker_ble_emulator.products.GATT_LAYOUTS``.
SERVICE_UUID = GATT_LAYOUTS[Transport.NEGOTIATED].advertised
MAC_LEN = 6


#: A MAC split where the Prime name takes its last two bytes.
MAC_LAYOUT = Struct("head" / Bytes(4), "tail" / Bytes(2))

#: The sku's length by ``version_code``.
SKU_LENGTHS = {0x01: 3, 0x02: 4}

#: The manufacturer record: versionCode, MAC, bindType, productType, sku, and
#: the capability byte where the family has one.
ADVERT_LAYOUT = Struct(
    "version_code" / Int8ub,
    "mac" / Bytes(MAC_LEN),
    "bind_type" / Int8ub,
    "product_type" / Bytes(2),
    "sku" / Bytes(lambda this: SKU_LENGTHS[this.version_code]),
    "capability" / Optional(Int8ub),
)
#: The legacy transport's record: the MAC, byte-reversed.
LEGACY_ADVERT_LAYOUT = ByteSwapped(Bytes(MAC_LEN))


class Unset(Enum):
    """Marks a parameter left to the product's default."""

    UNSET = "unset"


UNSET = Unset.UNSET


@dataclass(frozen=True)
class Advert:
    """The advertisement: the name and the manufacturer record under ``0xffff``.

    A product whose record isn't recorded advertises none (``product_type``
    None); it's still found by its service.

    Attributes:
        local_name: The advertised name, or None while the device has none.
        version_code: ``01`` with a 3-byte sku, ``02`` with a 4-byte one.
        bind_type: The device's binding state.
        product_type: The model key, 2 bytes; None for no record.
        sku: The serial's sku substring.
        capability: The trailing capability byte; None where the family has none.
        prime_name: Name the device ``<model>_<last 2 MAC bytes>``, as the
            Prime line does, instead of ``local_name``.

    """

    local_name: str | None
    version_code: int | None = None
    bind_type: int | None = None
    product_type: bytes | None = None
    sku: bytes | None = None
    capability: int | None = None
    prime_name: bool = False

    def manufacturer_data(self, mac: bytes) -> bytes | None:
        """Return the record's bytes for a device with ``mac``; None for none."""
        if self.product_type is None:
            return None
        return ADVERT_LAYOUT.build(
            {
                "version_code": self.version_code,
                "mac": mac,
                "bind_type": self.bind_type,
                "product_type": self.product_type,
                "sku": self.sku,
                "capability": self.capability,
            },
        )


@dataclass(frozen=True)
class Profile:
    """How one product is emulated.

    Attributes:
        serial: The default synthetic serial.
        outer: The default outer.
        path: The default path.
        module_build: The default module firmware build.
        auth_mode: The provisioned policy byte.
        advert: The advertisement.
        data: Recorded-frame resources in ``devices/data/``; the first that
            holds a msgtype supplies it.
        replies: Reply msgtypes by request msgtype, in send order.
        pushes: Msgtypes the MCU can push.
        device_version: The device MCU firmware ``0830`` reports; None where no
            ``0830`` is recorded, which leaves ``0030`` unanswered.
        version_names: ``0830`` ``a3``-``a5``: the model and component names;
            None where the build reports none.
        module_replies: Msgtypes of recorded module session-op replies, by the
            build they were recorded on.
        known_commands: The MCU firmware's command table: every request it
            handles -> the status it acks with when no recording or layout
            answers, or None for no reply (or none known).
        past_table_status: The ack status of a request past the table's last
            op; None where the MCU drops those.
        rejects: The MCU answers a setting it refuses with ``04``; otherwise
            it acks ``00`` and leaves the setting unapplied.
        push_490: The ``0490`` push's field names in ``devices/data/``, where
            the MCU posts one.
        expansion: An expansion battery is attached.
        chip: ``0829 a2``.
        lib_version: ``0829 a3``.
        serial_tail: How many of the serial's last characters ``0829 a5``
            carries after the MAC.
        fragment_cap: The largest frame the module sends (``0803`` MTU cap).
        mcu_channel: The channel the MCU's frames travel on.
        layout: The product's own command/telemetry code, hand-written
            directly in its module.
        burst: The push a trigger repeats on a timer, where the MCU has one.
        factory_reset: The request msgtype that factory-resets the device.
        cloud_pushes: Msgtypes the MCU sends only to the cloud, never to BLE.
        mqtt_routes: The ``a1`` value of each message the MCU publishes to
            MQTT on the cloud, by msgtype; none where it isn't modeled.
        clock_floor: The earliest UTC the MCU's clock set takes.
        clock_lead: Seconds the MCU's clock set puts its clock ahead of the UTC.

    """

    serial: str
    outer: Outer
    path: Path
    module_build: ModuleBuild
    auth_mode: AuthMode
    advert: Advert
    data: tuple[str, ...]
    replies: Mapping[int, tuple[int, ...]]
    pushes: tuple[int, ...]
    device_version: str | None
    version_names: tuple[str, str, str] | None = None
    module_replies: Mapping[ModuleBuild, tuple[int, ...]] = field(default_factory=dict)
    known_commands: Mapping[int, int | None] = field(default_factory=dict)
    past_table_status: int | None = None
    rejects: bool = True
    push_490: str | None = None
    expansion: bool = True
    chip: bytes = b"ESP32"
    lib_version: bytes = b"0.0.0.3"
    serial_tail: int = 0
    fragment_cap: int = 253
    mcu_channel: int = CHANNEL_SESSION
    layout: LayoutProtocol | None = None
    burst: Burst | None = None
    factory_reset: int | None = None
    cloud_pushes: tuple[int, ...] = ()
    mqtt_routes: Mapping[int, bytes] = field(default_factory=dict)
    clock_floor: int = 0
    clock_lead: int = 0

    def frames(self) -> dict[int, bytes]:
        """Return the recorded cleartext payloads by msgtype."""
        frames: dict[int, bytes] = {}
        for name in reversed(self.data):
            frames |= {
                int(key, 16): bytes.fromhex(value)
                for key, value in data_resource(name).items()
            }
        return frames

    def session_replies(self, build: ModuleBuild) -> dict[int, bytes]:
        """Return ``build``'s recorded session-op replies by request msgtype."""
        frames = self.frames()
        return {
            reply & ~RESPONSE: frames[reply]
            for reply in self.module_replies.get(build, ())
        }

    def versions(self, build: ModuleBuild) -> Versions | None:
        """Return what ``0830`` reports on ``build``; None if it isn't recorded."""
        if self.device_version is None:
            return None
        versions = Versions(build.value.encode(), self.device_version.encode())
        if self.version_names is None:
            return versions
        model, mcu, esp32 = (name.encode() for name in self.version_names)
        return replace(versions, model=model, mcu=mcu, esp32=esp32)

    def script(self, layout: LayoutProtocol | None = None) -> McuScript:
        """Return the MCU script built from the packaged recorded frames.

        Args:
            layout: The product's layout, for command acks and named values.

        """
        frames, channel = self.frames(), self.mcu_channel
        return McuScript(
            replies={
                request: tuple(
                    mcu_frame(reply, frames[reply], channel) for reply in replies
                )
                for request, replies in self.replies.items()
            },
            pushes={
                msgtype: mcu_frame(msgtype, frames[msgtype], channel)
                for msgtype in self.pushes
            },
            layout=layout,
            push_490=None
            if self.push_490 is None
            else CG2_490.from_json(data_resource(self.push_490)),
            rejects=self.rejects,
            channel=channel,
            burst=self.burst,
            commands=self.known_commands,
            past_table_status=self.past_table_status,
            factory_reset=self.factory_reset,
            cloud_pushes={
                msgtype: mcu_frame(msgtype, frames[msgtype], channel)
                for msgtype in self.cloud_pushes
            },
            mqtt_routes=self.mqtt_routes,
            clock_floor=self.clock_floor,
            clock_lead=self.clock_lead,
        )


def data_resource(name: str) -> dict[str, Any]:
    """Return a JSON resource in ``devices/data/``."""
    text = resources.files(__package__).joinpath("data", name).read_text()
    data: dict[str, Any] = json.loads(text)
    return data


#: Emulation profiles by product; each product module registers its own.
PROFILES: dict[Product, Profile] = {}
#: Legacy-transport emulation profiles by product (``Transport.LEGACY``).
LEGACY_PROFILES: dict[Product, LegacyProfile] = {}


def register(pn: Product, profile: Profile) -> None:
    """Make ``pn`` emulatable with ``profile`` on the negotiated transport."""
    PROFILES[pn] = profile


def register_legacy(pn: Product, profile: LegacyProfile) -> None:
    """Make ``pn`` emulatable with ``profile`` on the legacy transport."""
    LEGACY_PROFILES[pn] = profile


def parse_mac(mac: str) -> bytes:
    """Return ``AA:BB:CC:DD:EE:FF`` (or bare hex) as 6 bytes.

    Raises:
        ValueError: If it isn't 6 hex bytes.

    """
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != MAC_LEN:
        msg = f"MAC {mac!r} is {len(raw)} bytes, not {MAC_LEN}"
        raise ValueError(msg)
    return raw


class EmulatedDevice:
    """One emulated device; a backend connects bleak clients to it."""

    #: The product's profile; its concrete type is the transport's.
    profile: Profile | LegacyProfile
    #: The provisioned serial; None for none.
    serial: str | None
    #: The product's layout, hand-written in its device module; None where it
    #: has none, which is every transport but the negotiated one.
    layout: LayoutProtocol | None
    #: What answers writes and generates notifications; the transport's module.
    module: DeviceModule

    def __init__(  # noqa: PLR0913  # the identity plus the four protocol choices
        self,
        pn: Product,
        serial: str | Unset | None = UNSET,
        mac: str = DEFAULT_MAC,
        transport: Transport | None = None,
        *,
        outer: Outer | None = None,
        path: Path | None = None,
        module: ModuleBuild | None = None,
        clock: Clock | None = None,
        mtu: int | None = None,
    ) -> None:
        """Build the device from its product's profile.

        Args:
            pn: The product.
            serial: The provisioned serial; None for none; unset for the default.
            mac: The BLE MAC.
            transport: The GATT transport; None for the product's.
            outer: The negotiation outer; None for the profile's. ``PLAIN``
                makes the module accept a cleartext connect whatever its build.
            path: The key establishment path; None for the profile's.
            module: The module firmware build; None for the profile's. Its
                build decides whether a cleartext connect is refused.
            clock: Time for the module's timers; the monotonic clock if None.
            mtu: The link's ATT MTU; None for the profile's fragment cap plus 3
                (253 -> 256, the MTU seen on the bench). Also the ceiling the
                module reports in ``0803``/``0829``: the real value is
                ``min(mtu, the client's own request)``.

        Raises:
            NotImplementedError: If the product or a chosen option isn't emulated.
            TypeError: If both ``outer`` and ``module`` are given, or ``outer``,
                ``path``, ``module``, or a non-default ``serial`` is given for a
                product on the legacy transport (none of them apply to it).

        """
        if outer is not None and module is not None:
            msg = "pass outer or module, not both"
            raise TypeError(msg)
        self.pn = pn
        self.mac = parse_mac(mac)
        self.transport = transport or PRODUCTS[pn].transport
        gatt = GATT_LAYOUTS.get(self.transport)
        if gatt is None:
            msg = f"{self.transport} transport: not emulated yet"
            raise NotImplementedError(msg)
        self.gatt = gatt
        #: Seconds between runs of the module's authorize timer; unused outside
        #: the negotiated transport, which is the only one with such a timer.
        self.timer_period = TIMER_PERIOD
        #: Seconds between checks for the MCU's due timed pushes (its burst).
        self.push_period = PUSH_PERIOD
        #: On WiFi to the cloud, with no BLE link (``set_cloud``).
        self.cloud = False
        self._listener: Callable[[Output], None] | None = None
        if self.transport == Transport.LEGACY:
            self._init_legacy(pn, serial, outer, path, module, mtu)
            return
        profile = PROFILES.get(pn)
        if profile is None:
            msg = f"{pn} has no emulation profile yet"
            raise NotImplementedError(msg)
        self.profile = profile
        self.serial = profile.serial if serial is UNSET else serial
        self.outer = outer or profile.outer
        self.path = path or profile.path
        self.module_build = module or profile.module_build
        if self.path != Path.ECDH:
            msg = f"{self.path} path: not emulated yet"
            raise NotImplementedError(msg)
        config = ModuleConfig(
            mac=self.mac,
            serial=None if self.serial is None else self.serial.encode(),
            auth_mode=profile.auth_mode,
            enforce=self.module_build.enforces and self.outer == Outer.ENCRYPTED,
            fragment_cap=profile.fragment_cap if mtu is None else mtu - 3,
            chip=profile.chip,
            lib_version=profile.lib_version,
            serial_tail=profile.serial_tail,
            session_replies=profile.session_replies(self.module_build),
            versions=profile.versions(self.module_build),
        )
        #: The product's own command/telemetry code, where it has one.
        self.layout = profile.layout
        script = profile.script(self.layout)
        self.module = Module(config, script, clock or MonotonicClock())
        if script.push_490 is not None and not profile.expansion:
            self.module.values.update(script.push_490.without_expansion())

    def _init_legacy(  # noqa: PLR0913, PLR0917  # mirrors __init__'s own rejected choices
        self,
        pn: Product,
        serial: str | Unset | None,
        outer: Outer | None,
        path: Path | None,
        module: ModuleBuild | None,
        mtu: int | None,
    ) -> None:
        """Build a legacy-transport device; see ``__init__``.

        Raises:
            NotImplementedError: If the product has no legacy emulation profile.
            TypeError: If ``outer``, ``path``, ``module``, ``mtu``, or a
                non-default ``serial`` is given; none of them apply to this
                transport.

        """
        if (
            outer is not None
            or path is not None
            or module is not None
            or mtu is not None
        ):
            msg = "outer/path/module/mtu choose the negotiated transport's options"
            raise TypeError(msg)
        if serial is not UNSET:
            msg = f"{pn}'s serial is fixed in its captured telemetry"
            raise TypeError(msg)
        legacy_profile = LEGACY_PROFILES.get(pn)
        if legacy_profile is None:
            msg = f"{pn} has no legacy emulation profile yet"
            raise NotImplementedError(msg)
        self.profile = legacy_profile
        self.serial = legacy_profile.serial_number
        self.layout = None
        self.module = LegacyModule(legacy_profile)

    @property
    def negotiated_module(self) -> Module:
        """The module, narrowed to the negotiated ``ff09`` transport's.

        Raises:
            NotImplementedError: If this device's transport has no MCU script.

        """
        if not isinstance(self.module, Module):
            msg = f"{self.pn} speaks {self.transport}, which has no MCU script"
            raise NotImplementedError(msg)
        return self.module

    def set_values(self, **values: Value) -> None:
        """Set telemetry values by field name in every later frame that carries them.

        Names are the Anker app's, in snake_case, plus the ``0490`` push's. The
        timed events the change starts run once due; those due at once run here.

        Raises:
            LayoutError: If no message the device sends has a field so named.
            TypeError: If a value doesn't fit its field.

        """
        layout, push_490 = self.layout, self.mcu.push_490
        for name, value in values.items():
            known = False
            if layout is not None:
                for msgtype in layout.locate(name):
                    layout.build(msgtype, {name: value})
                    known = True
            if push_490 is not None and name in push_490.fields:
                push_490.check(name, value)
                known = True
            if not known:
                msg = f"{self.pn} sends no typed field {name!r}"
                raise LayoutError(msg)
        module = self.negotiated_module
        before = dict(module.values)
        module.values.update(values)
        if layout is not None:
            module.values.update(layout.implied(values))
            module.start_timed(before)
            self._emit(module.check_pushes())

    @property
    def mcu(self) -> McuScript:
        """What the MCU answers and pushes."""
        return self.negotiated_module.mcu

    def use_mcu(self, script: McuScript) -> None:
        """Replace what the MCU answers and pushes (``McuScript()`` for a quiet one)."""
        self.negotiated_module.mcu = script

    def set_reply(self, request: int, *replies: tuple[int, bytes]) -> None:
        """Make the MCU answer ``request`` with these frames, in order.

        Args:
            request: The request's 12-bit message type.
            replies: ``(msgtype, cleartext)`` of each frame; none for silence.

        """
        script = self.negotiated_module.mcu
        frames = tuple(
            mcu_frame(msgtype, cleartext, script.channel)
            for msgtype, cleartext in replies
        )
        self.use_mcu(replace(script, replies={**script.replies, request: frames}))

    def set_push(self, msgtype: int, cleartext: bytes) -> None:
        """Make the MCU push ``cleartext`` as ``msgtype`` (sent by ``push``)."""
        script = self.negotiated_module.mcu
        frame = mcu_frame(msgtype, cleartext, script.channel)
        self.use_mcu(replace(script, pushes={**script.pushes, msgtype: frame}))

    @property
    def address(self) -> str:
        """The BLE address bleak reports for the device."""
        return ":".join(f"{byte:02X}" for byte in self.mac)

    @property
    def local_name(self) -> str | None:
        """The advertised name: the profile's, or ``<model>_<last 2 MAC bytes>``."""
        if isinstance(self.profile, LegacyProfile):
            return self.profile.local_name
        advert = self.profile.advert
        if not advert.prime_name:
            return advert.local_name
        return f"{self.pn}_{MAC_LAYOUT.parse(self.mac).tail.hex().upper()}"

    @property
    def ble_device(self) -> BLEDevice:
        """A ``BLEDevice`` that carries this device in ``details``."""
        return BLEDevice(self.address, self.local_name, self)

    @property
    def advertisement_data(self) -> AdvertisementData:
        """The advertisement a scan would report."""
        if isinstance(self.profile, LegacyProfile):
            #: This transport advertises no manufacturer record, only the MAC
            #: reversed under the Anker company id (observed on the wire).
            record: bytes | None = LEGACY_ADVERT_LAYOUT.build(self.mac)
        else:
            record = self.profile.advert.manufacturer_data(self.mac)
        return AdvertisementData(
            local_name=self.local_name,
            manufacturer_data={} if record is None else {COMPANY_ID: record},
            service_data={},
            service_uuids=[self.gatt.advertised],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        )

    def listen(self, listener: Callable[[Output], None] | None) -> None:
        """Route the device's own output (button grants, pushes) to ``listener``."""
        self._listener = listener

    def press_button(self) -> None:
        """Press the side button."""
        self._emit(self.module.press_button())

    def push(self, msgtype: int) -> None:
        """Make the MCU push its recorded frame of ``msgtype``."""
        self._emit(self.negotiated_module.push(msgtype))

    def notify(self, frames: list[bytes]) -> None:
        """Send raw bytes to the connected client as notifications, in order."""
        self._emit(Output(frames))

    def drop(self) -> None:
        """Drop the link from the device side."""
        self._emit(Output(disconnect=True))

    def set_cloud(self, on: bool) -> None:  # noqa: FBT001  # a switch
        """Put the device on WiFi to the cloud, or back on BLE.

        On the cloud (bound, with the module's cloud flag set) the device has no
        BLE link: an open link drops and connecting fails until it's switched off.
        """
        self.cloud = on
        if on:
            self.drop()

    def mqtt_frame(self, msgtype: int) -> bytes:
        """Return the plain ``ff09`` frame the device publishes to MQTT as ``msgtype``.

        Built from the device's state on the cloud; nothing is sent on a link.

        Raises:
            RuntimeError: If the device isn't on the cloud (``set_cloud``), or
                its state holds the message back (a C Gen 2 ``0503`` outside an
                active TOU period).
            LayoutError: If it publishes no such message to MQTT.

        """
        if not self.cloud:
            msg = f"{self.pn} publishes to MQTT only on the cloud (set_cloud)"
            raise RuntimeError(msg)
        module = self.negotiated_module
        return encode(module.mcu.mqtt(msgtype, values=module.values, rtc=module.rtc))

    def _emit(self, output: Output) -> None:
        if self._listener is not None:
            self._listener(output)


class ProductDevice(EmulatedDevice):
    """An ``EmulatedDevice`` whose product is its class's ``PRODUCT``."""

    PRODUCT: ClassVar[Product]

    def __init__(  # noqa: PLR0913  # the identity plus the four protocol choices
        self,
        serial: str | Unset | None = UNSET,
        mac: str = DEFAULT_MAC,
        transport: Transport | None = None,
        *,
        outer: Outer | None = None,
        path: Path | None = None,
        module: ModuleBuild | None = None,
        clock: Clock | None = None,
        mtu: int | None = None,
    ) -> None:
        """Build the product's device; see ``EmulatedDevice``."""
        super().__init__(
            self.PRODUCT,
            serial,
            mac,
            transport,
            outer=outer,
            path=path,
            module=module,
            clock=clock,
            mtu=mtu,
        )
