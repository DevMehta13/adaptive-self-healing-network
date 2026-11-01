"""A stopped relay under each policy, in the live-demo scenario with a fixed seed (simulation).

Regression for the incident where the controller said "no available route" for over a minute
after `stop B` under highest reliability, although A-D and D-C were up. The cause: that policy
only prices links whose delivery probability is known, and the live-demo nodes keep no probe
statistics (`probe_window` 0), so with B gone no link at all was priced. These runs cover both
settings of `probe_window`, a policy set from the start and one switched while running.

Simulation only. Nothing here says anything about real radios.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from iot_backend.live import schema
from iot_backend.simulator import Simulation
from iot_backend.simulator.scenario_file import ControllerHost, load_scenario

LIVE_DEMO = Path(__file__).resolve().parents[2] / "scenarios" / "live-demo.json"
SEED = 7
SWITCH_MS, STOP_MS, START_MS, END_MS = 20_000, 40_000, 100_000, 140_000
POLICIES = ["immediate_rtt", "lowest_delay", "highest_reliability"]


def run_demo(start_policy, switch_to=None, probe_window=0):
    spec = load_scenario(LIVE_DEMO)
    spec.config.seed = SEED
    spec.config.probe_window = probe_window
    sim = Simulation(spec.config)
    host = ControllerHost(sim, replace(spec.controller, policy=start_policy))
    sim.run(SWITCH_MS)
    if switch_to:
        host.controller.set_policy(switch_to)
    sim.run(STOP_MS - sim.now_ms)
    sim.stop_node("B")
    sim.run(START_MS - sim.now_ms)
    sim.start_node("B")
    sim.run(END_MS - sim.now_ms)
    return host


def events(host, *types, after=0, before=END_MS + 1):
    return [e for e in host.route_events if e.type in types and after <= e.time_ms < before]


def d_active_after_stop(host):
    found = [
        e for e in events(host, "route_active", after=STOP_MS) if e.data["path"] == ["A", "D", "C"]
    ]
    assert found, "no route through D was ever confirmed after B stopped"
    return found[0].time_ms - STOP_MS


CASES = [
    pytest.param(
        policy,
        switch,
        window,
        id=f"{switch or policy}-{'switched' if switch else 'start'}-w{window}",
    )
    for window in (0, 20)
    for policy, switch in (
        [("immediate_rtt", None), ("lowest_delay", None), ("highest_reliability", None)]
        + [("immediate_rtt", "lowest_delay"), ("immediate_rtt", "highest_reliability")]
    )
]


@pytest.mark.parametrize(("policy", "switch", "window"), CASES)
def test_stopping_b_gives_a_route_through_d_as_fast_as_the_default_policy(policy, switch, window):
    baseline = d_active_after_stop(run_demo("immediate_rtt", None, window))
    host = run_demo(policy, switch, window)
    elapsed = d_active_after_stop(host)
    assert elapsed < 3_500  # the controller confirms D at once: no 5 s evaluation, no 3 in a row
    assert abs(elapsed - baseline) <= 600
    assert not events(host, "no_available_route", after=STOP_MS)


@pytest.mark.parametrize(("policy", "switch", "window"), CASES)
def test_starting_b_again_leaves_no_lasting_no_route(policy, switch, window):
    host = run_demo(policy, switch, window)
    assert not events(host, "no_available_route", after=STOP_MS)
    state = host.controller.route_state()
    assert state["kind"] == "route" and state["state"] == "active"
    assert state["path"] in (["A", "B", "C"], ["A", "D", "C"])


def test_without_statistics_the_policy_says_it_used_rtt_and_the_events_still_validate():
    host = run_demo("immediate_rtt", "highest_reliability", probe_window=0)
    used = [
        e
        for e in events(host, "route_computed", "route_active", after=STOP_MS)
        if e.data.get("rtt_fallback")
    ]
    assert used, "the fallback was used but not reported"
    assert host.controller.route_state()["delivery_probability"] is None
    for event in host.route_events:
        assert schema.validate_event(event.type, {"sim_ms": event.time_ms, **event.data}) == []


def test_with_statistics_reliability_is_priced_by_probability_and_never_a_fallback():
    host = run_demo("immediate_rtt", "highest_reliability", probe_window=20)
    assert not [e for e in host.route_events if e.data.get("rtt_fallback")]
    computed = events(host, "route_computed", after=STOP_MS)[0].data
    assert computed["delivery_probability"] == 1.0
    assert all(g["delivery_probability"] is not None for g in computed["graph"])


def test_the_run_is_repeatable():
    first = [(e.time_ms, e.type) for e in run_demo("highest_reliability").route_events]
    second = [(e.time_ms, e.type) for e in run_demo("highest_reliability").route_events]
    assert first == second
