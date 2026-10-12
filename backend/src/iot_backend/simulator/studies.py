"""Simulated comparison studies (Phase 4b): recovery modes and false-failover rates.

Everything here is simulation. The reference time is the scripted failure time, link delays and
loss are configured values, and nothing says what real boards will do.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from iot_backend.simulator.link import LinkConfig
from iot_backend.simulator.measurements import Recovery, recovery_measurement
from iot_backend.simulator.scenario import Simulation
from iot_backend.simulator.scenario_file import (
    ControllerHost,
    apply_actions,
    load_scenario,
)
from iot_backend.simulator.trace import LOCAL_FAILOVER, LOCAL_NO_ROUTE

# The three ways a relay failure can be handled. Each is a set of overrides applied on top of a
# scenario file, so the same scripted failure is replayed under each.
MODES: dict[str, dict[str, object]] = {
    "controller-only": {
        "local_failover": False,
        "failover_t1": True,
        "failover_t2_k": 0,
        "backup_next_hops": False,
    },
    "local failover, T1": {
        "local_failover": True,
        "failover_t1": True,
        "failover_t2_k": 0,
        "backup_next_hops": True,
    },
    "local failover, T1 + T2": {
        "local_failover": True,
        "failover_t1": True,
        "failover_t2_k": 3,
        "backup_next_hops": True,
    },
}


@dataclass
class Run:
    sim: Simulation
    host: ControllerHost
    actions: list
    duration_ms: int


def run_scenario(
    path: str | Path,
    seed: int,
    mode: dict[str, object] | None = None,
    interval_ms: int | None = None,
    duration_ms: int | None = None,
    loss: dict[tuple[str, str], float] | None = None,
    clear_drops: bool = False,
) -> Run:
    """Run a controller-routing scenario with overrides. Used by the studies and the tests."""
    spec = load_scenario(path)
    config = spec.config
    config.seed = seed
    routing = spec.controller
    for key, value in (mode or {}).items():
        if key == "backup_next_hops":
            routing = replace(routing, backup_next_hops=bool(value))
        else:
            setattr(config, key, value)
    if interval_ms is not None:
        config.reading_interval_ms = interval_ms
    links = dict(config.links)
    if clear_drops:
        links = {k: replace(v, drop_frames=frozenset(), drop_msg=None) for k, v in links.items()}
    for key, probability in (loss or {}).items():
        links[key] = replace(links.get(key, LinkConfig()), loss=probability)
    config.links = links
    sim = Simulation(config)
    host = ControllerHost(sim, routing)
    apply_actions(sim, spec.actions, host)
    length = duration_ms if duration_ms is not None else spec.duration_ms
    sim.run(length)
    return Run(sim, host, spec.actions, length)


def measure(run: Run) -> Recovery | None:
    return recovery_measurement(
        run.actions, run.sim.trace.events, run.host.route_events, run.host.topology_events
    )


def compare_modes(
    path: str | Path, seeds: Iterable[int], interval_ms: int | None = None
) -> dict[str, list[Recovery]]:
    """The same scripted failure, replayed under each mode, for each seed."""
    out: dict[str, list[Recovery]] = {name: [] for name in MODES}
    for seed in seeds:
        for name, mode in MODES.items():
            recovery = measure(run_scenario(path, seed, mode, interval_ms))
            if recovery is not None:
                out[name].append(recovery)
    return out


@dataclass(frozen=True)
class FalseTriggerRow:
    loss: float
    k: int
    runs: int
    runs_with_failover: int
    failovers: int
    t1: int
    t2: int
    simulated_hours: float

    @property
    def per_hour(self) -> float:
        return self.failovers / self.simulated_hours if self.simulated_hours else 0.0


def false_triggers(
    path: str | Path,
    seeds: Iterable[int],
    losses: Iterable[float],
    ks: Iterable[int],
    duration_ms: int,
    t1: bool = True,
) -> list[FalseTriggerRow]:
    """Failovers with no failure at all, under random loss on the A-B link (both directions).

    No node is stopped and no link is cut, so every local failover counted here is false. The
    scenario's scripted drops are cleared; only the random loss remains.
    """
    seeds = list(seeds)
    rows = []
    for loss in losses:
        for k in ks:
            counted = with_any = t1_count = t2_count = 0
            for seed in seeds:
                run = run_scenario(
                    path,
                    seed,
                    {**MODES["local failover, T1 + T2"], "failover_t2_k": k, "failover_t1": t1},
                    duration_ms=duration_ms,
                    loss={("A", "B"): loss, ("B", "A"): loss},
                    clear_drops=True,
                )
                events = [
                    e
                    for e in run.sim.trace.events
                    if e.event in (LOCAL_FAILOVER, LOCAL_NO_ROUTE) and e.node == "A"
                ]
                counted += len(events)
                with_any += 1 if events else 0
                t1_count += sum(1 for e in events if e.info["trigger"].startswith("T1"))
                t2_count += sum(1 for e in events if e.info["trigger"].startswith("T2"))
            hours = len(seeds) * duration_ms / 3_600_000
            rows.append(
                FalseTriggerRow(loss, k, len(seeds), with_any, counted, t1_count, t2_count, hours)
            )
    return rows
