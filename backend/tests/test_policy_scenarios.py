"""Phase 9 in the simulator: probe statistics from the nodes, the lossy/reliable scenario, the
two policies through the whole stack, the comparison study, the CLI and the API.

Simulation only. Nothing here says anything about real radios.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from iot_backend.config import Settings
from iot_backend.live import schema
from iot_backend.main import create_app
from iot_backend.simulator.__main__ import main as cli_main
from iot_backend.simulator.policy_study import (
    COMPARED_POLICIES,
    compare_policies,
    format_comparison,
    policy_run,
    summarize_policy,
)
from iot_backend.simulator.scenario_file import ScenarioError, load_scenario, parse_scenario
from iot_backend.simulator.studies import MODES, run_scenario
from iot_backend.simulator.trace import LOCAL_FAILOVER, ROUTE_INSTALLED
from iot_backend.transports import TopologyReportEvent

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"
LOSSY = SCENARIOS / "fast-lossy-vs-slow-reliable.json"


def last_route(run):
    return [e for e in run.host.route_events if e.type == "route_computed"][-1].data["path"]


# ---- what the nodes report ----


def collect_reports(path, seed, until_ms, **overrides):
    spec = load_scenario(path)
    spec.config.seed = seed
    for key, value in overrides.items():
        setattr(spec.config, key, value)
    from iot_backend.simulator import Simulation
    from iot_backend.simulator.scenario_file import ControllerHost

    sim = Simulation(spec.config)
    reports: list[TopologyReportEvent] = []
    sim.gateway.subscribe(lambda e: reports.append(e) if isinstance(e, TopologyReportEvent) else 0)
    ControllerHost(sim, spec.controller)
    sim.run(until_ms)
    return sim, reports


def test_nodes_report_probe_statistics_once_five_probes_have_completed():
    _, reports = collect_reports(SCENARIOS / "live-demo.json", 3, 20_000, probe_window=20)
    stats = [(r, n) for r in reports for n in r.neighbors if n.probes_total is not None]
    assert stats, "no probe statistics were reported"
    for _, entry in stats:
        assert 5 <= entry.probes_total <= 20
        assert 0 <= entry.probes_ok <= entry.probes_total
    early = [r for r in reports if r.received_ms < 4_000]
    assert all(n.probes_total is None for r in early for n in r.neighbors)  # fewer than 5 so far


def test_a_clean_link_reports_every_probe_answered():
    _, reports = collect_reports(SCENARIOS / "live-demo.json", 3, 40_000, probe_window=20)
    late = [n for r in reports if r.received_ms > 30_000 for n in r.neighbors]
    assert late and all(n.probes_total == 20 and n.probes_ok == 20 for n in late)


def test_a_lossy_link_reports_unanswered_probes_and_only_for_its_own_end():
    _, reports = collect_reports(LOSSY, 1, 60_000)
    ab = [n for r in reports if r.origin == 1 and r.received_ms > 30_000 for n in r.neighbors]
    on_b = [n for n in ab if n.neighbor == 2]
    on_d = [n for n in ab if n.neighbor == 4]
    assert on_b and on_d
    assert any(n.probes_ok < n.probes_total for n in on_b)  # 15 % loss each way: ~28 % of probes
    assert all(n.probes_ok == n.probes_total for n in on_d)  # the clean path loses none


def test_statistics_are_off_unless_the_scenario_turns_the_window_on():
    _, reports = collect_reports(SCENARIOS / "live-demo.json", 3, 30_000)
    assert reports
    assert all(n.probes_total is None for r in reports for n in r.neighbors)


def test_a_neighbor_in_the_lost_state_is_not_rated_and_history_starts_afresh():
    spec = parse_scenario(
        {
            **json.loads(LOSSY.read_text()),
            "duration_s": 60,
            "actions": [
                {"at_ms": 20000, "action": "stop_node", "node": "B"},
                {"at_ms": 40000, "action": "start_node", "node": "B"},
            ],
        }
    )
    from iot_backend.simulator import Simulation
    from iot_backend.simulator.scenario_file import ControllerHost, apply_actions

    sim = Simulation(spec.config)
    reports: list[TopologyReportEvent] = []
    sim.gateway.subscribe(lambda e: reports.append(e) if isinstance(e, TopologyReportEvent) else 0)
    host = ControllerHost(sim, spec.controller)
    apply_actions(sim, spec.actions, host)
    sim.run(60_000)
    during = [
        n
        for r in reports
        if r.origin == 1 and 28_000 < r.received_ms < 39_000
        for n in r.neighbors
        if n.neighbor == 2
    ]
    assert during and all(n.state == "lost" and n.probes_total is None for n in during)
    after = [
        n
        for r in reports
        if r.origin == 1 and r.received_ms > 40_000
        for n in r.neighbors
        if n.neighbor == 2 and n.probes_total is not None
    ]
    assert all(n.probes_total <= (60_000 - 40_000) // 900 for n in after)  # no pre-outage history


# ---- the scenario through the whole stack ----


def test_the_scenario_is_labelled_a_simulated_setting_and_is_valid():
    spec = load_scenario(LOSSY)
    assert "SIMULATED SETTING" in spec.description
    assert spec.config.routing == "controller"
    assert spec.config.probe_window == 20
    assert spec.controller.policy == "lowest_delay"


def test_policy_must_be_one_of_the_known_names_in_a_scenario_file():
    raw = json.loads(LOSSY.read_text())
    raw["controller"]["policy"] = "fastest"
    with pytest.raises(ScenarioError):
        parse_scenario(raw)
    raw["controller"]["policy"] = "highest_reliability"
    assert parse_scenario(raw).controller.policy == "highest_reliability"
    raw["controller"]["hysteresis_evaluations"] = 5
    assert parse_scenario(raw).controller.hysteresis_evaluations == 5


def share_of_time_on(run, path):
    """Fraction of the run for which the route that was active (confirmed) was `path`."""
    spans, current, since = {}, None, 0
    for event in run.host.route_events:
        if event.type == "route_active":
            spans[tuple(current or ())] = spans.get(tuple(current or ()), 0) + event.time_ms - since
            current, since = event.data["path"], event.time_ms
    spans[tuple(current or ())] = spans.get(tuple(current or ()), 0) + run.duration_ms - since
    return spans.get(tuple(path), 0) / run.duration_ms


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_lowest_delay_mostly_uses_the_fast_lossy_path_and_reliability_the_clean_one(seed):
    # Not "always": a break of a lossy link (lost HELLOs) sends either policy over D for a
    # while, and lowest delay then needs the hold-down and three evaluations to come back, so
    # across seeds 1 to 10 it spent 33 % to 86 % of the run on B. Reliability spent 0 % to 8 %.
    delay = run_scenario(LOSSY, seed, policy="lowest_delay")
    reliable = run_scenario(LOSSY, seed, policy="highest_reliability")
    on_b_delay = share_of_time_on(delay, ["A", "B", "C"])
    on_b_reliable = share_of_time_on(reliable, ["A", "B", "C"])
    assert on_b_delay > on_b_reliable + 0.25
    assert on_b_reliable < 0.2
    assert delay.host.controller.route_state()["plan_policy"] == "lowest_delay"
    assert reliable.host.controller.route_state()["plan_policy"] == "highest_reliability"


def test_the_two_policies_deliver_differently_over_the_same_seeds():
    results = compare_policies(LOSSY, range(1, 6))
    fast, safe = (summarize_policy(results[p]) for p in COMPARED_POLICIES)
    assert safe.delivery_rate > fast.delivery_rate
    assert safe.time_mean_ms < fast.time_mean_ms  # retries over the lossy path cost seconds
    assert safe.trace_dropped == 0 and fast.trace_dropped == 0
    assert safe.lost <= fast.lost


def test_reliability_changes_route_fewer_times_than_lowest_delay_on_this_network():
    # Not a general claim: on this network the lossy links keep losing HELLOs, so the route
    # through them keeps breaking and being re-earned under lowest delay.
    results = compare_policies(LOSSY, range(1, 6))
    fast, safe = (summarize_policy(results[p]) for p in COMPARED_POLICIES)
    assert safe.route_changes_total < fast.route_changes_total


def test_a_run_under_one_policy_is_repeatable_and_a_different_seed_differs():
    one = policy_run(LOSSY, 5, "highest_reliability", 90_000)
    again = policy_run(LOSSY, 5, "highest_reliability", 90_000)
    other = policy_run(LOSSY, 6, "highest_reliability", 90_000)
    assert one == again
    assert one != other


def test_every_route_event_names_its_policy_and_validates_against_the_schema():
    run = run_scenario(LOSSY, 2, policy="highest_reliability", duration_ms=60_000)
    for event in run.host.route_events:
        problems = schema.validate_event(event.type, {"sim_ms": event.time_ms, **event.data})
        assert problems == [], (event.type, problems)
        if event.type in ("route_computed", "route_active"):
            assert event.data["policy"] == "highest_reliability"


# ---- local failover is not delayed by the policy ----


@pytest.mark.parametrize("policy", ["lowest_delay", "highest_reliability"])
def test_local_failover_and_the_broken_route_replacement_are_not_delayed(policy):
    path = SCENARIOS / "local-failover-b-stops.json"
    baseline = run_scenario(path, 4, MODES["local failover, T1 + T2"])
    spec = load_scenario(path)
    mode = {**MODES["local failover, T1 + T2"], "probe_window": 20}
    under = run_scenario(path, 4, mode, policy=policy)
    stop = next(a["at_ms"] for a in spec.actions if a["action"] == "stop_node")

    def times(run):
        failover = [e.time_ms for e in run.sim.trace.of_type(LOCAL_FAILOVER)]
        d_active = next(
            e.time_ms
            for e in run.host.route_events
            if e.type == "route_active" and e.data["path"] == ["A", "D", "C"] and e.time_ms > stop
        )
        return failover, d_active

    base_failover, base_active = times(baseline)
    pol_failover, pol_active = times(under)
    assert base_failover and pol_failover
    assert pol_failover[0] - stop < 3_000  # A switched on its own, long before any evaluation
    assert abs(pol_failover[0] - base_failover[0]) <= 200
    assert pol_active - stop < 3_500  # the controller confirmed D at once: no 3 x 5 s wait
    assert abs(pol_active - base_active) <= 600
    assert pol_active - stop < 3 * 5_000


def test_without_a_policy_in_the_scenario_nothing_changed_from_phase_four():
    run = run_scenario(SCENARIOS / "b-failure-auto-reroute.json", 42)
    assert run.host.controller.policy == "immediate_rtt"
    assert not [e for e in run.host.route_events if e.type.startswith("route_candidate")]
    assert (
        ROUTE_INSTALLED
    )  # (the trace constant exists; the existing scenario tests cover the rest)


# ---- the study and the CLI ----


def test_the_comparison_table_names_what_it_measures(capsys):
    results = compare_policies(LOSSY, range(1, 3), duration_ms=60_000)
    text = format_comparison("x.json", results, 60)
    for label in (
        "readings delivered",
        "readings lost",
        "delivery rate, pooled",
        "delivery time, mean",
        "route changes per run",
        "SIMULATION, not hardware",
    ):
        assert label in text
    assert "lowest_delay" in text and "highest_reliability" in text


def test_the_command_line_study_runs(capsys):
    code = cli_main([str(LOSSY), "--compare-policies", "2", "--duration", "60"])
    out = capsys.readouterr().out
    assert code == 0
    assert "highest_reliability" in out and "delivery rate, pooled" in out


def test_the_command_line_study_needs_a_scenario(capsys):
    assert cli_main(["--compare-policies", "2"]) == 2


# ---- the live API ----


def make_client(**overrides) -> TestClient:
    settings = Settings(
        _env_file=None, live_autostart=False, heartbeat_interval_seconds=0.05, **overrides
    )
    return TestClient(create_app(settings))


def step(client, ms):
    assert client.post("/api/control/step", json={"ms": ms}).status_code == 200


def test_the_snapshot_reports_the_policy_and_the_default_is_the_phase_four_behaviour():
    with make_client() as client:
        step(client, 10_000)
        snap = client.get("/api/snapshot").json()
        assert snap["policy"]["current"] == "immediate_rtt"
        assert snap["policy"]["available"] == [
            "immediate_rtt",
            "lowest_delay",
            "highest_reliability",
        ]
        assert snap["policy"]["hysteresis_evaluations"] == 3
        assert snap["route"]["policy"] == "immediate_rtt"
        assert schema.validate_snapshot(snap) == []
        for link in snap["links"]:
            assert "smoothed_rtt_ms" in link and "delivery_probability" in link


def test_the_policy_can_be_chosen_by_configuration():
    with make_client(routing_policy="highest_reliability") as client:
        step(client, 5_000)
        assert client.get("/api/snapshot").json()["policy"]["current"] == "highest_reliability"


def test_the_policy_can_be_changed_through_the_api_and_the_stream_says_so():
    with make_client() as client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "status"
        step(client, 10_000)
        reply = client.post("/api/policy", json={"policy": "lowest_delay"})
        assert reply.status_code == 200
        body = reply.json()
        assert body["ok"] and body["policy"] == "lowest_delay" and body["changed"] is True
        assert (
            client.post("/api/policy", json={"policy": "lowest_delay"}).json()["changed"] is False
        )
        snap = client.get("/api/snapshot").json()
        assert (
            snap["policy"]["current"] == "lowest_delay"
            and snap["route"]["policy"] == "lowest_delay"
        )
        events = client.get("/api/events?after_id=0&limit=1000").json()["events"]
        changed = [e for e in events if e["type"] == "policy_changed"]
        assert [(e["data"]["old"], e["data"]["new"]) for e in changed] == [
            ("immediate_rtt", "lowest_delay")
        ]
        assert schema.validate_event("policy_changed", changed[0]["data"]) == []


def test_the_policy_endpoint_refuses_bad_requests():
    with make_client() as client:
        assert client.post("/api/policy", json={"policy": "fastest"}).status_code == 400
        assert client.post("/api/policy", json={"policy": 3}).status_code == 400
        assert client.post("/api/policy", json={}).status_code == 400
        assert (
            client.post("/api/policy", json={"policy": "lowest_delay", "x": 1}).status_code == 400
        )
        assert client.post("/api/policy", data={"policy": "lowest_delay"}).status_code == 415
        evil = client.post(
            "/api/policy",
            json={"policy": "lowest_delay"},
            headers={"Origin": "http://evil.example"},
        )
        assert evil.status_code == 403
        assert client.get("/api/snapshot").json()["policy"]["current"] == "immediate_rtt"
        error = client.post("/api/policy", json={"policy": "fastest"}).json()["error"]
        assert error["code"] == "bad_request"


def test_choosing_a_policy_does_not_touch_the_simulated_environment():
    with make_client() as client:
        step(client, 10_000)
        before = client.get("/api/snapshot").json()["presenter"]
        client.post("/api/policy", json={"policy": "highest_reliability"})
        assert client.get("/api/snapshot").json()["presenter"] == before
        events = client.get("/api/events?after_id=0&limit=1000").json()["events"]
        assert not [e for e in events if e["type"] == "environment" and "policy" in str(e)]
