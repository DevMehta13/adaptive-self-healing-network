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

The dashboard model (`DashboardModel`, model.py) is a consumer of the gateway and of the
controller's own outputs. It never reads node, link or trace state. What the *presenter* did to
the environment is published on a separate, labelled `environment` channel.
"""

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from iot_backend.controller import ReadingLedger, TopologyView
from iot_backend.live.model import NODE_LETTERS, DashboardModel, LiveEvent
from iot_backend.simulator import Simulation
from iot_backend.simulator.scenario_file import (
    ControllerHost,
    ScenarioError,
    apply_actions,
    load_scenario,
    parse_link,
)
from iot_backend.transports.laptop_gateway import LaptopGateway

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
    transport: str = "direct"  # "serial": the simulated C speaks the serial line format
    policy: str = ""  # routing policy override ("" = the scenario's controller section)


class LiveSimulation(DashboardModel):
    mode = "simulation"
    label = SIMULATION_LABEL

    def __init__(self, config: LiveConfig) -> None:
        super().__init__(config.history, config.recent_readings, config.recent_events_in_snapshot)
        self._config = config
        self.speed = config.speed
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
        spec.config.transport = self._config.transport
        self.scenario = name
        self.description = spec.description
        self.run_id += 1
        self._sim = Simulation(spec.config)
        self._reset_model()  # per-run dashboard model (what the controller and C have learned)
        self._presenter_conditions: dict[str, Any] = {
            "stopped_nodes": [],
            "links_down": [],
            "link_overrides": {},
            "gateway_attached": True,
        }
        self._sim.gateway.subscribe(self._on_gateway_event)
        self._host: ControllerHost | None = None
        if spec.config.routing == "controller":
            routing = replace(spec.controller, policy=self._config.policy or spec.controller.policy)
            self._host = ControllerHost(self._sim, routing, on_start=self._on_controller)
        elif spec.config.discovery:
            self._on_controller(
                TopologyView(self._sim.gateway), None, None, ReadingLedger(self._sim.gateway)
            )
        apply_actions(self._sim, spec.actions, self._host)
        for action in spec.actions:  # scripted actions are announced as such, on their own channel
            self._sim.call_at(int(action["at_ms"]), lambda a=action: self._announce_scripted(a))

    # ------------------------------------------------------------------ clock

    @property
    def sim_ms(self) -> int:
        return self._sim.now_ms

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

    # ------------------------------------------------------------------ snapshot parts

    def _presenter(self) -> dict[str, Any]:
        return {
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
        }

    def _laptop_gateway(self) -> LaptopGateway:
        return self._sim.gateway

    @property
    def simulation(self) -> Simulation:
        """For tests only."""
        return self._sim


__all__ = [
    "MAX_SPEED",
    "MIN_SPEED",
    "ControlError",
    "DashboardModel",
    "LiveConfig",
    "LiveEvent",
    "LiveSimulation",
]
