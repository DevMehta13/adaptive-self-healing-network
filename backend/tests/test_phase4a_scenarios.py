"""Phase 4a scenarios: automatic routes, rerouting and installation, seen from the outside.

Every claim is checked from the packet trace and the controller's event stream, never from the
configuration: a route counts as used only if packets were observed taking it.
"""

from pathlib import Path

import pytest

from iot_backend.simulator import Simulation, observed_paths
from iot_backend.simulator.measurements import recovery_measurement
from iot_backend.simulator.scenario_file import apply_actions, load_scenario, start_controller
from iot_backend.simulator.trace import (
    FRAME_DROPPED,
    FRAME_RECEIVED,
    FRAME_SENT,
    NODE_BOOTED,
    NODE_STOPPED,
    READING_DELIVERED,
    ROUTE_INSTALLED,
    ROUTE_UPDATE_SENT,
)

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"
PHASE_4A = [
    "auto-initial-route",
    "b-failure-auto-reroute",
    "b-returns",
    "one-way-a-b",
    "both-relays-fail",
    "no-route-told-explicitly",
    "route-update-lost",
    "route-ack-lost",
    "stale-update-ignored",
    "controller-restart",
]
HOLD_DOWN_MS = 5_000
TIMEOUT_MS = 1_000


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


def delivered(sim):
    return [e for e in sim.trace.of_type(READING_DELIVERED) if e.node == "C"]


def delivered_via(sim, relay, after_ms=0, before_ms=None):
    """Identities accepted at C whose observed path ran through `relay`, in a time window."""
    out = []
    for e in delivered(sim):
        if e.time_ms < after_ms or (before_ms is not None and e.time_ms >= before_ms):
            continue
        paths = observed_paths(sim.trace.events, e.identity)
        if any(len(p) == 3 and p[1] == relay and p[2] == "C" for p in paths):
            out.append(e.identity)
    return out


# ---- every scenario: labelled, repeatable, and the controller drives it ----


@pytest.mark.parametrize("name", PHASE_4A)
def test_scenarios_label_the_simulated_link_delays_and_use_controller_routing(name):
    spec = load_scenario(SCENARIOS / f"{name}.json")
    assert spec.config.routing == "controller"
    assert spec.config.probe_interval_ms > 0
    assert "SIMULATED SETTING, not a measurement" in spec.description
    # the scenario never edits a route by hand: no set_route action, no manual routes
    assert "set_route" not in {a["action"] for a in spec.actions}
    # the cheaper B links are a configured setting
    assert spec.config.links[("A", "B")].delay_ms < spec.config.links[("A", "D")].delay_ms


@pytest.mark.parametrize("name", PHASE_4A)
def test_same_seed_gives_an_identical_trace_and_route_history(name):
    first, host1, _ = run(name)
    second, host2, _ = run(name)
    assert first.trace.events == second.trace.events
    assert first.summary() == second.summary()
    assert host1.route_events == host2.route_events
    assert host1.topology_events == host2.topology_events


def test_a_different_seed_gives_a_different_trace():
    first, _, _ = run("b-failure-auto-reroute", seed=1)
    second, _, _ = run("b-failure-auto-reroute", seed=2)
    assert first.trace.events != second.trace.events


# ---- the black-hole property and install order, over every scenario ----


@pytest.mark.parametrize("name", PHASE_4A)
def test_a_never_sends_toward_a_running_relay_that_has_no_installed_route(name):
    """No black hole: whenever A addresses a relay that is powered on, that relay has been told
    where to send the frame (it holds an installed entry since its last boot)."""
    sim, _, _ = run(name)
    events = sorted(sim.trace.events, key=lambda e: e.time_ms)  # stable: keeps trace order
    knows = {"B": False, "D": False}
    running = {"B": True, "D": True}
    checked = 0
    for e in events:
        if e.node in knows:
            if e.event == ROUTE_INSTALLED:
                knows[e.node] = True
            elif e.event == NODE_STOPPED:
                running[e.node], knows[e.node] = False, False
            elif e.event == NODE_BOOTED:
                running[e.node], knows[e.node] = True, False
        if e.event == FRAME_SENT and e.node == "A" and e.info["msg"] == "DATA":
            relay = e.info["to"]
            if relay in knows and running[relay]:
                checked += 1
                assert knows[relay], (
                    f"{name}: A sent to {relay} at {e.time_ms} ms before its install"
                )
    assert checked > 0


@pytest.mark.parametrize("name", [n for n in PHASE_4A if n != "controller-restart"])
def test_relays_are_installed_and_acknowledged_before_the_source_is_told(name):
    """From the trace alone: for each route version the source's update is originated only after
    C has received the acknowledgement of every relay update of that version."""
    sim, host, _ = run(name)
    events = sim.trace.events
    updates = [e for e in events if e.event == ROUTE_UPDATE_SENT]
    by_version = {}
    for e in updates:
        by_version.setdefault(e.info["version"], []).append(e)
    checked = 0
    for version, sent in by_version.items():
        to_source = [e for e in sent if e.info["target"] == "A"]
        to_relays = [
            e for e in sent if e.info["target"] in ("B", "D") and e.info["next_hop"] == "C"
        ]
        if not to_source or not to_relays or to_source[0].info["next_hop"] is None:
            continue
        relay = to_relays[0].info["target"]
        first_source_update = to_source[0].time_ms
        relay_acks = [
            e
            for e in events
            if e.event == FRAME_RECEIVED
            and e.node == "C"
            and e.info["msg"] == "ROUTE_INSTALL_ACK"
            and e.identity.src == {"B": 2, "D": 4}[relay]
            and e.time_ms <= first_source_update
        ]
        installed_at_relay = [
            e
            for e in events
            if e.event == ROUTE_INSTALLED and e.node == relay and e.info["version"] == version
        ]
        assert relay_acks, f"{name} v{version}: source told before any ack from {relay}"
        assert installed_at_relay and installed_at_relay[0].time_ms <= first_source_update
        checked += 1
    assert checked >= 1


def test_a_route_is_active_only_after_every_node_on_it_acknowledged():
    sim, host, _ = run("b-failure-auto-reroute")
    for active in route_events(host, "route_active"):
        version, path = active.data["version"], active.data["path"]
        forwarding_nodes = path[:-1]
        installs = {
            e.node: e.time_ms
            for e in sim.trace.of_type(ROUTE_INSTALLED)
            if e.info["version"] == version
        }
        assert set(forwarding_nodes) <= set(installs)
        assert all(t <= active.time_ms for t in installs.values())
        assert set(active.data["confirmed_by"]) == set(forwarding_nodes)


# ---- auto-initial-route ----


def test_auto_initial_route_is_found_and_used_without_any_configuration():
    sim, host, spec = run("auto-initial-route")
    assert spec.config.routing == "controller"  # and nodes started with no data routes at all
    first = route_events(host, "route_computed")[0]
    assert first.data["path"] == ["A", "B", "C"] and first.data["trigger"] == "initial"
    # before the route there were no data frames at all, and readings were dropped no_route
    installed_at_a = [e for e in sim.trace.of_type(ROUTE_INSTALLED) if e.node == "A"][0]
    data_sends = [e for e in sim.trace.of_type(FRAME_SENT) if e.info["msg"] == "DATA"]
    assert data_sends and min(e.time_ms for e in data_sends) >= installed_at_a.time_ms
    s = sim.summary()
    assert s.dropped_no_route == 1  # the 2 s reading; the route was active by about 4 s
    assert s.attempts == s.readings_created - s.dropped_no_route  # every other reading was sent
    # every delivered reading was observed going A > B > C
    assert len(delivered(sim)) == len(delivered_via(sim, "B")) >= 7
    assert delivered_via(sim, "D") == []
    assert s.lost == 0 and s.duplicates == 0


def test_readings_before_the_first_route_are_counted_and_bounded():
    sim, _, _ = run("auto-initial-route", until_ms=3_000)
    s = sim.summary()
    assert s.attempts == 0 and s.dropped_no_route == s.readings_created == 1
    assert s.drops["no_route"] == 1


# ---- b-failure-auto-reroute ----


def test_b_failure_reroutes_automatically_through_d():
    sim, host, spec = run("b-failure-auto-reroute")
    assert [a["action"] for a in spec.actions] == ["stop_node"]  # the only input is the failure
    computed = route_events(host, "route_computed")
    assert [e.data["path"] for e in computed] == [["A", "B", "C"], ["A", "D", "C"]]
    assert computed[1].time_ms > 12_000
    active = route_events(host, "route_active")
    assert [e.data["version"] for e in active] == [1, 2]
    before = delivered_via(sim, "B", before_ms=12_000)
    after = delivered_via(sim, "D", after_ms=12_000)
    assert len(before) >= 4 and len(after) >= 5
    assert delivered_via(sim, "B", after_ms=12_100) == []  # B is dead: nothing after the failure
    assert sim.route_state("A", "C") == (1000, 2, "D")
    # D was told first, then A
    order = [
        e.info["target"] for e in sim.trace.of_type(ROUTE_UPDATE_SENT) if e.info["version"] == 2
    ]
    assert order == ["D", "A"]


def test_recovery_measurement_is_consistent_with_the_trace():
    sim, host, spec = run("b-failure-auto-reroute")
    r = recovery_measurement(
        spec.actions, sim.trace.events, host.route_events, host.topology_events
    )
    assert r is not None and r.new_path == ("A", "D", "C")
    assert 2_000 <= r.detection_ms <= 3_500  # 3 s neighbor timeout plus batching and transit
    assert r.detection_ms < r.route_active_after_ms < r.service_recovery_ms
    assert 100 <= r.distribution_ms <= 400  # two install rounds over the flooding path
    assert r.delivery_gap_ms >= r.service_recovery_ms
    assert r.readings_lost >= 0
    # the first delivery the measurement points at really did go over D
    assert delivered_via(sim, "D", after_ms=r.first_delivery_ms - 1)


# ---- b-returns ----


def test_b_returns_but_is_used_again_only_after_the_hold_down():
    sim, host, _ = run("b-returns")
    held = route_events(host, "link_held_down")
    assert {h.data["link"] for h in held} == {"A-B", "B-C"}
    assert all(h.data["hold_down_ms"] == HOLD_DOWN_MS for h in held)
    eligible_at = max(h.data["eligible_at_ms"] for h in held)
    seen_at = min(h.time_ms for h in held)
    assert eligible_at - seen_at >= HOLD_DOWN_MS
    computed = route_events(host, "route_computed")
    assert [e.data["path"] for e in computed] == [
        ["A", "B", "C"],
        ["A", "D", "C"],
        ["A", "B", "C"],
    ]
    back_to_b = computed[2]
    assert back_to_b.time_ms >= eligible_at  # not a moment before the hold-down ended
    # no reading went over B between its return (20 s) and the end of the hold-down
    assert delivered_via(sim, "B", after_ms=20_000, before_ms=back_to_b.time_ms) == []
    assert len(delivered_via(sim, "D", after_ms=20_000, before_ms=back_to_b.time_ms)) >= 2
    # ... and afterwards B is chosen again because the *simulated* B links are cheaper
    assert len(delivered_via(sim, "B", after_ms=back_to_b.time_ms)) >= 5
    assert sim.route_state("A", "C")[2] == "B"


def test_the_returned_relay_was_installed_before_a_switched_back():
    sim, host, _ = run("b-returns")
    boots = [e for e in sim.trace.of_type(NODE_BOOTED) if e.node == "B" and e.time_ms > 0]
    assert len(boots) == 1  # restarted once: its RAM, and its route entry, were lost
    version = route_events(host, "route_computed")[2].data["version"]
    installs = {
        e.node: e.time_ms
        for e in sim.trace.of_type(ROUTE_INSTALLED)
        if e.info["version"] == version
    }
    assert installs["B"] > boots[0].time_ms and installs["B"] < installs["A"]


# ---- one-way-a-b ----


def test_one_way_failure_of_a_to_b_reroutes_while_b_stays_alive():
    sim, host, _ = run("one-way-a-b")
    view = host.view
    assert view.node_status("B") == "reachable"  # B is alive and heard
    assert view.link_state("A", "B") == "one_way"
    assert [e for e in host.topology_events if e.type == "node_lost_contact"] == []
    computed = route_events(host, "route_computed")
    assert [e.data["path"] for e in computed] == [["A", "B", "C"], ["A", "D", "C"]]
    assert len(delivered_via(sim, "D", after_ms=12_000)) >= 5
    assert delivered_via(sim, "B", after_ms=12_100) == []


# ---- both-relays-fail ----


def test_both_relays_failing_reports_no_route_and_stays_bounded():
    sim, host, _ = run("both-relays-fail")
    assert [e.data["path"] for e in route_events(host, "route_computed")] == [
        ["A", "B", "C"],
        ["A", "D", "C"],
    ]
    no_route = route_events(host, "no_available_route")
    assert no_route and no_route[0].time_ms > 22_000
    # A cannot be told: both of C's neighbors are dead, so nothing leaves C. The controller
    # says so honestly: the announcement is unconfirmed, never confirmed.
    assert route_events(host, "no_route_confirmed") == []
    failed = route_events(host, "route_install_failed")
    assert failed and {f.data["state"] for f in failed} == {"unconfirmed"}
    assert {f.data["kind"] for f in failed} == {"no_route"}
    # A still holds its old route to D, so it keeps *trying* (and losing) readings: that is
    # bounded by 3 attempts each and by the pending limit, and is not "no_route" dropping
    s = sim.summary()
    assert sim.route_state("A", "C") == (1000, 2, "D")
    assert s.attempts <= 3 * s.readings_created
    assert s.unresolved <= 32


def test_no_route_announcements_stop_by_themselves():
    sim, host, spec = run("both-relays-fail")
    stats = host.controller.stats
    sent = stats.updates_sent + stats.retries
    plans = stats.plans
    sim.run(600_000)  # ten more simulated minutes with the network still dead
    assert stats.updates_sent + stats.retries == sent
    assert stats.plans == plans
    assert plans <= 10
    assert sent <= 3 * plans  # never more than the per-update attempt limit per plan
    assert sim.summary().unresolved <= 32  # the source's memory did not grow either


# ---- no-route-told-explicitly ----


def test_no_route_is_told_to_a_which_then_drops_readings_with_a_reason():
    sim, host, _ = run("no-route-told-explicitly", until_ms=28_000)
    declared = route_events(host, "no_available_route")
    assert len(declared) == 1 and declared[0].time_ms > 12_000
    confirmed = route_events(host, "no_route_confirmed")
    assert len(confirmed) == 1 and confirmed[0].data["confirmed_by"] == ["A"]
    assert sim.route_state("A", "C") == (1000, 2, None)  # explicitly "no route", not just empty
    told = [
        e
        for e in sim.trace.of_type(FRAME_DROPPED)
        if e.node == "A" and e.info["reason"] == "no_route" and e.info.get("told_by_controller")
    ]
    s = sim.summary()
    assert len(told) >= 5 and s.dropped_no_route == len(told) + 1  # +1: before the first route
    assert s.unresolved <= 2
    # nothing was sent after the announcement: the frames did not pile up anywhere
    last_send = max(
        e.time_ms
        for e in sim.trace.of_type(FRAME_SENT)
        if e.node == "A" and e.info["msg"] == "DATA"
    )
    assert last_send < confirmed[0].time_ms + 2_000


def test_the_route_returns_only_after_the_links_return_and_the_hold_down_passes():
    sim, host, _ = run("no-route-told-explicitly")
    computed = route_events(host, "route_computed")
    assert len(computed) == 2 and computed[1].time_ms >= 30_000 + HOLD_DOWN_MS
    assert sim.route_state("A", "C")[2] == "B"
    assert len(delivered_via(sim, "B", after_ms=computed[1].time_ms)) >= 3


# ---- route-update-lost and route-ack-lost ----


def test_a_lost_route_update_is_retried_and_never_shown_active_before_its_acknowledgement():
    sim, host, _ = run("route-update-lost")
    to_b = [
        e
        for e in sim.trace.of_type(ROUTE_UPDATE_SENT)
        if e.info["target"] == "B" and e.info["version"] == 1
    ]
    assert len(to_b) == 2 and to_b[1].time_ms - to_b[0].time_ms == TIMEOUT_MS
    assert sim.summary().drops["scripted"] == 2  # both copies of the first update were lost
    installed_at_b = [e for e in sim.trace.of_type(ROUTE_INSTALLED) if e.node == "B"][0]
    active = route_events(host, "route_active")[0]
    assert installed_at_b.time_ms >= to_b[1].time_ms  # only the retry reached B
    assert active.time_ms > installed_at_b.time_ms > to_b[0].time_ms
    # until the retried update was acknowledged, the route was installing, not active
    first = route_events(host, "route_computed")[0]
    assert active.time_ms - first.time_ms >= TIMEOUT_MS
    assert host.controller.stats.retries == 1
    assert len(delivered_via(sim, "B")) >= 5


def test_a_lost_acknowledgement_causes_a_resend_that_the_relay_recognises():
    sim, host, _ = run("route-ack-lost")
    installs_at_b = [e for e in sim.trace.of_type(ROUTE_INSTALLED) if e.node == "B"]
    assert len(installs_at_b) == 1  # installed once, although it was told twice
    sends = [e for e in sim.trace.of_type(ROUTE_UPDATE_SENT) if e.info["target"] == "B"]
    assert len(sends) == 2
    assert host.controller.stats.retries == 1
    active = route_events(host, "route_active")[0]
    assert active.time_ms >= sends[1].time_ms
    assert len(delivered_via(sim, "B")) >= 5


# ---- stale-update-ignored ----


def test_a_late_stale_update_does_not_overwrite_a_newer_route():
    sim, host, _ = run("stale-update-ignored")
    assert sim.route_state("A", "C") == (1000, 2, "D")
    ignored = [e for e in sim.trace.events if e.event == "route_update_ignored" and e.node == "A"]
    assert [(e.time_ms, e.info["reason"]) for e in ignored] == [
        (25_000, "stale"),
        (26_000, "stale"),
    ]
    assert [e.info["version"] for e in ignored] == [1, 77]
    # had either been applied, A would have sent to dead B; instead delivery went on via D
    assert delivered_via(sim, "B", after_ms=25_000) == []
    assert len(delivered_via(sim, "D", after_ms=25_000)) >= 6
    # the controller treated the STALE answers as old news: its route stays active
    assert route_events(host, "route_active")[-1].data["version"] == 2
    assert host.controller.route_state()["state"] == "active"
    assert host.controller.stats.acks_ignored >= 2


# ---- controller-restart ----


def test_a_restarted_controller_with_a_new_epoch_is_accepted_and_nothing_gets_stuck():
    sim, host, _ = run("controller-restart")
    first_run = [e for e in host.controllers[0].events if e.type == "route_active"]
    second_run = [e for e in host.controllers[1].events if e.type == "route_active"]
    assert [e.data["version"] for e in first_run] == [1, 2]
    assert [(e.data["epoch"], e.data["version"]) for e in second_run] == [(1001, 1)]
    # version 1 < 2, yet both nodes took it, because its epoch is newer
    assert sim.route_state("A", "C") == (1001, 1, "D")
    assert sim.route_state("D", "C") == (1001, 1, "C")
    assert [e for e in sim.trace.events if e.event == "route_update_ignored"] == []
    # the old controller sent nothing after it stopped; delivery never stopped
    stop_ms = 25_000
    assert all(e.time_ms <= stop_ms or e.data.get("epoch") == 1001 for e in host.route_events)
    assert len(delivered_via(sim, "D", after_ms=stop_ms)) >= 8
    assert delivered_via(sim, "B", after_ms=stop_ms) == []


# ---- measurements over many seeds (simulation only) ----


def test_recovery_bounds_hold_across_seeds():
    results = []
    for seed in range(1, 13):
        sim, host, spec = run("b-failure-auto-reroute", seed=seed)
        r = recovery_measurement(
            spec.actions, sim.trace.events, host.route_events, host.topology_events
        )
        assert r is not None and r.new_path == ("A", "D", "C")
        assert r.detection_ms is not None and r.service_recovery_ms is not None
        results.append(r)
    # detection is bounded by the neighbor timeout plus batching and transit; route installation
    # adds two rounds over flooded paths; the first delivery waits for the next retry or reading
    assert all(1_800 <= r.detection_ms <= 3_500 for r in results)
    assert all(r.route_active_after_ms - r.detection_ms <= 400 for r in results)
    assert all(r.service_recovery_ms <= 5_000 for r in results)
    assert all(r.readings_lost <= 2 for r in results)
