"""The simulated gateway: virtual node C handing events to the controller.

It implements `iot_backend.transports.Gateway`. The controller receives plain events and the
clock; it never receives node objects, so it cannot read simulator internals.
"""

from collections.abc import Callable

from iot_backend.protocol import Frame, NeighborCode, TopologyReportPayload
from iot_backend.simulator.engine import Engine
from iot_backend.transports import GatewayHandler, NeighborObservation, TopologyReportEvent


class SimulatedGateway:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._handlers: list[GatewayHandler] = []

    # ---- Gateway interface ----

    def subscribe(self, handler: GatewayHandler) -> None:
        self._handlers.append(handler)

    def now_ms(self) -> int:
        return self._engine.now_ms

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self._engine.schedule(delay_ms, callback)

    # ---- node side ----

    def deliver(self, frame: Frame) -> None:
        """Node C passes a received management frame up, as it would write a serial line."""
        payload = frame.payload
        if not isinstance(payload, TopologyReportPayload):
            return
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
                )
                for e in payload.entries
            ),
        )
        for handler in list(self._handlers):
            handler(event)
