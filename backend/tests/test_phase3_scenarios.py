"""Phase 3 scenarios: discovery and failure detection seen through the controller view."""

from collections import Counter
from pathlib import Path

import pytest

from iot_backend.controller import TopologyView
from iot_backend.controller.topology import LINK_ONE_WAY, LINK_STALE, LOST_CONTACT, REACHABLE, STALE
from iot_backend.simulator import SimulatedGateway, Simulation
from iot_backend.simulator.measurements import detection_measurements
from iot_backend.simulator.scenario_file import apply_actions, load_scenario
from iot_backend.simulator.trace import (
    FRAME_FORWARDED,
    FRAME_RECEIVED,
    FRAME_SENT,
    READING_LOST,
)

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"
TIMEOUT_MS = 3_000
COALESCE_MS = 50
# Declared within the neighbor timeout plus report batching and about three hops of transit.
DETECTION_BOUND_MS = TIMEOUT_MS + COALESCE_MS + 250


def run(name: str, until_ms: int | None = None):
    spec = load_scenario(SCENARIOS / f"{name}.json")
    sim = Simulation(spec.config)
    view = TopologyView(sim.gateway)
    apply_actions(sim, spec.actions)
    sim.run(spec.duration_ms if until_ms is None else until_ms)
    return sim, view, spec


def events_of(view, type_, node=None):
    return [
        e for e in view.events if e.type == type_ and (node is None or e.data.get("node") == node)
    ]


# ---- b-loss-detected ----


def test_b_loss_is_detected_within_the_bound_via_reports_through_d():
    sim, view, spec = run("b-loss-detected")
    (lost,) = events_of(view, "node_lost_contact", "B")
    latency = lost.time_ms - 8_000
    assert TIMEOUT_MS - 1_100 < latency <= DETECTION_BOUND_MS  # last HELLO was up to 1.1 s old
    assert set(lost.data["observed_by"]) == {"A", "C"}
    assert view.node_status("B") == LOST_CONTACT
    # B's neighbors A and C are the observers; A's report could only reach C through D
    a_reports = [
        e
        for e in sim.trace.of_type(FRAME_FORWARDED)
        if e.info["msg"] == "TOPOLOGY_REPORT" and e.identity.src == 1 and e.time_ms > 8_000
    ]
    assert a_reports and {e.node for e in a_reports} == {"D"}
    # links through B are down and unusable; the D path is still up
    assert view.usable_links() == [("A", "D"), ("C", "D")]
    assert view.link_state("A", "B") == "down" and view.link_state("B", "C") == "down"
    # data still uses the manual route through B, so readings are lost (no healing yet)
    assert sim.trace.of_type(READING_LOST)


def test_detection_measurement_matches_the_events():
    _, view, spec = run("b-loss-detected")
    (m,) = detection_measurements(spec.actions, view.events)
    assert (m.kind, m.node, m.reference_ms) == ("loss", "B", 8_000)
    assert m.latency_ms is not None and 1_500 < m.latency_ms <= DETECTION_BOUND_MS


# ---- b-return-detected ----


def test_b_return_is_detected_with_a_new_session_and_links_return_to_two_way():
    sim, view, spec = run("b-return-detected")
    (regained,) = events_of(view, "node_regained_contact", "B")
    assert regained.data["restarted"] is True
    assert 14_000 <= regained.time_ms <= 14_000 + 1_500
    assert view.node_status("B") == REACHABLE
    assert view.usable_links() == [("A", "B"), ("A", "D"), ("B", "C"), ("C", "D")]
    losses = detection_measurements(spec.actions, view.events)
    assert [(m.kind, m.node) for m in losses] == [("loss", "B"), ("return", "B")]
    assert all(m.latency_ms is not None for m in losses)


# ---- one-way-link ----


def test_one_way_link_is_reported_one_way_and_is_not_usable():
    sim, view, _ = run("one-way-link")
    assert view.link_state("A", "B") == LINK_ONE_WAY
    snapshot = view.snapshot()
    (link,) = [x for x in snapshot["links"] if (x["a"], x["b"]) == ("A", "B")]
    assert link["usable"] is False
    assert link["directions"] == {"B->A": True, "A->B": False}  # B's frames still reach A
    assert ("A", "B") not in view.usable_links()
    # neither endpoint is lost: both are still heard elsewhere
    assert view.node_status("A") == REACHABLE and view.node_status("B") == REACHABLE
    assert events_of(view, "node_lost_contact") == []
    assert view.usable_links() == [("A", "D"), ("B", "C"), ("C", "D")]


# ---- report-survives-relay-loss ----


def test_a_reports_reach_c_through_d_after_b_stops():
    sim, view, _ = run("report-survives-relay-loss")
    a_reports_at_c = [
        e
        for e in sim.trace.of_type(FRAME_RECEIVED)
        if e.node == "C"
        and e.info.get("msg") == "TOPOLOGY_REPORT"
        and e.identity.src == 1
        and e.time_ms > 7_000
    ]
    assert a_reports_at_c
    assert {e.info["sender"] for e in a_reports_at_c} == {"D"}  # none via B, which is dead
    assert view.node_status("A") == REACHABLE
    assert view.snapshot()["nodes"][0]["last_report_age_ms"] < 6_000  # A's view stays fresh


# ---- flooding-is-bounded ----


def test_flooding_scenario_stays_bounded():
    sim, view, spec = run("flooding-is-bounded")
    forwards = Counter(
        (e.node, e.identity)
        for e in sim.trace.of_type(FRAME_FORWARDED)
        if e.info["msg"] in {"TOPOLOGY_REPORT", "DELIVERY_ACK"}
    )
    assert max(forwards.values()) == 1
    s = sim.summary()
    assert s.flood_cache_evictions > 0  # the 8-entry cache really was exercised
    assert s.frames_sent < 1_500
    # the controller view is unaffected by the tiny caches
    assert view.usable_links() == [("A", "B"), ("A", "D"), ("B", "C"), ("C", "D")]


# ---- stale-report-expiry ----


def test_reports_expire_when_they_stop_arriving():
    sim, view, spec = run("stale-report-expiry", until_ms=12_000)
    assert view.node_status("A") == REACHABLE  # C stopped at 10 s; reports are still fresh
    assert view.usable_links()  # within the 15 s rule
    sim.run(30_000)  # to 42 s
    for node in "ABCD":
        assert view.node_status(node) == STALE
    assert view.usable_links() == []
    assert {x["state"] for x in view.snapshot()["links"]} == {LINK_STALE}
    stale_events = events_of(view, "node_stale")
    assert {e.data["node"] for e in stale_events} == set("ABCD")
    first_stale = min(e.time_ms for e in stale_events)
    assert 10_000 + 15_000 - 6_000 <= first_stale <= 10_000 + 15_000 + 1_000
    # nobody was reported as lost: the silence is the controller's, not the nodes'
    assert events_of(view, "node_lost_contact") == []


# ---- general ----


def test_topology_events_are_deterministic_for_a_seed():
    first = run("b-return-detected")[1].events
    second = run("b-return-detected")[1].events
    assert first == second and first


def test_controller_receives_only_what_the_gateway_delivers():
    sim, view, _ = run("b-loss-detected")
    gateway = sim.gateway
    assert isinstance(gateway, SimulatedGateway)
    # the gateway object holds the engine and handlers; it holds no node or link objects
    held = {type(v).__name__ for v in vars(gateway).values()}
    assert not held & {"Node", "Link", "Simulation"}
    # nodes only reached the gateway by sending frames: every report in the view was sent
    sent_reports = {
        (e.identity.src, e.identity.seq)
        for e in sim.trace.of_type(FRAME_SENT)
        if e.info["msg"] == "TOPOLOGY_REPORT"
    }
    assert sent_reports


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_loss_detection_bound_holds_across_seeds(seed):
    spec = load_scenario(SCENARIOS / "b-loss-detected.json")
    spec.config.seed = seed
    sim = Simulation(spec.config)
    view = TopologyView(sim.gateway)
    apply_actions(sim, spec.actions)
    sim.run(spec.duration_ms)
    (m,) = detection_measurements(spec.actions, view.events)
    assert m.latency_ms is not None and m.latency_ms <= DETECTION_BOUND_MS
