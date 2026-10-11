"""Load a repeatable scenario from a JSON file (see scenarios/ at the repository root).

The format is strict: unknown keys are errors, so a typo cannot silently change an experiment.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iot_backend.simulator.link import LinkConfig
from iot_backend.simulator.scenario import (
    LINK_KEYS,
    NODE_LETTERS,
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
}
_SETTING_KEYS = {
    "reading_interval_ms",
    "ack_timeout_ms",
    "max_attempts",
    "ttl",
    "queue_capacity",
    "service_ms",
}
_LINK_KEYS = {"delay_ms", "loss", "drop_frames", "down"}
_ACTION_KEYS = {
    "stop_node": {"node"},
    "start_node": {"node"},
    "restart_node": {"node"},
    "set_route": {"node", "dst", "next_hop"},
    "set_permitted": {"node", "neighbors"},
    "link_down": {"link", "down"},
    "set_link": {"link", "delay_ms", "loss"},
    "inject_frame": {"node", "from", "hex"},
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
        setattr(config, key, int(value))

    reading = raw.get("reading", {})
    _check_keys("reading", set(reading), {"from", "to"})
    config.reading_src = reading.get("from", config.reading_src)
    config.reading_dst = reading.get("to", config.reading_dst)

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
    )


def apply_actions(sim: Simulation, actions: list[dict[str, Any]]) -> None:
    """Schedule a scenario's timed actions on a simulation."""
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


__all__ = [
    "NODE_LETTERS",
    "ScenarioError",
    "ScenarioSpec",
    "apply_actions",
    "load_scenario",
    "parse_link",
    "parse_scenario",
]
