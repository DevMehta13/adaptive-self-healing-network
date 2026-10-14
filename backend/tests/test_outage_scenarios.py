"""Phase 5b scenarios: the controller goes away and comes back; C holds and replays.

Ground truth (what C actually accepted, from the simulator's trace) is compared with what the
controller's ledger received. The controller side never reads C's buffer; the tests may.
"""

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from iot_backend.controller import GatewayLinkEvent
from iot_backend.simulator import Simulation
from iot_backend.simulator.scenario_file import ControllerHost, apply_actions, load_scenario
from iot_backend.simulator.trace import (
    ACK_RECEIVED,
    READING_DELIVERED,
    ROUTE_INSTALLED,
)
from iot_backend.transports import ReplayEndEvent, ReplayStartEvent

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"
PHASE_5B = [
    "controller-outage-replay",
    "outage-with-failover",
    "buffer-overflow",
    "coalescing",
    "stale-replay",
    "heartbeat-timeout",
]


@dataclass
class Outage:
    sim: Simulation
    host: ControllerHost
    spec: Any
    records: list = field(default_factory=list)
    links: list[GatewayLinkEvent] = field(default_factory=list)
    markers: list = field(default_factory=list)  # ReplayStart / ReplayEnd as C sent them

    def run(self, until_ms: int) -> "Outage":
        self.sim.run(until_ms - self.sim.now_ms)
        return self

    def delivered_at_c(self, start_ms: int = 0, end_ms: int = 10**12):
        return [
            e
            for e in self.sim.trace.of_type(READING_DELIVERED)
            if e.node == "C" and start_ms <= e.time_ms <= end_ms
        ]


def build(name: str, seed: int | None = None, scripted: bool = True, **overrides: Any) -> Outage:
    spec = load_scenario(SCENARIOS / f"{name}.json")
    if seed is not None:
        spec.config.seed = seed
    for key, value in overrides.items():
        setattr(spec.config, key, value)
    sim = Simulation(spec.config)
    holder = Outage(sim, None, spec)  # type: ignore[arg-type]

    def on_start(view, controller, session, ledger):
        ledger.subscribe(holder.records.append)
        session.subscribe(holder.links.append)

    holder.host = ControllerHost(sim, spec.controller, on_start=on_start)
    sim.gateway.subscribe(
        lambda e: (
            holder.markers.append(e) if isinstance(e, (ReplayStartEvent, ReplayEndEvent)) else 0
        )
    )
    if scripted:
        apply_actions(sim, spec.actions, holder.host)
    return holder


def identity(event) -> tuple[int, int, int]:
    return (event.identity.src, event.identity.session, event.identity.seq)


def route_events(o: Outage, type_: str):
    return [e for e in o.host.route_events if e.type == type_]


# ---- every scenario ----


@pytest.mark.parametrize("name", PHASE_5B)
def test_scenarios_are_labelled_and_run_with_the_heartbeat_on(name):
    spec = load_scenario(SCENARIOS / f"{name}.json")
    assert "SIMULATED SETTING, not a measurement" in spec.description
    assert spec.controller.heartbeat_interval_ms == 1000
    assert spec.config.controller_timeout_ms == 3000
    assert {a["action"] for a in spec.actions} <= {"detach_gateway", "attach_gateway", "stop_node"}


@pytest.mark.parametrize("name", PHASE_5B)
def test_same_seed_gives_an_identical_trace_records_and_markers(name):
    first = build(name).run(build(name).spec.duration_ms)
    second = build(name).run(first.spec.duration_ms)
    assert first.sim.trace.events == second.sim.trace.events
    assert first.records == second.records
    assert first.markers == second.markers
    assert first.host.route_events == second.host.route_events


# ---- controller-outage-replay ----


def test_every_reading_made_during_the_outage_is_replayed_once_and_none_is_doubled():
    o = build("controller-outage-replay").run(60_000)
    outage = o.delivered_at_c(15_000, 35_000)
    assert len(outage) >= 8
    ledger_ids = [(r.origin, r.session, r.seq) for r in o.records]
    assert len(ledger_ids) == len(set(ledger_ids))  # nothing counted twice
    assert set(identity(e) for e in outage) <= set(ledger_ids)  # nothing made during it is missing
    assert len(o.records) == len(o.delivered_at_c())  # and nothing was invented either
    outage_ids = {identity(e) for e in outage}
    late = [r for r in o.records if (r.origin, r.session, r.seq) in outage_ids]
    assert late and all(r.replayed for r in late)
    # the estimated original time is the time C accepted it (an age, not a timestamp)
    arrived = {identity(e): e.time_ms for e in outage}
    for r in late:
        assert r.received_ms == arrived[(r.origin, r.session, r.seq)]
        assert r.delay_ms >= 0
    assert o.host.ledger.unique_replayed == len(late)


def test_c_keeps_forwarding_and_acknowledging_while_the_controller_is_away():
    o = build("controller-outage-replay").run(35_000)
    in_outage = o.delivered_at_c(15_000, 35_000)
    acks = [e for e in o.sim.trace.of_type(ACK_RECEIVED) if 15_000 <= e.time_ms <= 35_000]
    assert len(in_outage) >= 8 and len(acks) >= len(in_outage) - 1  # fail-static on the radio
    assert o.sim.summary().lost == 0


def test_the_laptop_sees_the_link_lost_then_restored_and_the_replay_markers():
    o = build("controller-outage-replay").run(60_000)
    kinds = [e.kind for e in o.links]
    assert kinds == ["link_lost", "link_restored", "replay_started", "replay_finished"]
    lost, restored, started, finished = o.links
    assert lost.data["since_ms"] == pytest.approx(15_000, abs=1_100)  # the last time it heard C
    assert 17_000 < lost.time_ms <= 19_500  # noticed within the link timeout
    assert restored.time_ms >= 35_000 and restored.data["lost_for_ms"] > 18_000
    assert started.data["entries"] >= started.data["held"] > 0
    assert finished.data["replayed"] >= started.data["entries"]  # plus any that arrived meanwhile
    assert finished.data["dropped_overflow"] == 0
    # the markers come in order around the entries
    start_marker = next(m for m in o.markers if isinstance(m, ReplayStartEvent))
    end_marker = next(m for m in o.markers if isinstance(m, ReplayEndEvent))
    assert start_marker.received_ms <= end_marker.received_ms


# ---- outage-with-failover ----


def test_a_failover_during_the_outage_is_learned_from_the_replay_and_confirmed():
    o = build("outage-with-failover").run(70_000)
    failover_at_a = [e for e in o.sim.trace.events if e.event == "local_failover" and e.node == "A"]
    assert len(failover_at_a) == 1 and 20_000 < failover_at_a[0].time_ms < 24_000
    seen = route_events(o, "local_failover")
    assert len(seen) == 1 and seen[0].time_ms >= 40_000  # learned only after reattachment
    assert seen[0].data["node"] == "A" and seen[0].data["to"] == "D"
    confirmed = [
        e for e in route_events(o, "route_active") if e.data["trigger"] == "local_failover_report"
    ]
    assert confirmed and confirmed[0].data["path"] == ["A", "D", "C"]
    # never fought: A was not sent back to B at any point
    installs = [e for e in o.sim.trace.of_type(ROUTE_INSTALLED) if e.node == "A"]
    assert all(e.info["next_hop"] != "B" for e in installs if e.time_ms > failover_at_a[0].time_ms)
    assert o.sim.forwarding_state("A", "C")["primary"] == "D"
    # readings kept arriving over D on the radio side while the controller was away
    during = o.delivered_at_c(failover_at_a[0].time_ms + 500, 40_000)
    assert len(during) >= 6


# ---- buffer-overflow ----


def test_a_long_outage_drops_the_oldest_and_counts_them_and_memory_stays_bounded():
    o = build("buffer-overflow")
    peak = 0
    for second in range(1, 76):
        o.run(second * 1000)
        peak = max(peak, o.sim.gateway.bridge.size)
    assert peak <= 20  # the configured capacity, always
    bridge = o.sim.gateway.bridge
    # at the end of the outage the buffer holds readings only: reports and telemetry gave way
    kinds = {kind for kind, _, _ in bridge.slots()}
    assert kinds == {"reading"}
    assert bridge.counters.dropped_overflow > 200
    o.run(100_000)
    outage = o.delivered_at_c(15_000, 75_000)
    got = {(r.origin, r.session, r.seq): r for r in o.records}
    made = [identity(e) for e in outage]
    delivered = [i for i in made if i in got]
    lost = [i for i in made if i not in got]
    assert len(delivered) >= 15 and lost
    # every loss is a dropped entry that was counted (drops also include reports)
    assert len(lost) <= bridge.counters.dropped_overflow
    # the oldest went first: everything lost is older than everything that survived
    order = {identity(e): e.time_ms for e in outage}
    assert max(order[i] for i in lost) < min(order[i] for i in delivered)
    start = next(m for m in o.markers if isinstance(m, ReplayStartEvent))
    assert start.dropped_overflow >= len(lost)
    assert o.records and len({(r.origin, r.session, r.seq) for r in o.records}) == len(o.records)


# ---- coalescing ----


def test_many_reports_during_an_outage_occupy_one_slot_per_origin():
    o = build("coalescing").run(34_900)  # just before the reattachment
    bridge = o.sim.gateway.bridge
    slots = bridge.slots()
    reports = [key for kind, key, _ in slots if kind == "report"]
    telemetry = [key for kind, key, _ in slots if kind == "telemetry"]
    assert len(reports) == len({k[1] for k in reports}) <= 4
    assert len(telemetry) == len({k[1] for k in telemetry}) <= 4
    assert bridge.counters.coalesced > 80  # dozens of reports and telemetry were superseded
    assert bridge.size < 40  # while readings (every 2 s) are the bulk
    o.run(50_000)
    replayed_reports = [e for e in o.sim.trace.events if e.event == "frame_sent"]  # sanity only
    assert replayed_reports
    start = next(m for m in o.markers if isinstance(m, ReplayStartEvent))
    assert start.coalesced > 80


# ---- stale-replay ----


def test_replayed_old_topology_does_not_make_links_look_fresh_or_undo_newer_state():
    o = build("stale-replay").run(49_000)
    view = o.host.view
    assert {n["status"] for n in view.snapshot()["nodes"]} == {"stale"}  # blind for 40 s
    o.run(55_000)  # reattached at 50 s: the held reports (made at 31 s) are replayed
    snapshot = view.snapshot()
    ages = {n["id"]: n["last_report_age_ms"] for n in snapshot["nodes"]}
    assert all(age is not None and age > 15_000 for age in ages.values()), ages
    assert {n["status"] for n in snapshot["nodes"]} == {"stale"}  # old information stays old
    assert not [link for link in snapshot["links"] if link["state"] == "up"]
    # the controller did not panic: the route stays and no "no route" was announced
    assert route_events(o, "no_available_route") == []
    assert o.host.controller.route_state()["state"] == "active"
    o.run(70_000)  # the next periodic reports (about 61 s) are fresh
    assert {n["status"] for n in view.snapshot()["nodes"]} == {"reachable"}


# ---- heartbeat-timeout ----


def test_c_declares_the_controller_unreachable_within_the_timeout_and_recovers():
    o = build("heartbeat-timeout")
    bridge = o.sim.gateway.bridge
    went_unreachable = came_back = None
    previous = True
    for tenth in range(1, 601):
        o.run(tenth * 100)
        now = o.sim.now_ms
        if previous and not bridge.reachable and went_unreachable is None and now > 20_000:
            went_unreachable = now
        if not previous and bridge.reachable and now >= 40_000 and came_back is None:
            came_back = now
        previous = bridge.reachable
    # last heartbeat landed within a second before the detach at 20 s, so the 3 s timeout
    # expires between 22 s and 23 s
    assert went_unreachable is not None and 22_000 <= went_unreachable <= 23_100
    assert came_back is not None and 40_000 <= came_back <= 41_100  # first heartbeat after attach
    assert bridge.counters.outages == 1


def test_a_cut_heartbeat_path_with_a_working_up_path_replays_without_double_counting():
    """C thinks the controller is gone, the laptop is in fact still listening: entries C
    already sent up are replayed, and the controller counts every reading once."""
    o = build("controller-outage-replay", scripted=False)  # no detach: only the heartbeat path
    o.sim.call_at(15_000, lambda: setattr(o.sim.gateway, "heartbeats_blocked", True))
    o.sim.call_at(35_000, lambda: setattr(o.sim.gateway, "heartbeats_blocked", False))
    o.run(60_000)
    keys = [(r.origin, r.session, r.seq) for r in o.records]
    assert len(keys) == len(set(keys)) == len(o.delivered_at_c())
    assert o.host.ledger.repeats_seen_again >= 1  # some arrived twice, and were counted once
    assert o.sim.gateway.events_lost == 0  # the up path never failed


# ---- structure ----


def test_the_controller_side_never_reads_c_buffer():
    import iot_backend.controller as controller_pkg
    import iot_backend.live.runtime as runtime

    for path in Path(controller_pkg.__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        assert "bridge" not in names and "CBridge" not in names, path.name
    text = Path(runtime.__file__).read_text(encoding="utf-8")
    assert ".bridge" not in text and "CBridge" not in text
