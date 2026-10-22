# Copyright (c) 2026 Shawn Stricker
"""The device MCU behind the module: recorded cleartext replies and pushes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from construct import GreedyBytes, Int8ul, Struct

from .c_g2_490 import MSGTYPE_490
from .frame import CHANNEL_SESSION, COMPOSER_SEND, RESPONSE, make_frame
from .layouts import STATUS_ACCEPTED, LayoutError
from .messages import BLE_REPLY_ROUTE, ROUTE_FIELD
from .parameters import ParameterContainer, Parameters


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .c_g2_490 import CG2_490
    from .frame import Frame
    from .layouts import LayoutProtocol, Value


def mcu_frame(msgtype: int, cleartext: bytes, channel: int = CHANNEL_SESSION) -> Frame:
    """Return a frame as the MCU hands it to the module: cleartext, on ``0301xx``."""
    return make_frame(COMPOSER_SEND, channel, msgtype, cleartext)


def _rebuilt(frame: Frame, payload: bytes) -> Frame:
    """Return ``frame`` with another payload, on its own channel."""
    return mcu_frame(frame.cmd.msgtype, payload, frame.pattern.channel)


#: A reply's cleartext: its status byte, then its fields.
REPLY = Struct("status" / Int8ul, "fields" / GreedyBytes)
#: An MCU ack: its status, the ``a1`` route, then the fields it carries back.
ACK = Struct("status" / Int8ul, "route" / ROUTE_FIELD, "fields" / GreedyBytes)
#: The state name a read reply finds the MCU's RTC under: local seconds.
RTC = "rtc"
#: The state name a frame built for MQTT finds set: the unit is bound and on
#: WiFi to the cloud, where it has no BLE link.
CLOUD = "cloud"


@dataclass(frozen=True)
class Burst:
    """A push the MCU repeats a fixed number of times after each trigger.

    Attributes:
        trigger: The request msgtype that starts the burst, or restarts its count.
        push: The pushed msgtype.
        count: Pushes per trigger; the first goes out with the trigger's reply.
        period: Seconds between pushes.

    """

    trigger: int
    push: int
    count: int
    period: float


@dataclass(frozen=True)
class McuScript:
    """What the MCU answers, by request msgtype, and what it can push.

    A command the layout maps but nothing recorded answers gets an ack. An MCU
    that rejects answers a setting the layout refuses with ``04``; one that
    doesn't acks it ``00`` and leaves it unapplied.

    Attributes:
        replies: Cleartext frames sent in order for a request msgtype.
        pushes: Cleartext frames by push msgtype, sent on demand.
        layout: The product's layout, for command acks and named values.
        push_490: The C Gen 2 line's ``0490`` push, named fields, if posted.
        rejects: The MCU answers a refused setting ``04`` (the Prime MCUs),
            not ``00`` (the C Gen 2 display board's acks).
        channel: The channel the MCU's frames travel on: ``0f``, or ``11`` on
            the Prime Charger 160W.
        burst: The push a trigger repeats on a timer, where the MCU has one.
        commands: The firmware's command table: request msgtype -> the status
            it acks with when no recording or layout answers, or None for no
            reply (or none known).
        past_table_status: The ack status of a request past the table's last
            op; None where those are dropped.
        factory_reset: The request msgtype that factory-resets the device: it
            clears the settings and pairing, then the link drops.
        cloud_pushes: Pushes the MCU sends only to the cloud, never to BLE.
        mqtt_routes: The ``a1`` value of each message the MCU publishes to
            MQTT on the cloud, by msgtype.

    """

    replies: Mapping[int, Sequence[Frame]] = field(default_factory=dict)
    pushes: Mapping[int, Frame] = field(default_factory=dict)
    layout: LayoutProtocol | None = None
    push_490: CG2_490 | None = None
    rejects: bool = True
    channel: int = CHANNEL_SESSION
    burst: Burst | None = None
    commands: Mapping[int, int | None] = field(default_factory=dict)
    past_table_status: int | None = None
    factory_reset: int | None = None
    cloud_pushes: Mapping[int, Frame] = field(default_factory=dict)
    mqtt_routes: Mapping[int, bytes] = field(default_factory=dict)

    def state(self, values: Mapping[str, Value]) -> dict[str, Value]:
        """Return every named value the recorded frames carry, ``values`` over them."""
        layout = self.layout
        if layout is None:
            return dict(values)
        recorded: dict[str, Value] = {}
        for frames in (*self.replies.values(), self.pushes.values()):
            for frame in frames:
                recorded |= layout.read(frame.cmd.msgtype, frame.payload)
        return recorded | dict(values)

    def respond(
        self,
        msgtype: int,
        *,
        request: bytes = b"",
        values: Mapping[str, Value] | None = None,
        before: Mapping[str, Value] | None = None,
        rtc: int = 0,
    ) -> list[Frame]:
        """Return the frames answering a request; none for one the MCU drops.

        A listed request answers with its frames, an empty list being silence.
        Where the layout maps the command, the reply's status is the layout's
        check of the request's values (always ``00`` on an MCU that doesn't
        reject), and an accepted reply carries back the fields the layout
        echoes, followed by the pushes its change draws; a handler the layout
        marks silent sends only the pushes. Anything else gets the command
        table's ack, if it has one.

        Args:
            msgtype: The request's 12-bit message type.
            request: The request's cleartext fields, checked against the layout.
            values: The device's telemetry values by name, set in every frame
                that carries them.
            before: The values before the request applied; None if it changed
                nothing.
            rtc: The MCU's RTC, local seconds, for a read that reports it.

        """
        layout = self.layout
        if msgtype in self.replies:
            frames = list(self.replies[msgtype])
        elif layout is not None and layout.has_command(msgtype):
            status = layout.check(msgtype, request) if self.rejects else STATUS_ACCEPTED
            if status != STATUS_ACCEPTED:
                frames = [self._ack(msgtype, status)]
            else:
                after = self.state(values or {})
                prior = after if before is None else self.state(before)
                echoed = layout.reply_fields(msgtype, request, {**after, RTC: rtc})
                frames = [
                    *([] if echoed is None else [self._ack(msgtype, status, echoed)]),
                    *(
                        mcu_frame(push, payload, self.channel)
                        for push, payload in layout.follow_ups(
                            msgtype, request, prior, after
                        )
                    ),
                ]
        else:
            table_status = self._table_status(msgtype)
            if table_status is None:
                return []
            return [self._ack(msgtype, table_status)]
        return [self._with_values(frame, values, rtc) for frame in frames]

    def _ack(self, msgtype: int, status: int, echoed: bytes = b"") -> Frame:
        """Return the ``<status> a1 01 31`` reply to ``msgtype``, then ``echoed``."""
        ack = ACK.build(
            {"status": status, "route": {"route": BLE_REPLY_ROUTE}, "fields": echoed}
        )
        return mcu_frame(msgtype | RESPONSE, ack, self.channel)

    def _table_status(self, msgtype: int) -> int | None:
        """Return the command table's ack status for ``msgtype``; None to drop it."""
        if msgtype in self.commands:
            return self.commands[msgtype]
        if self.commands and msgtype > max(self.commands):
            return self.past_table_status
        return None

    def push(
        self,
        msgtype: int,
        *,
        values: Mapping[str, Value] | None = None,
        rtc: int | None = None,
    ) -> Frame:
        """Return the scripted push of ``msgtype``.

        Args:
            msgtype: The push's 12-bit message type.
            values: The device's telemetry values by name.
            rtc: The MCU's RTC, local seconds, for a push the clock shapes.

        Raises:
            KeyError: If the script has no such push.

        """
        return self._with_values(self.pushes[msgtype], values, rtc)

    def mqtt(
        self, msgtype: int, *, values: Mapping[str, Value] | None = None, rtc: int = 0
    ) -> Frame:
        """Return the frame the MCU publishes to MQTT as ``msgtype``, on the cloud.

        It is the push or reply the MCU would send to BLE, built from the named
        values with the cloud set, under its MQTT ``a1``.

        Raises:
            LayoutError: If the MCU publishes no such message to MQTT.

        """
        layout = self.layout
        route = self.mqtt_routes.get(msgtype)
        recorded = self._mqtt_source(msgtype)
        if layout is None or route is None or recorded is None:
            msg = f"the MCU publishes no {msgtype:04x} to MQTT"
            raise LayoutError(msg)
        names = layout.names(msgtype)
        named = {name: value for name, value in (values or {}).items() if name in names}
        payload = layout.update(
            msgtype, recorded.payload, {**named, RTC: rtc, CLOUD: True}
        )
        params = Parameters.parse(payload)
        params["a1"] = ParameterContainer(key=b"\xa1", type=None, value=route)
        return _rebuilt(recorded, Parameters.build(params))

    def _mqtt_source(self, msgtype: int) -> Frame | None:
        """Return the recorded push or reply ``msgtype`` is built from, if any."""
        if msgtype in self.pushes:
            return self.pushes[msgtype]
        if msgtype in self.cloud_pushes:
            return self.cloud_pushes[msgtype]
        replies = (frame for frames in self.replies.values() for frame in frames)
        return next((frame for frame in replies if frame.cmd.msgtype == msgtype), None)

    def _with_values(
        self, frame: Frame, values: Mapping[str, Value] | None, rtc: int | None
    ) -> Frame:
        """Return ``frame`` with the named values set.

        A frame the layout rebuilds also gets the RTC, under ``RTC``, when known.
        """
        msgtype = frame.cmd.msgtype
        if not values:
            return frame
        if self.push_490 is not None and msgtype == MSGTYPE_490:
            payload = self.push_490.update(frame.payload, values)
        elif self.layout is not None:
            names = self.layout.names(msgtype)
            named = {name: value for name, value in values.items() if name in names}
            if not named:
                return frame
            if rtc is not None:
                named[RTC] = rtc
            payload = self.layout.update(msgtype, frame.payload, named)
        else:
            return frame
        return _rebuilt(frame, payload)
