"""Phase 4b scenarios: backup next hops and local failover, read from the packet trace.

Every ordering claim here comes from trace timestamps, not from configuration.
"""

from pathlib import Path

import pytest

from iot_backend.simulator import Simulation
from iot_backend.simulator.scenario_file import apply_actions, load_scenario, start_controller
from iot_backend.simulator.studies import (
    MODES,
    compare_modes,
    false_triggers,
    measure,
    run_scenario,
)
from iot_backend.simulator.trace import (
    ACK_RECEIVED,
    FRAME_SENT,
    LOCAL_FAILOVER,
    LOCAL_NO_ROUTE,
    NODE_BOOTED,
    READING_DELIVERED,
    ROUTE_INSTALLED,
    ROUTE_UPDATE_SENT,
    SEND_FAILED,
)

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"
PHASE_4B = [
    "local-failover-b-stops",
    "local-failover-one-way-a-b",
    "failover-without-controller",
    "backup-also-dead",
    "backup-unusable-but-reportable",
    "no-false-failover",
    "b-returns-after-failover",
]
HOLD_DOWN_MS = 5_000


def run(name, until_ms=None, seed=None):
    spec = load_scenario(SCENARIOS / f"{name}.json")
    if seed is not None:
        spec.config.seed = seed
    sim = Simulation(spec.config)
    host = start_controller(sim, spec)
    apply_actions(sim, spec.actions, host)
    sim.run(spec.duration_ms if until_ms is None else until_ms)
    return sim, host, spec


def route_events(host, type_=None):
    return [e for e in host.route_events if type_ is None or e.type == type_]


def failovers(sim, node="A"):
    return [e for e in sim.trace.of_type(LOCAL_FAILOVER) if e.node == node]


def data_sends(sim, to, after_ms=0, before_ms=None, node="A"):
    return [
        e
        for e in sim.trace.of_type(FRAME_SENT)
        if e.node == node
        and e.info["msg"] == "DATA"
        and e.info["to"] == to
        and e.time_ms > after_ms
        and (before_ms is None or e.time_ms < before_ms)
    ]


def delivered(sim, after_ms=0):
    return [
        e for e in sim.trace.of_type(READING_DELIVERED) if e.node == "C" and e.time_ms > after_ms
    ]


# ---- every scenario ----


@pytest.mark.parametrize("name", PHASE_4B)
def test_scenarios_are_labelled_simulated_and_use_local_failover(name):
    spec = load_scenario(SCENARIOS / f"{name}.json")
    assert "SIMULATED SETTING, not a measurement" in spec.description
    assert spec.config.routing == "controller" and spec.config.local_failover is True
    assert spec.controller.backup_next_hops is True
    assert "set_route" not in {a["action"] for a in spec.actions}


@pytest.mark.parametrize("name", PHASE_4B)
def test_same_seed_gives_an_identical_trace_and_route_history(name):
    first, host1, _ = run(name)
    second, host2, _ = run(name)
    assert first.trace.events == second.trace.events
    assert host1.route_events == host2.route_events
    assert first.summary() == second.summary()


# ---- backup installation ----


def test_the_backup_entry_is_installed_and_confirmed_before_the_route_is_active():
    sim, host, _ = run("local-failover-b-stops", until_ms=8_000)
    computed = route_events(host, "route_computed")[0]
    assert computed.data["path"] == ["A", "B", "C"]
    assert computed.data["backups"] == {"A": "D"} and computed.data["backup_entries"] == {"D": "C"}
    installs = {
        e.node: e.time_ms for e in sim.trace.of_type(ROUTE_INSTALLED) if e.info["version"] == 1
    }
    assert installs["D"] < installs["B"] < installs["A"]  # backup relay, primary relay, source
    active = route_events(host, "route_active")[0]
    assert active.time_ms >= installs["A"]
    assert active.data["confirmed_by"] == ["D", "B", "A"]  # nobody skipped
    assert sim.forwarding_state("A", "C")["backup"] == "D"
    assert sim.forwarding_state("D", "C")["primary"] == "C"  # D can really carry the backup


# ---- local-failover-b-stops ----


def test_a_moves_to_d_through_its_backup_before_the_controller_route_arrives():
    sim, host, _ = run("local-failover-b-stops")
    (fail,) = failovers(sim)
    assert fail.info["to"] == "D" and fail.info["failed"] == "B"
    assert fail.info["trigger"] in ("T1_neighbor_state", "T2_send_failures")
    # ordering, from the trace: A's own switch precedes the controller's instruction to A
    to_a = [
        e
        for e in sim.trace.of_type(ROUTE_UPDATE_SENT)
        if e.info["target"] == "A" and e.info["version"] == 2
    ]
    installed_v2 = [
        e for e in sim.trace.of_type(ROUTE_INSTALLED) if e.node == "A" and e.info["version"] == 2
    ]
    assert to_a and installed_v2
    assert 12_000 < fail.time_ms < to_a[0].time_ms < installed_v2[0].time_ms
    assert data_sends(sim, "B", after_ms=fail.time_ms) == []
    # the controller confirmed the backup path as the new route, labelling it as a result of a
    # local failover, and (here) learned of it before its own detection declared B lost
    (seen,) = route_events(host, "local_failover")
    assert seen.data["outcome"] == "backup_in_use" and seen.data["matches_current_route"]
    active = route_events(host, "route_active")[-1]
    assert active.data["path"] == ["A", "D", "C"]
    assert active.data["trigger"] == "local_failover_report"
    lost = [e for e in host.topology_events if e.type == "node_lost_contact"]
    assert lost and lost[0].time_ms > fail.time_ms
    assert sim.summary().lost == 0


def test_the_failed_over_route_keeps_delivering_and_acknowledging():
    sim, _, _ = run("local-failover-b-stops")
    after = delivered(sim, after_ms=12_000)
    assert len(after) >= 8
    acked = [e for e in sim.trace.of_type(ACK_RECEIVED) if e.time_ms > 12_000]
    assert len(acked) >= 8


# ---- local-failover-one-way-a-b ----


def test_one_way_failure_a_to_b_fails_over_and_b_stays_alive():
    sim, host, _ = run("local-failover-one-way-a-b")
    (fail,) = failovers(sim)
    assert fail.info["to"] == "D" and fail.time_ms > 12_000
    assert not [e for e in host.topology_events if e.type == "node_lost_contact"]  # B is alive
    assert data_sends(sim, "B", after_ms=fail.time_ms) == []
    assert len(delivered(sim, after_ms=14_000)) >= 6


def test_one_way_failure_with_only_t1_still_fails_over_though_a_never_loses_b():
    run_ = run_scenario(
        SCENARIOS / "local-failover-one-way-a-b.json",
        seed=42,
        mode={**MODES["local failover, T1"]},
    )
    sim = run_.sim
    (fail,) = failovers(sim)
    assert fail.info["trigger"] == "T1_neighbor_state" and fail.info["state"] == "one_way"
    lost = [
        e
        for e in sim.trace.events
        if e.event == "neighbor_changed"
        and e.node == "A"
        and e.info["neighbor"] == "B"
        and e.info["new"] == "lost"
    ]
    assert lost == []  # a timeout-only rule would never have fired here


# ---- failover-without-controller ----


def test_failover_works_with_the_controller_detached_and_it_catches_up_on_reattach():
    sim, host, _ = run("failover-without-controller")
    assert sim.gateway.events_lost > 0  # the controller really saw nothing
    window = [e for e in host.route_events if 10_000 < e.time_ms < 32_000]
    assert window == []  # ... and did nothing: no command, no event
    (fail,) = failovers(sim)
    assert 14_000 < fail.time_ms < 16_500  # A acted alone
    # C kept accepting readings and A kept getting its ACKs, with no laptop involved
    during = [e for e in delivered(sim, after_ms=fail.time_ms) if e.time_ms < 32_000]
    acked = [e for e in sim.trace.of_type(ACK_RECEIVED) if fail.time_ms < e.time_ms < 32_000]
    assert len(during) >= 7 and len(acked) >= 7
    assert data_sends(sim, "D", after_ms=fail.time_ms, before_ms=32_000)
    assert data_sends(sim, "B", after_ms=fail.time_ms) == []
    # after reattaching the controller learns of the failover and installs a matching route
    seen = route_events(host, "local_failover")
    assert seen and seen[0].time_ms > 32_000
    final = route_events(host, "route_active")[-1]
    assert final.data["path"] == ["A", "D", "C"] and final.time_ms > 32_000
    assert sim.forwarding_state("A", "C")["failed_over"] is False  # replaced by a newer version
    assert sim.forwarding_state("A", "C")["primary"] == "D"
    assert sim.summary().lost == 0


def test_a_blind_controller_does_not_announce_no_route_or_disturb_the_route():
    sim, host, _ = run("failover-without-controller", until_ms=31_000)
    assert route_events(host, "no_available_route") == []
    assert route_events(host, "no_route_confirmed") == []
    assert sim.forwarding_state("A", "C")["current"] == "D"


# ---- backup-also-dead ----


def test_when_the_backup_is_dead_too_a_has_no_route_locally_and_everything_stays_bounded():
    sim, host, spec = run("backup-also-dead")
    first = failovers(sim)
    no_route = [e for e in sim.trace.of_type(LOCAL_NO_ROUTE) if e.node == "A"]
    assert len(first) == 1 and len(no_route) == 1  # one switch, then nothing left
    assert no_route[0].info["why"] == "backup_failed"
    assert no_route[0].time_ms > first[0].time_ms
    state = sim.forwarding_state("A", "C")
    assert state["local_no_route"] is True and state["current"] is None
    s = sim.summary()
    assert s.drops["no_route"] >= 10  # new readings are dropped at once, with a reason
    assert s.unresolved <= 33
    # the report is bounded: the first copy plus its configured repeats, per event
    assert sim.node_counters("A").failover_reports <= 2 * (1 + spec.config.failover_report_repeats)
    reports = sim.node_counters("A").failover_reports
    sim.run(300_000)  # five more minutes in the same state
    assert sim.node_counters("A").failover_reports == reports  # it stopped by itself
    assert sim.summary().unresolved <= 33


def test_a_dead_backup_is_not_hidden_from_the_controller_when_a_report_can_get_out():
    sim, host, _ = run("backup-unusable-but-reportable")
    no_route = [e for e in sim.trace.of_type(LOCAL_NO_ROUTE) if e.node == "A"]
    assert len(no_route) == 1
    seen = [e for e in route_events(host, "local_failover") if e.data["outcome"] == "no_route"]
    assert len(seen) == 1 and seen[0].data["from"] == "D"  # reached C through D
    assert seen[0].time_ms > no_route[0].time_ms


# ---- no-false-failover ----


def test_k_minus_one_consecutive_failures_do_not_trigger():
    sim, host, _ = run("no-false-failover")
    failed = [e for e in sim.trace.of_type(SEND_FAILED) if e.node == "A" and e.info["to"] == "B"]
    assert len(failed) == 6  # three episodes of two failures each
    assert max(e.info["consecutive"] for e in failed) == 2  # K - 1
    assert failovers(sim) == []
    assert [e for e in sim.trace.of_type(LOCAL_NO_ROUTE)] == []
    assert route_events(host, "local_failover") == []


def test_false_failovers_stay_at_zero_without_loss_and_fall_as_k_rises():
    path = SCENARIOS / "no-false-failover.json"
    rows = false_triggers(path, range(1, 7), losses=(0.0, 0.10), ks=(2, 3, 4), duration_ms=90_000)
    by = {(r.loss, r.k): r for r in rows}
    assert all(by[(0.0, k)].failovers == 0 for k in (2, 3, 4))
    # with a lossy link a larger K means fewer false triggers (T1 is unaffected by K)
    assert by[(0.10, 2)].t2 > by[(0.10, 3)].t2 >= by[(0.10, 4)].t2
    assert by[(0.10, 3)].runs_with_failover >= 1  # and at 10 % loss it is not zero


# ---- b-returns-after-failover ----


def test_no_local_switch_back_and_the_controller_restores_b_only_after_the_hold_down():
    sim, host, _ = run("b-returns-after-failover")
    (fail,) = failovers(sim)
    boots = [e for e in sim.trace.of_type(NODE_BOOTED) if e.node == "B" and e.time_ms > 0]
    returned_at = boots[0].time_ms
    # when did A's own table go back to B? only when a newer version from the controller arrived
    back = [
        e
        for e in sim.trace.of_type(ROUTE_INSTALLED)
        if e.node == "A" and e.info["next_hop"] == "B" and e.time_ms > fail.time_ms
    ]
    assert len(back) == 1
    assert back[0].info["version"] >= 3
    assert data_sends(sim, "B", after_ms=fail.time_ms, before_ms=back[0].time_ms) == []
    held = route_events(host, "link_held_down")
    after_return = [h for h in held if h.time_ms > returned_at]
    assert {h.data["link"] for h in after_return} == {"A-B", "B-C"}
    assert back[0].time_ms >= max(h.data["eligible_at_ms"] for h in after_return)
    # the policy, not a reflex: the route computed for B names the topology change as its cause
    computed = [e for e in route_events(host, "route_computed") if e.data["path"][1] == "B"][-1]
    assert computed.data["trigger"] == "topology_changed"
    assert data_sends(sim, "B", after_ms=back[0].time_ms)  # and then it is used again


# ---- the comparison (simulation, small, fixed seeds) ----


def test_the_three_modes_order_as_the_timers_predict():
    path = SCENARIOS / "local-failover-b-stops.json"
    results = compare_modes(path, range(1, 9))
    controller, t1, t1t2 = (results[name] for name in MODES)
    assert all(r.switch_by == "controller_route" for r in controller)
    assert all(r.switch_by == "local_failover" for r in t1 + t1t2)
    assert {r.local_failover_trigger for r in t1} == {"T1_neighbor_state"}
    assert {r.local_failover_trigger for r in t1t2} == {"T2_send_failures"}
    for c, a, b in zip(controller, t1, t1t2, strict=True):
        # T1 waits for the same 3 s timeout the controller waits for: no worse, little better
        assert a.switch_after_ms <= c.switch_after_ms + 50
        assert c.switch_after_ms - a.switch_after_ms < 600
        # T2 does not wait for the timeout at all
        assert b.switch_after_ms < a.switch_after_ms - 1_000
        assert b.readings_lost <= a.readings_lost
        assert b.delivery_gap_ms < a.delivery_gap_ms


def test_each_failure_is_measured_the_same_way_in_every_mode():
    run_ = run_scenario(SCENARIOS / "local-failover-b-stops.json", 3, MODES["controller-only"])
    r = measure(run_)
    assert r is not None and r.failure == "stop_node B" and r.new_path == ("A", "D", "C")
    assert r.switch_ms is not None and r.first_send_ms is not None
    assert r.first_send_ms >= r.switch_ms  # it cannot send down a route it does not have yet
    assert delivered(run_.sim, after_ms=r.failure_ms)
