"""The simulated gateway: virtual node C handing events to the controller.

It implements `iot_backend.transports.Gateway`. The controller receives plain events and the
clock; it never receives node objects, so it cannot read simulator internals. Commands go the
other way as plain `RouteUpdateCommand` values: C turns each into a management frame that it
originates on the controller's behalf (the frame's source is C).
"""

from collections.abc import Callable

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
from iot_backend.simulator.engine import Engine
from iot_backend.transports import (
    GatewayHandler,
    LocalFailoverEvent,
    NeighborObservation,
    ReadingEvent,
    RouteInstallAckEvent,
    RouteUpdateCommand,
    TelemetryEvent,
    TopologyReportEvent,
)


class SimulatedGateway:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._handlers: list[GatewayHandler] = []
        self._originate: Callable[[RouteUpdateCommand], None] | None = None
        # False models a controller that cannot see the network or command it (Phase 4b test
        # aid). Events and commands are discarded, not buffered; buffering is Phase 5.
        self.connected = True
        self.events_lost = 0
        self.commands_lost = 0
        self.commands_sent = 0  # route updates actually handed to node C

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

    # ---- node side ----

    def attach_originator(self, originate: Callable[[RouteUpdateCommand], None]) -> None:
        """Node C registers how it turns a command into a frame."""
        self._originate = originate

    def deliver_reading(self, event: ReadingEvent) -> None:
        """Node C reports a DATA frame it accepted, with its own link-layer evidence."""
        if not self.connected:
            self.events_lost += 1
            return
        for handler in list(self._handlers):
            handler(event)

    def deliver(self, frame: Frame) -> None:
        """Node C passes a received management frame up, as it would write a serial line."""
        payload = frame.payload
        if not self.connected:
            self.events_lost += 1
            return
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
        for handler in list(self._handlers):
            handler(event)
