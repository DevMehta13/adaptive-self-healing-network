"""The live simulation: the discrete-event simulator paced against the wall clock.

How a discrete-event simulation becomes a live one
--------------------------------------------------
The engine knows nothing about real time: it has a queue of future events and a virtual clock
that jumps from one event to the next. To run it live we do not change it. A small task wakes
every few tens of milliseconds, measures how much real time has passed, multiplies it by the
speed factor, and tells the engine "run until your virtual clock has advanced by that many
milliseconds". Everything the engine does in that slice is as deterministic as before; only
*when* the slices are requested depends on the wall clock. Tests skip the wall clock and ask
for slices directly (`advance`), so they are fast and repeatable.

Three separate parts live here, and only the controller's side is shown to the dashboard:

* the **simulated environment** (nodes, links) - ground truth, never exposed as such;
* the **gateway** on node C - the only channel from the network to the controller;
* the **controller** (topology view and routing) - it learns only through the gateway.

The dashboard model (`LiveSimulation`) is a consumer of the gateway and of the controller's
own outputs. It never reads node, link or trace state. What the *presenter* did to the
environment is published on a separate, labelled `environment` channel.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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
from iot_backend.simulator import Simulation
from iot_backend.simulator.scenario import NODE_LETTERS
from iot_backend.simulator.scenario_file import (
    ControllerHost,
    ScenarioError,
    apply_actions,
    load_scenario,
    parse_link,
)
from iot_backend.transports import GatewayEvent, TelemetryEvent

MIN_SPEED = 0.1
MAX_SPEED = 1000.0
SIMULATION_LABEL = (
    "SIMULATION: nodes, links and delays are simulated. Link delays are configured settings "
    "(B's links are set cheaper than D's), not measurements."
)


class ControlError(Exception):
    """A presenter request that cannot be applied. `code` is stable, `status` is the HTTP code."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class LiveConfig:
    scenarios_dir: Path
    scenario: str = "live-demo"
    speed: float = 1.0
    tick_s: float = 0.05  # how often the pacing task wakes
    max_advance_ms: int = 5_000  # most simulated time served in one wake-up
    history: int = 2_000  # state-changing events kept for catch-up
    recent_readings: int = 50
    recent_events_in_snapshot: int = 100
    trace_max_events: int = 5_000  # simulator trace bound (not shown to the dashboard)


@dataclass(frozen=True)
class LiveEvent:
    event_id: int
    type: str
    data: dict[str, Any]


def _name(node: int) -> str:
    return NODE_NAMES.get(node, str(node))


class LiveSimulation:
    def __init__(self, config: LiveConfig) -> None:
        self._config = config
        self._subscribers: list[Callable[[LiveEvent], None]] = []
        self._next_event_id = 1
        self._history: deque[LiveEvent] = deque(maxlen=config.history)
        self.run_id = 0
        self.speed = config.speed
        self.paused = False
        self._debt_ms = 0.0
        self._task: asyncio.Task[None] | None = None
        self._build(config.scenario)

    # ------------------------------------------------------------------ building

    def scenario_path(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
            raise ControlError("unknown_scenario", f"no scenario named {name!r}", 404)
        path = self._config.scenarios_dir / f"{name}.json"
        if not path.is_file():
            raise ControlError("unknown_scenario", f"no scenario named {name!r}", 404)
        return path

    def scenario_names(self) -> list[str]:
        return sorted(p.stem for p in self._config.scenarios_dir.glob("*.json"))

    def _build(self, name: str) -> None:
        path = self.scenario_path(name)
        try:
            spec = load_scenario(path)
        except ScenarioError as error:
            raise ControlError("bad_scenario", str(error), 400) from error
        spec.config.trace_max_events = self._config.trace_max_events
        self.scenario = name
        self.description = spec.description
        self.run_id += 1
        self._sim = Simulation(spec.config)
        self._view: TopologyView | None = None
        self._controller: RouteController | None = None
        # per-run dashboard model (what the controller and C have learned)
        self._telemetry: dict[str, dict[str, Any]] = {}
        self._readings: deque[dict[str, Any]] = deque(maxlen=self._config.recent_readings)
        self._delivered_unique = 0
        self._replayed = 0
        self._session: GatewaySession | None = None
        self._ledger: ReadingLedger | None = None
        self._last_reading_ms: int | None = None
        self._presenter_conditions: dict[str, Any] = {
            "stopped_nodes": [],
            "links_down": [],
            "link_overrides": {},
            "gateway_attached": True,
        }
        self._sim.gateway.subscribe(self._on_gateway_event)
        self._host: ControllerHost | None = None
        if spec.config.routing == "controller":
            self._host = ControllerHost(self._sim, spec.controller, on_start=self._on_controller)
        elif spec.config.discovery:
            self._on_controller(
                TopologyView(self._sim.gateway), None, None, ReadingLedger(self._sim.gateway)
            )
        apply_actions(self._sim, spec.actions, self._host)
        for action in spec.actions:  # scripted actions are announced as such, on their own channel
            self._sim.call_at(int(action["at_ms"]), lambda a=action: self._announce_scripted(a))

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

    # ------------------------------------------------------------------ events

    @property
    def sim_ms(self) -> int:
        return self._sim.now_ms

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

    # ------------------------------------------------------------------ clock

    def advance(self, ms: int) -> int:
        """Run the simulation forward by `ms` simulated milliseconds. Returns the new time."""
        if ms > 0:
            self._sim.run(int(ms))
        return self.sim_ms

    async def run_forever(self) -> None:
        """Pace the simulation against the wall clock until cancelled."""
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self._config.tick_s)
            now = loop.time()
            elapsed, last = now - last, now
            if self.paused:
                continue
            self._debt_ms += elapsed * 1000.0 * self.speed
            whole = int(self._debt_ms)
            self._debt_ms -= whole
            # If the laptop cannot keep up, simulated time falls behind rather than spiralling.
            self.advance(min(whole, self._config.max_advance_ms))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------ presenter controls
    #
    # These change the simulated environment and nothing else. None of them calls the
    # controller or the topology view: the controller must notice the effect itself, through
    # the gateway, exactly as it would on hardware.

    def _announce(self, action: str, target: str | None, label: str, **detail: Any) -> None:
        self._emit(
            "environment",
            {
                "channel": "environment",
                "source": "presenter",
                "action": action,
                "target": target,
                "detail": detail,
                "label": f"{label} (injected by presenter, simulation)",
            },
        )

    def _announce_scripted(self, action: dict[str, Any]) -> None:
        kind = action["action"]
        target = action.get("node") or action.get("link")
        self._emit(
            "environment",
            {
                "channel": "environment",
                "source": "scenario",
                "action": kind,
                "target": target,
                "detail": {k: v for k, v in action.items() if k not in ("action", "at_ms", "hex")},
                "label": f"{kind} {target or ''} (scripted by scenario, simulation)".replace(
                    "  ", " "
                ),
            },
        )

    def _node(self, name: Any) -> str:
        if not isinstance(name, str) or name.upper() not in NODE_LETTERS:
            raise ControlError("bad_request", f"node must be one of {list(NODE_LETTERS)}")
        return name.upper()

    def _link(self, text: Any) -> tuple[str, str]:
        if not isinstance(text, str):
            raise ControlError("bad_request", "link must be a string such as 'A->B'")
        try:
            return parse_link(text)
        except ScenarioError as error:
            raise ControlError("bad_request", str(error)) from error

    def stop_node(self, node: Any) -> None:
        name = self._node(node)
        self._sim.stop_node(name)
        if name not in self._presenter_conditions["stopped_nodes"]:
            self._presenter_conditions["stopped_nodes"].append(name)
        self._announce("stop_node", name, f"Stop {name}")

    def start_node(self, node: Any) -> None:
        name = self._node(node)
        self._sim.start_node(name)
        stopped = self._presenter_conditions["stopped_nodes"]
        if name in stopped:
            stopped.remove(name)
        self._announce("start_node", name, f"Start {name}")

    def link_down(self, link: Any, down: Any) -> None:
        if not isinstance(down, bool):
            raise ControlError("bad_request", "down must be true or false")
        src, dst = self._link(link)
        self._sim.set_link_down(src, dst, down)
        text, downs = f"{src}->{dst}", self._presenter_conditions["links_down"]
        if down and text not in downs:
            downs.append(text)
        if not down and text in downs:
            downs.remove(text)
        verb = "Take down" if down else "Restore"
        self._announce("link_down", text, f"{verb} {text}", down=down)

    def link_config(self, link: Any, loss: Any = None, delay_ms: Any = None) -> None:
        src, dst = self._link(link)
        if loss is None and delay_ms is None:
            raise ControlError("bad_request", "give loss and/or delay_ms")
        if loss is not None and (
            isinstance(loss, bool) or not isinstance(loss, (int, float)) or not 0 <= loss <= 1
        ):
            raise ControlError("bad_request", "loss must be a number from 0 to 1")
        if delay_ms is not None and (
            isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or not 0 <= delay_ms <= 5000
        ):
            raise ControlError("bad_request", "delay_ms must be an integer from 0 to 5000")
        self._sim.configure_link(
            src, dst, delay_ms=delay_ms, loss=None if loss is None else float(loss)
        )
        text = f"{src}->{dst}"
        overrides = self._presenter_conditions["link_overrides"].setdefault(text, {})
        if loss is not None:
            overrides["loss"] = float(loss)
        if delay_ms is not None:
            overrides["delay_ms"] = delay_ms
        self._announce(
            "link_config",
            text,
            f"Set {text}",
            **({"loss": loss} if loss is not None else {}),
            **({"delay_ms": delay_ms} if delay_ms is not None else {}),
        )

    def set_gateway(self, attached: Any) -> None:
        if not isinstance(attached, bool):
            raise ControlError("bad_request", "attached must be true or false")
        self._sim.set_gateway_connected(attached)
        self._presenter_conditions["gateway_attached"] = attached
        verb = "Attach" if attached else "Detach"
        self._announce("gateway", "C", f"{verb} the gateway", attached=attached)

    def pause(self) -> None:
        if not self.paused:
            self.paused = True
            self._emit("runtime", {"change": "paused", "paused": True, "speed": self.speed})

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            self._emit("runtime", {"change": "resumed", "paused": False, "speed": self.speed})

    def set_speed(self, factor: Any) -> None:
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            raise ControlError("bad_request", "factor must be a number")
        if not MIN_SPEED <= factor <= MAX_SPEED:
            raise ControlError("bad_request", f"factor must be between {MIN_SPEED} and {MAX_SPEED}")
        self.speed = float(factor)
        self._emit("runtime", {"change": "speed", "paused": self.paused, "speed": self.speed})

    def reset(self, scenario: Any) -> None:
        if not isinstance(scenario, str):
            raise ControlError("bad_request", "scenario must be a string")
        self.scenario_path(scenario)  # validate before touching anything
        self._build(scenario)
        self._debt_ms = 0.0
        self._emit("reset", {"run_id": self.run_id, "scenario": self.scenario})

    # ------------------------------------------------------------------ snapshot

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
            return {
                "state": "unmonitored",
                "since_ms": 0,
                "last_heard_ms": None,
                "note": "no heartbeat is configured, so the controller cannot tell",
            }
        described = self._session.describe()
        described["network_view"] = {
            "lost": "stale: nothing has arrived from C since the link was lost",
            "connecting": "waiting: nothing has arrived from C yet",
        }.get(described["state"], "live")
        return described

    def snapshot(self) -> dict[str, Any]:
        view = self._view.snapshot() if self._view is not None else {"links": []}
        route = (
            self._controller.route_state()
            if self._controller is not None
            else {"state": "no_controller", "kind": None, "version": None}
        )
        recent = list(self._history)[-self._config.recent_events_in_snapshot :]
        return {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "label": SIMULATION_LABEL,
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
            "presenter": {
                "channel": "environment",
                "note": (
                    "What the presenter did to the simulated environment. This is not "
                    "something the controller knows."
                ),
                "conditions": {
                    "stopped_nodes": list(self._presenter_conditions["stopped_nodes"]),
                    "links_down": list(self._presenter_conditions["links_down"]),
                    "link_overrides": {
                        k: dict(v) for k, v in self._presenter_conditions["link_overrides"].items()
                    },
                    "gateway_attached": self._presenter_conditions["gateway_attached"],
                },
            },
            "recent_events": [
                {"event_id": e.event_id, "type": e.type, "data": e.data} for e in recent
            ],
        }

    # ------------------------------------------------------------------ introspection for tests

    @property
    def simulation(self) -> Simulation:
        return self._sim

    @property
    def controller(self) -> RouteController | None:
        return self._controller

    @property
    def view(self) -> TopologyView | None:
        return self._view


__all__ = ["MAX_SPEED", "MIN_SPEED", "ControlError", "LiveConfig", "LiveEvent", "LiveSimulation"]
