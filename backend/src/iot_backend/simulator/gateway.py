"""The simulated gateway: virtual node C handing events to the controller.

It implements `iot_backend.transports.Gateway`. The controller receives plain events and the
clock; it never receives node objects, so it cannot read simulator internals. Commands go the
other way as plain `RouteUpdateCommand` values: C turns each into a management frame that it
originates on the controller's behalf (the frame's source is C).

Between C and the laptop sits C's bridge (`bridge.py`): heartbeat watching, a bounded buffer and
replay. `connected = False` models the laptop side being gone (the controller process stopped,
or its serial reader): heartbeats and commands going down and every message going up are lost,
exactly as they would be on hardware. C notices by the missing heartbeats, not by this flag.
"""

from collections.abc import Callable
from dataclasses import replace

from iot_backend.protocol import (
    FailoverReason,
    Frame,
    LocalFailoverReportPayload,
    NeighborCode,
    RouteAckStatus,
    RouteInstallAckPayload,
    TelemetryPayload,
    TopologyReportPayload,
)
from iot_backend.simulator.bridge import BridgeConfig, CBridge
from iot_backend.simulator.engine import Engine
from iot_backend.transports import (
    GatewayEvent,
    GatewayHandler,
    Heartbeat,
    LocalFailoverEvent,
    NeighborObservation,
    ReadingEvent,
    RouteInstallAckEvent,
    RouteUpdateCommand,
    TelemetryEvent,
    TopologyReportEvent,
)


class SimulatedGateway:
    def __init__(self, engine: Engine, bridge: BridgeConfig | None = None) -> None:
        self._engine = engine
        self.bridge = CBridge(engine, bridge or BridgeConfig(), self._to_laptop)
        self._handlers: list[GatewayHandler] = []
        self._originate: Callable[[RouteUpdateCommand], None] | None = None
        # False models a controller that cannot see the network or command it (Phase 4b test
        # aid). Events and commands are discarded, not buffered; buffering is Phase 5.
        self.connected = True
        self.events_lost = 0
        self.commands_lost = 0
        self.commands_sent = 0  # route updates actually handed to node C
        self.heartbeats_sent = 0
        # Test aid: the heartbeat path alone is cut while the up path still works.
        self.heartbeats_blocked = False

    # ---- Gateway interface ----

    def subscribe(self, handler: GatewayHandler) -> None:
        self._handlers.append(handler)

    def now_ms(self) -> int:
        return self._engine.now_ms

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self._engine.schedule(delay_ms, callback)

    def send_route_update(self, command: RouteUpdateCommand) -> None:
        if self._originate is None:
            raise RuntimeError("gateway is not attached to a node")
        if not self.connected:
            self.commands_lost += 1
            return
        self.commands_sent += 1
        self._originate(command)

    def send_heartbeat(self, heartbeat: Heartbeat) -> None:
        if not self.connected or self.heartbeats_blocked:
            self.commands_lost += 1
            return
        self.heartbeats_sent += 1
        self._engine.schedule(0, self.bridge.heartbeat)

    def _to_laptop(self, event: GatewayEvent) -> None:
        """Hand one message to the serial line. The laptop's clock is `now`; C's age is the
        only thing carried about when it happened."""
        if not self.connected:
            self.events_lost += 1
            return
        age = getattr(event, "age_ms", 0)
        if age:
            event = replace(event, received_ms=self._engine.now_ms - age)  # type: ignore[type-var]
        for handler in list(self._handlers):
            handler(event)

    # ---- node side ----

    def attach_originator(self, originate: Callable[[RouteUpdateCommand], None]) -> None:
        """Node C registers how it turns a command into a frame."""
        self._originate = originate

    def deliver_reading(self, event: ReadingEvent) -> None:
        """Node C reports a DATA frame it accepted, with its own link-layer evidence."""
        self.bridge.up(event)

    def deliver(self, frame: Frame) -> None:
        """Node C passes a received management frame up, as it would write a serial line."""
        payload = frame.payload
        event: TopologyReportEvent | RouteInstallAckEvent | LocalFailoverEvent | TelemetryEvent
        if isinstance(payload, TopologyReportPayload):
            event = TopologyReportEvent(
                received_ms=self._engine.now_ms,
                origin=frame.src,
                session=frame.session,
                seq=frame.seq,
                neighbors=tuple(
                    NeighborObservation(
                        neighbor=e.neighbor,
                        state=NeighborCode(e.state).name.lower(),
                        session=e.session,
                        last_heard_ms_ago=e.age_ms,
                        rtt_ms=e.rtt_ms,
                    )
                    for e in payload.entries
                ),
            )
        elif isinstance(payload, RouteInstallAckPayload):
            event = RouteInstallAckEvent(
                received_ms=self._engine.now_ms,
                origin=frame.src,
                session=frame.session,
                seq=frame.seq,
                epoch=payload.epoch,
                version=payload.version,
                destination=payload.destination,
                status=RouteAckStatus(payload.status).name.lower(),
            )
        elif isinstance(payload, LocalFailoverReportPayload):
            event = LocalFailoverEvent(
                received_ms=self._engine.now_ms,
                origin=frame.src,
                session=frame.session,
                seq=frame.seq,
                epoch=payload.epoch,
                version=payload.version,
                destination=payload.destination,
                from_next_hop=payload.from_next_hop,
                to_next_hop=payload.to_next_hop,
                reason=FailoverReason(payload.reason).name.lower(),
            )
        elif isinstance(payload, TelemetryPayload):
            event = TelemetryEvent(
                received_ms=self._engine.now_ms,
                origin=frame.src,
                session=frame.session,
                seq=frame.seq,
                created=payload.created,
                acknowledged=payload.acknowledged,
                lost=payload.lost,
                no_route=payload.no_route,
                delivered=payload.delivered,
                forwarded=payload.forwarded,
                dropped=payload.dropped,
                failovers=payload.failovers,
                send_failures=payload.send_failures,
            )
        else:
            return
        self.bridge.up(event)
