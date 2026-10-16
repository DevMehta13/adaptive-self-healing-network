"""What the dashboard is shown, in both modes: the controller's and C's knowledge.

`DashboardModel` consumes only a gateway (the laptop's side of the link to node C) and the
controller's own outputs. It never reads simulator state, so the same code serves the
simulation (`runtime.LiveSimulation`) and real boards (`hardware.LiveHardware`). The runtimes
supply the clock, the label, the presenter section and the gateway.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from iot_backend.controller import (
    GatewayLinkEvent,
    GatewaySession,
    ReadingLedger,
    ReadingRecord,
    RouteController,
    TopologyView,
)
from iot_backend.controller.routing import RouteEvent
from iot_backend.controller.topology import TopologyEvent
from iot_backend.protocol import NODE_NAMES
from iot_backend.transports import GatewayEvent, TelemetryEvent, UplinkLossEvent
from iot_backend.transports.laptop_gateway import LaptopGateway
from iot_backend.transports.serial_gateway import SerialGateway

NODE_LETTERS = ("A", "B", "C", "D")


@dataclass(frozen=True)
class LiveEvent:
    event_id: int
    type: str
    data: dict[str, Any]


def _name(node: int) -> str:
    return NODE_NAMES.get(node, str(node))


class DashboardModel:
    mode = "simulation"
    label = ""

    def __init__(self, history: int, recent_readings: int, recent_events: int) -> None:
        self._subscribers: list[Callable[[LiveEvent], None]] = []
        self._next_event_id = 1
        self._history: deque[LiveEvent] = deque(maxlen=history)
        self._recent_readings = recent_readings
        self._recent_events = recent_events
        self.run_id = 0
        self.speed = 1.0
        self.paused = False
        self.scenario: str | None = None
        self._reset_model()

    def _reset_model(self) -> None:
        """Forget everything learned (a new run, or a new controller process)."""
        self._view: TopologyView | None = None
        self._controller: RouteController | None = None
        self._session: GatewaySession | None = None
        self._ledger: ReadingLedger | None = None
        self._telemetry: dict[str, dict[str, Any]] = {}
        self._readings: deque[dict[str, Any]] = deque(maxlen=self._recent_readings)
        self._delivered_unique = 0
        self._replayed = 0
        self._last_reading_ms: int | None = None

    # ---- supplied by the runtime ----

    @property
    def sim_ms(self) -> int:
        """The clock every event is stamped with (simulated time, or the laptop's in hardware)."""
        raise NotImplementedError

    def _laptop_gateway(self) -> LaptopGateway | None:
        return None

    def _presenter(self) -> dict[str, Any]:
        raise NotImplementedError

    # ---- wiring ----

    def _on_controller(
        self,
        view: TopologyView,
        controller: RouteController | None,
        session: GatewaySession | None = None,
        ledger: ReadingLedger | None = None,
    ) -> None:
        self._view = view
        self._controller = controller
        self._session = session
        self._ledger = ledger
        view.subscribe(self._on_topology_event)
        if controller is not None:
            controller.subscribe(self._on_route_event)
        if session is not None:
            session.subscribe(self._on_link_event)
        if ledger is not None:
            ledger.subscribe(self._on_reading_record)

    # ---- events ----

    @property
    def last_event_id(self) -> int:
        return self._next_event_id - 1

    @property
    def oldest_event_id(self) -> int | None:
        return self._history[0].event_id if self._history else None

    def subscribe(self, callback: Callable[[LiveEvent], None]) -> Callable[[], None]:
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback) if callback in self._subscribers else None

    def _emit(self, type_: str, data: dict[str, Any]) -> LiveEvent:
        event = LiveEvent(self._next_event_id, type_, {"sim_ms": self.sim_ms, **data})
        self._next_event_id += 1
        self._history.append(event)
        for callback in list(self._subscribers):
            callback(event)
        return event

    def events_after(self, after_id: int, limit: int = 200) -> tuple[list[LiveEvent], bool]:
        """Events with id > after_id, oldest first, and whether the answer is complete.

        Incomplete means the history no longer reaches back to `after_id`: fetch the snapshot.
        """
        oldest = self.oldest_event_id
        complete = oldest is None or after_id + 1 >= oldest
        selected = [e for e in self._history if e.event_id > after_id][:limit]
        return selected, complete

    def _on_topology_event(self, event: TopologyEvent) -> None:
        self._emit(event.type, dict(event.data))

    def _on_route_event(self, event: RouteEvent) -> None:
        self._emit(event.type, dict(event.data))

    def _on_gateway_event(self, event: GatewayEvent) -> None:
        if isinstance(event, TelemetryEvent):
            self._on_telemetry(event)
        elif isinstance(event, UplinkLossEvent):
            # The laptop's own finding: numbered messages from C that will never arrive.
            self._emit(
                "uplink_loss",
                {
                    "reason": event.reason,
                    "c_boot": event.boot,
                    "first": event.first,
                    "last": event.last,
                    "count": event.count,
                    "readings": event.readings,
                    "unknown": event.unknown,
                    "lower_bound": event.reason == "c_restarted",
                },
            )

    def _on_reading_record(self, record: ReadingRecord) -> None:
        """A reading counted once by the ledger (the controller side), with C's evidence."""
        origin, via = _name(record.origin), _name(record.via)
        # Only what node C observed: the neighbor that handed it the frame and the TTL left.
        # Relays further back are unknown to C, so they are null, not guessed.
        if record.hops <= 1:
            path: list[str | None] = [origin, "C"]
        else:
            path = [origin, *([None] * (record.hops - 2)), via, "C"]
        data = {
            "origin": origin,
            "session": record.session,
            "seq": record.seq,
            "value": record.value / 100,
            "value_hundredths": record.value,
            "via": via,
            "hops": record.hops,
            "ttl": record.ttl,
            "observed_path": path,
            "received_ms": record.received_ms,
            "replayed": record.replayed,
            "delay_ms": record.delay_ms,
        }
        self._delivered_unique += 1
        if record.replayed:
            self._replayed += 1
        self._last_reading_ms = max(self._last_reading_ms or 0, record.received_ms)
        self._readings.append(data)
        self._emit("reading_received", data)

    def _on_link_event(self, event: GatewayLinkEvent) -> None:
        """What the laptop knows about its link to C (the controller-view channel)."""
        names = {
            "link_lost": "gateway_link_lost",
            "link_restored": "gateway_link_restored",
            "replay_started": "replay_started",
            "replay_finished": "replay_finished",
        }
        self._emit(names[event.kind], dict(event.data))

    def _on_telemetry(self, event: TelemetryEvent) -> None:
        counters = {
            "created": event.created,
            "acknowledged": event.acknowledged,
            "lost": event.lost,
            "no_route": event.no_route,
            "delivered": event.delivered,
            "forwarded": event.forwarded,
            "dropped": event.dropped,
            "failovers": event.failovers,
            "send_failures": event.send_failures,
        }
        node = _name(event.origin)
        held = self._telemetry.get(node)
        if held is not None and event.received_ms < held["received_ms"]:
            return  # a replayed counter set older than the one already shown
        self._telemetry[node] = {
            "session": event.session,
            "counters": counters,
            "received_ms": event.received_ms,
        }
        self._emit(
            "telemetry",
            {
                "node": node,
                "session": event.session,
                "counters": counters,
                "received_ms": event.received_ms,
            },
        )

    # ---- snapshot ----

    def _nodes(self) -> list[dict[str, Any]]:
        known: dict[str, dict[str, Any]] = {}
        if self._view is not None:
            for node in self._view.snapshot()["nodes"]:
                known[node["id"]] = dict(node)
        now = self.sim_ms
        out = []
        for name in NODE_LETTERS:
            entry = known.get(
                name,
                {
                    "id": name,
                    "status": "unknown",
                    "since_ms": 0,
                    "session": None,
                    "last_report_age_ms": None,
                },
            )
            t = self._telemetry.get(name)
            entry["telemetry"] = (
                None
                if t is None
                else {
                    "session": t["session"],
                    "counters": dict(t["counters"]),
                    "received_ms": t["received_ms"],
                    "age_ms": now - t["received_ms"],
                    "known_by": f"{name} (own counters, delivered by flooding)",
                }
            )
            out.append(entry)
        return out

    def _counts(self) -> dict[str, Any]:
        now = self.sim_ms
        counts: dict[str, Any] = {
            "delivered": {
                "value": self._delivered_unique,
                "known_by": "C",
                "as_of_ms": self._last_reading_ms if self._last_reading_ms is not None else 0,
                "age_ms": None if self._last_reading_ms is None else now - self._last_reading_ms,
            },
            "duplicates": {
                "value": self._ledger.repeats_from_c + self._ledger.repeats_seen_again
                if self._ledger is not None
                else 0,
                "known_by": "C and the controller",
            },
            "delivered_late": {
                "value": self._replayed,
                "known_by": "C (held while the controller was unreachable)",
            },
        }
        source = self._telemetry.get("A")
        for key in ("created", "acknowledged", "lost", "no_route"):
            counts[key] = (
                None
                if source is None
                else {
                    "value": source["counters"][key],
                    "known_by": "A (telemetry)",
                    "as_of_ms": source["received_ms"],
                    "age_ms": now - source["received_ms"],
                }
            )
        if source is not None:
            c = source["counters"]
            counts["pending"] = {
                "value": max(0, c["created"] - c["acknowledged"] - c["lost"] - c["no_route"]),
                "known_by": "A (telemetry, derived)",
                "as_of_ms": source["received_ms"],
                "age_ms": now - source["received_ms"],
            }
        else:
            counts["pending"] = None
        return counts

    def _gateway_state(self) -> dict[str, Any]:
        """The controller's knowledge of its link to C (never the simulator's flag)."""
        if self._session is None or not self._session.active:
            described: dict[str, Any] = {
                "state": "unmonitored",
                "since_ms": 0,
                "last_heard_ms": None,
                "note": "no heartbeat is configured, so the controller cannot tell",
            }
        else:
            described = self._session.describe()
            described["network_view"] = {
                "lost": "stale: nothing has arrived from C since the link was lost",
                "connecting": "waiting: nothing has arrived from C yet",
            }.get(described["state"], "live")
        gateway = self._laptop_gateway()
        if gateway is not None:
            described["uplink"] = gateway.describe()
            if isinstance(gateway, SerialGateway):
                described["serial"] = gateway.describe_serial()
        return described

    def snapshot(self) -> dict[str, Any]:
        view = self._view.snapshot() if self._view is not None else {"links": []}
        route = (
            self._controller.route_state()
            if self._controller is not None
            else {"state": "no_controller", "kind": None, "version": None}
        )
        recent = list(self._history)[-self._recent_events :]
        return {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "label": self.label,
            "last_event_id": self.last_event_id,
            "clock": {"sim_ms": self.sim_ms, "speed": self.speed, "paused": self.paused},
            "nodes": self._nodes(),
            "links": view["links"],
            "route": route,
            "gateway": self._gateway_state(),
            "readings": {
                "delivered_unique": self._delivered_unique,
                "duplicates": (
                    self._ledger.repeats_from_c + self._ledger.repeats_seen_again
                    if self._ledger is not None
                    else 0
                ),
                "replayed": self._replayed,
                "recent": list(self._readings),
            },
            "counts": self._counts(),
            "presenter": self._presenter(),
            "recent_events": [
                {"event_id": e.event_id, "type": e.type, "data": e.data} for e in recent
            ],
        }

    @property
    def controller(self) -> RouteController | None:
        return self._controller

    @property
    def view(self) -> TopologyView | None:
        return self._view
