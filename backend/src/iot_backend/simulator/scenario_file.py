"""Load a repeatable scenario from a JSON file (see scenarios/ at the repository root).

The format is strict: unknown keys are errors, so a typo cannot silently change an experiment.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from iot_backend.controller import (
    GatewaySession,
    ReadingLedger,
    RouteController,
    RoutingConfig,
    TopologyView,
)
from iot_backend.simulator.link import LinkConfig
from iot_backend.simulator.scenario import (
    LINK_KEYS,
    NODE_LETTERS,
    ROUTING_CONTROLLER,
    ROUTING_MANUAL,
    ScenarioConfig,
    Simulation,
)

_TOP_KEYS = {
    "name",
    "description",
    "seed",
    "duration_s",
    "settings",
    "reading",
    "permitted",
    "routes",
    "links",
    "initial_seq",
    "actions",
    "routing",
    "controller",
}
_CONTROLLER_KEYS = {
    "epoch",
    "hold_down_ms",
    "install_timeout_ms",
    "install_max_attempts",
    "replan_delay_ms",
    "max_failed_plans",
    "no_route_grace_ms",
    "initial_settle_ms",
    "backup_next_hops",
    "heartbeat_interval_ms",
    "link_timeout_ms",
    "policy",
    "policy_eval_interval_ms",
    "hysteresis_evaluations",
}
_SETTING_KEYS = {
    "reading_interval_ms",
    "ack_timeout_ms",
    "max_attempts",
    "ttl",
    "queue_capacity",
    "service_ms",
    "discovery",
    "ack_delivery",
    "hello_interval_ms",
    "hello_jitter_ms",
    "neighbor_timeout_ms",
    "report_interval_ms",
    "report_coalesce_ms",
    "flood_cache_size",
    "probe_interval_ms",
    "probe_jitter_ms",
    "rtt_window",
    "rtt_min_samples",
    "rtt_max_age_ms",
    "probe_window",
    "probe_stats_min",
    "max_routes",
    "local_failover",
    "failover_t1",
    "failover_t2_k",
    "send_fail_ms",
    "failover_report_repeats",
    "telemetry_interval_ms",
    "controller_timeout_ms",
    "buffer_capacity",
    "replay_spacing_ms",
}
_BOOL_SETTINGS = {"discovery", "local_failover", "failover_t1"}
_STRING_SETTINGS = {"ack_delivery"}
_LINK_KEYS = {"delay_ms", "loss", "drop_frames", "drop_msg", "down"}
_ACTION_KEYS = {
    "stop_node": {"node"},
    "start_node": {"node"},
    "restart_node": {"node"},
    "set_route": {"node", "dst", "next_hop"},
    "set_permitted": {"node", "neighbors"},
    "link_down": {"link", "down"},
    "set_link": {"link", "delay_ms", "loss"},
    "inject_frame": {"node", "from", "hex"},
    "restart_controller": {"epoch"},
    "detach_gateway": set(),
    "attach_gateway": set(),
}


class ScenarioError(ValueError):
    """The scenario file is invalid."""


@dataclass
class ScenarioSpec:
    name: str
    description: str
    duration_ms: int
    config: ScenarioConfig
    actions: list[dict[str, Any]] = field(default_factory=list)
    controller: RoutingConfig = field(default_factory=RoutingConfig)  # used if routing=controller


def parse_link(text: str) -> tuple[str, str]:
    """Parse "A->B" (or "A-B") into ("A", "B")."""
    parts = text.replace("->", "-").upper().split("-")
    key = (parts[0], parts[-1]) if len(parts) == 2 else ("", "")
    if key not in LINK_KEYS:
        raise ScenarioError(f"link must name two different nodes among A-D, got {text!r}")
    return key


def _check_keys(where: str, given: set[str], allowed: set[str], required: set[str] = frozenset()):
    unknown = given - allowed
    if unknown:
        raise ScenarioError(f"{where}: unknown keys {sorted(unknown)}")
    missing = required - given
    if missing:
        raise ScenarioError(f"{where}: missing keys {sorted(missing)}")


def load_scenario(path: str | Path) -> ScenarioSpec:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioError(f"cannot read scenario {path}: {error}") from error
    return parse_scenario(raw)


def parse_scenario(raw: dict[str, Any]) -> ScenarioSpec:
    _check_keys("scenario", set(raw), _TOP_KEYS, {"name", "duration_s"})
    config = ScenarioConfig(seed=int(raw.get("seed", 0)))

    settings = raw.get("settings", {})
    _check_keys("settings", set(settings), _SETTING_KEYS)
    for key, value in settings.items():
        if key in _BOOL_SETTINGS:
            value = bool(value)
        elif key in _STRING_SETTINGS:
            value = str(value)
        else:
            value = int(value)
        setattr(config, key, value)

    reading = raw.get("reading", {})
    _check_keys("reading", set(reading), {"from", "to"})
    config.reading_src = reading.get("from", config.reading_src)
    config.reading_dst = reading.get("to", config.reading_dst)

    routing = raw.get("routing", ROUTING_MANUAL)
    if routing not in (ROUTING_MANUAL, ROUTING_CONTROLLER):
        raise ScenarioError(f"routing must be 'manual' or 'controller', got {routing!r}")
    config.routing = routing
    if routing == ROUTING_CONTROLLER and "routes" in raw:
        raise ScenarioError("routing 'controller' installs routes itself: remove 'routes'")
    controller_raw = raw.get("controller", {})
    _check_keys("controller", set(controller_raw), _CONTROLLER_KEYS)
    if controller_raw and routing != ROUTING_CONTROLLER:
        raise ScenarioError("a 'controller' section needs routing 'controller'")
    try:
        controller = RoutingConfig(
            **{
                k: (bool(v) if k == "backup_next_hops" else str(v) if k == "policy" else int(v))
                for k, v in controller_raw.items()
            }
        )
    except ValueError as error:
        raise ScenarioError(f"controller: {error}") from error

    if "permitted" in raw:
        config.permitted = {node: set(nbrs) for node, nbrs in raw["permitted"].items()}
    for node, table in raw.get("routes", {}).items():
        config.routes.setdefault(node, {}).update(table)
    config.initial_seq = {node: int(seq) for node, seq in raw.get("initial_seq", {}).items()}

    for link_name, link in raw.get("links", {}).items():
        _check_keys(f"links.{link_name}", set(link), _LINK_KEYS)
        config.links[parse_link(link_name)] = LinkConfig(
            delay_ms=int(link.get("delay_ms", 20)),
            loss=float(link.get("loss", 0.0)),
            drop_frames=frozenset(int(n) for n in link.get("drop_frames", [])),
            drop_msg=link.get("drop_msg"),
            down=bool(link.get("down", False)),
        )

    actions = raw.get("actions", [])
    for i, action in enumerate(actions):
        kind = action.get("action")
        if kind not in _ACTION_KEYS:
            raise ScenarioError(f"actions[{i}]: unknown action {kind!r}")
        _check_keys(
            f"actions[{i}]", set(action), _ACTION_KEYS[kind] | {"action", "at_ms"}, {"at_ms"}
        )
        if kind == "restart_controller" and routing != ROUTING_CONTROLLER:
            raise ScenarioError(f"actions[{i}]: restart_controller needs routing 'controller'")
    try:
        Simulation(config)  # validates names, routes and links up front
    except ValueError as error:
        raise ScenarioError(str(error)) from error

    return ScenarioSpec(
        name=raw["name"],
        description=raw.get("description", ""),
        duration_ms=round(float(raw["duration_s"]) * 1000),
        config=config,
        actions=actions,
        controller=controller,
    )


class ControllerHost:
    """The controller side of a simulated run: a topology view and a route controller on the
    simulation's gateway. It can be restarted, which is a new process: empty view, a new epoch,
    version numbers starting again from 1. It touches the simulation only through its gateway.
    """

    def __init__(
        self,
        sim: Simulation,
        config: RoutingConfig,
        on_start: Callable[[TopologyView, RouteController, GatewaySession, ReadingLedger], None]
        | None = None,
    ) -> None:
        self._sim = sim
        self.config = config
        self._on_start = on_start  # called for the first run and after each restart
        self.views: list[TopologyView] = []
        self.controllers: list[RouteController] = []
        self.sessions: list[GatewaySession] = []
        self.ledgers: list[ReadingLedger] = []
        self._start(config)

    def _start(self, config: RoutingConfig) -> None:
        view = TopologyView(self._sim.gateway)
        self.views.append(view)
        controller = RouteController(view, self._sim.gateway, config)
        self.controllers.append(controller)
        ledger = ReadingLedger(self._sim.gateway)
        self.ledgers.append(ledger)
        # The heartbeat starts with the controller process and ends with it.
        session = GatewaySession(
            self._sim.gateway, config.heartbeat_interval_ms, config.link_timeout_ms
        )
        self.sessions.append(session)
        if self._on_start is not None:
            self._on_start(view, controller, session, ledger)

    @property
    def view(self) -> TopologyView:
        return self.views[-1]

    @property
    def controller(self) -> RouteController:
        return self.controllers[-1]

    @property
    def route_events(self) -> list:
        """Route events of every controller run, in order (ids restart with each run)."""
        return [e for c in self.controllers for e in c.events]

    @property
    def topology_events(self) -> list:
        return [e for v in self.views for e in v.events]

    @property
    def session(self) -> GatewaySession:
        return self.sessions[-1]

    @property
    def ledger(self) -> ReadingLedger:
        return self.ledgers[-1]

    def restart(self, epoch: int) -> None:
        self.controller.stop()
        self.session.stop()  # the heartbeat dies with the process
        self.config = replace(self.config, epoch=epoch)
        self._start(self.config)


def start_controller(sim: Simulation, spec: ScenarioSpec) -> ControllerHost | None:
    """Attach the route controller if the scenario uses controller routing."""
    if spec.config.routing != ROUTING_CONTROLLER:
        return None
    return ControllerHost(sim, spec.controller)


def apply_actions(
    sim: Simulation, actions: list[dict[str, Any]], host: ControllerHost | None = None
) -> None:
    """Schedule a scenario's timed actions on a simulation (and, if given, its controller)."""
    for action in actions:
        at = int(action["at_ms"])
        kind = action["action"]
        if kind == "stop_node":
            sim.stop_node(action["node"], at)
        elif kind == "start_node":
            sim.start_node(action["node"], at)
        elif kind == "restart_node":
            sim.restart_node(action["node"], at)
        elif kind == "set_route":
            sim.set_route(action["node"], action["dst"], action.get("next_hop"), at)
        elif kind == "set_permitted":
            sim.set_permitted(action["node"], set(action["neighbors"]), at)
        elif kind == "link_down":
            src, dst = parse_link(action["link"])
            sim.set_link_down(src, dst, bool(action["down"]), at)
        elif kind == "set_link":
            src, dst = parse_link(action["link"])
            sim.configure_link(src, dst, at, action.get("delay_ms"), action.get("loss"))
        elif kind == "inject_frame":
            sim.inject_frame(action["node"], bytes.fromhex(action["hex"]), at, action["from"])
        elif kind == "detach_gateway":
            sim.set_gateway_connected(False, at)
        elif kind == "attach_gateway":
            sim.set_gateway_connected(True, at)
        elif kind == "restart_controller":
            if host is None:
                raise ScenarioError("restart_controller needs a controller host")
            sim.call_at(at, lambda epoch=int(action["epoch"]): host.restart(epoch))


__all__ = [
    "NODE_LETTERS",
    "ControllerHost",
    "ScenarioError",
    "ScenarioSpec",
    "apply_actions",
    "load_scenario",
    "parse_link",
    "parse_scenario",
    "start_controller",
]
