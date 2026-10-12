"""Backup next hops: the loop-free, node-protecting rule, installation order, and how the
controller reacts to a node's local failover. Driven by a fake gateway; no simulator involved."""

import random

import pytest
from test_routing import (  # the same fake gateway and diamond helpers
    FOREVER,
    SESSIONS,
    A,
    B,
    C,
    D,
    FakeGateway,
    diamond_reports,
    events_of,
    symmetric,
    types,
)

from iot_backend.controller import (
    RouteController,
    RoutingConfig,
    TopologyConfig,
    TopologyView,
    protecting_backups,
    shortest_path,
)
from iot_backend.transports import LocalFailoverEvent

E, F = 5, 6


def links(*edges):
    return [(a, b, w) for a, b, w in edges]


# ---- the rule ----


def test_diamond_backup_for_a_is_d_and_relays_have_none():
    graph = links((A, B, 10), (B, C, 10), (A, D, 30), (D, C, 30))
    plan = protecting_backups(graph, (A, B, C))
    assert plan.backups == {A: D}  # A: if B fails, D reaches C without A or B
    assert plan.entries == {D: C}  # D must forward to C: its entry is installed with the route
    # B's only other neighbor is A, and A reaches C *through B*: no valid backup for B


def test_when_d_is_primary_a_backs_up_through_b():
    graph = links((A, B, 10), (B, C, 10), (A, D, 5), (D, C, 5))
    plan = protecting_backups(graph, (A, D, C))
    assert plan.backups == {A: B} and plan.entries == {B: C}


def test_a_neighbor_that_would_loop_back_is_rejected():
    # E hangs off A only: its own best path to C is E - A - B - C, back through A.
    graph = links((A, B, 1), (B, C, 1), (A, E, 1))
    assert protecting_backups(graph, (A, B, C)).backups == {}


def test_loop_free_but_not_node_protecting_is_rejected():
    # D does not loop back through A, but its best path to C runs D - B - C: through the
    # primary next hop B. When B fails, D's path fails too, so D is no protection at all.
    graph = links((A, B, 1), (B, C, 1), (A, D, 5), (D, B, 1))
    assert shortest_path(graph, D, C).path == (D, B, C)
    assert protecting_backups(graph, (A, B, C)).backups == {}


def test_a_backup_that_avoids_both_is_accepted_in_a_bigger_graph():
    # A - B - E - C primary; D gives A a path D - F - C that avoids A, B and E
    graph = links((A, B, 1), (B, E, 1), (E, C, 1), (A, D, 2), (D, F, 2), (F, C, 2), (B, D, 9))
    path = shortest_path(graph, A, C).path
    assert path == (A, B, E, C)
    plan = protecting_backups(graph, path)
    assert plan.backups[A] == D
    assert plan.entries == {D: F, F: C}  # every node on the backup path gets an entry


def test_no_valid_backup_means_none():
    graph = links((A, B, 1), (B, C, 1))
    assert protecting_backups(graph, (A, B, C)).backups == {}
    assert protecting_backups(graph, (A, B, C)).entries == {}


@pytest.mark.parametrize("seed", range(40))
def test_random_topologies_never_produce_a_forwarding_loop_or_a_backup_through_the_failed_hop(seed):
    """Property check on random graphs with 8 nodes: follow every backup to the end."""
    rng = random.Random(seed)
    nodes = list(range(1, 9))
    edges = {}
    for a in nodes:
        for b in nodes:
            if a < b and rng.random() < 0.45:
                edges[(a, b)] = rng.randint(1, 20)
    graph = [(a, b, w) for (a, b), w in edges.items()]
    primary = shortest_path(graph, 1, 8)
    if primary is None or len(primary.path) < 3:
        return
    path = primary.path
    plan = protecting_backups(graph, path)
    on_path = {n: path[i + 1] for i, n in enumerate(path[:-1])}
    for node, backup in plan.backups.items():
        failed = on_path[node]
        assert backup != failed
        # the failure of `failed` is simulated: frames follow each node's installed next hop
        # (primary entry for route nodes, backup-path entries for the others)
        visited = [node, backup]
        here = backup
        while here != 8:
            nxt = plan.entries.get(here, on_path.get(here))
            assert nxt is not None, f"seed {seed}: {here} has no entry on the backup path"
            assert nxt != failed, f"seed {seed}: backup of {node} relies on failed {failed}"
            assert nxt != node, f"seed {seed}: backup of {node} loops back"
            assert nxt not in visited, f"seed {seed}: loop {visited} -> {nxt}"
            assert (min(here, nxt), max(here, nxt)) in edges, "entry uses a non-existent link"
            visited.append(nxt)
            here = nxt
        assert failed not in visited and visited.count(node) == 1


# ---- installation order ----


def make(**kw):
    gw = FakeGateway()
    view = TopologyView(gw, TopologyConfig(report_expiry_ms=FOREVER))
    config = RoutingConfig(
        initial_settle_ms=1, no_route_grace_ms=10_000, backup_next_hops=True, **kw
    )
    return gw, view, RouteController(view, gw, config)


def test_backup_relay_is_installed_first_and_the_route_is_not_active_before_it_is_confirmed():
    gw, view, ctl = make()
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    assert [(c.node, c.next_hop, c.backup_next_hop) for _, c in gw.sent] == [(D, C, None)]
    state = ctl.route_state()
    assert state["backups"] == {"A": "D"} and state["backup_entries"] == {"D": "C"}
    gw.ack(D, gw.sent[-1][1])
    assert [(c.node, c.next_hop) for _, c in gw.sent][-1] == (B, C)  # then the primary relay
    assert "route_active" not in types(ctl)
    gw.ack(B, gw.sent[-1][1])
    last = gw.sent[-1][1]
    assert (last.node, last.next_hop, last.backup_next_hop) == (A, B, D)  # source last
    assert "route_active" not in types(ctl)
    gw.ack(A, last)
    active = events_of(ctl, "route_active")[0]
    assert active.data["confirmed_by"] == ["D", "B", "A"]
    phases = [e.data["phase"] for e in events_of(ctl, "route_installing")]
    assert phases == ["backup_relays", "relays", "source"]


def test_a_route_with_an_unconfirmed_backup_entry_never_becomes_active():
    gw, view, ctl = make(install_timeout_ms=1000, install_max_attempts=2, replan_delay_ms=10**6)
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    gw.advance(10_000)  # D never acknowledges
    assert "route_active" not in types(ctl)
    assert {c.node for _, c in gw.sent} == {D}  # the primary relay and A were never told
    assert ctl.route_state()["state"] == "unconfirmed"


def test_without_backup_next_hops_the_route_is_exactly_the_phase_4a_route():
    gw = FakeGateway()
    view = TopologyView(gw, TopologyConfig(report_expiry_ms=FOREVER))
    ctl = RouteController(view, gw, RoutingConfig(initial_settle_ms=1, no_route_grace_ms=10_000))
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    assert [(c.node, c.backup_next_hop) for _, c in gw.sent] == [(B, None)]
    assert ctl.route_state()["backups"] == {}


def test_a_backup_that_stops_being_valid_gets_a_new_version_with_it_withdrawn():
    gw, view, ctl = make()
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    for _ in range(3):
        for _, c in list(gw.sent):
            gw.ack(c.node, c)
    assert ctl.route_state()["state"] == "active" and ctl.route_state()["backups"] == {"A": "D"}
    diamond_reports(gw, symmetric(), down=(D,), seq=2)  # D is gone; the primary path is intact
    computed = events_of(ctl, "route_computed")[-1]
    assert computed.data["path"] == ["A", "B", "C"] and computed.data["version"] == 2
    assert computed.data["backups"] == {}
    assert gw.sent[-1][1].backup_next_hop is None or gw.sent[-1][1].node != A


# ---- reacting to a node's local failover ----


def active_route(gw, ctl):
    done = 0
    for _ in range(5):
        pending = gw.sent[done:]
        if not pending:
            return
        done = len(gw.sent)
        for _, c in pending:
            gw.ack(c.node, c)


def failover_report(gw, origin=A, from_hop=B, to_hop=D, epoch=1, version=1, reason="send_failures"):
    gw.emit(
        LocalFailoverEvent(
            received_ms=gw.now,
            origin=origin,
            session=SESSIONS[origin],
            seq=gw.now,
            epoch=epoch,
            version=version,
            destination=C,
            from_next_hop=from_hop,
            to_next_hop=to_hop,
            reason=reason,
        )
    )


def test_a_failover_report_is_recorded_distinctly_and_the_backup_path_is_confirmed():
    gw, view, ctl = make(epoch=1)
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    active_route(gw, ctl)
    before = len(gw.sent)
    failover_report(gw)  # A says: I moved from B to D (the controller's view still shows B up)
    (event,) = events_of(ctl, "local_failover")
    assert event.data["node"] == "A" and event.data["from"] == "B" and event.data["to"] == "D"
    assert event.data["outcome"] == "backup_in_use" and event.data["matches_current_route"]
    computed = events_of(ctl, "route_computed")[-1]
    assert computed.data["trigger"] == "local_failover_report"
    assert computed.data["path"] == ["A", "D", "C"]
    sent = [c for _, c in gw.sent[before:]]
    assert [(c.node, c.next_hop) for c in sent] == [(D, C)]  # relay first, then A after its ack
    gw.ack(D, sent[0])
    assert (gw.sent[-1][1].node, gw.sent[-1][1].next_hop) == (A, D)
    gw.ack(A, gw.sent[-1][1])
    active = events_of(ctl, "route_active")[-1]
    assert active.data["trigger"] == "local_failover_report"
    assert active.data["path"] == ["A", "D", "C"]


def test_the_controller_never_reinstalls_the_primary_the_node_just_abandoned():
    gw, view, ctl = make(epoch=1, hold_down_ms=5000)
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    active_route(gw, ctl)
    failover_report(gw)
    active_route(gw, ctl)
    first = len(gw.sent)
    # the topology view still says B is up and cheaper; reports keep arriving
    for step in range(1, 5):
        gw.advance(1000)
        diamond_reports(gw, symmetric(), seq=1 + step)
    assert all(c.next_hop != B for _, c in gw.sent[first:] if c.node == A)
    assert events_of(ctl, "link_held_down")[0].data["link"] == "A-B"
    gw.advance(1100)
    diamond_reports(gw, symmetric(), seq=9)
    # only after the hold-down, by the normal policy, does the route go back through B
    assert events_of(ctl, "route_computed")[-1].data["path"] == ["A", "B", "C"]


def test_a_repeated_failover_report_is_not_acted_on_twice():
    gw, view, ctl = make(epoch=1)
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    active_route(gw, ctl)
    failover_report(gw)
    plans = ctl.stats.plans
    failover_report(gw)
    failover_report(gw)
    assert ctl.stats.plans == plans
    assert len(events_of(ctl, "local_failover")) == 1
    assert ctl.stats.failover_reports_repeated == 2


def test_a_no_route_report_is_recorded_as_such():
    gw, view, ctl = make(epoch=1)
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    active_route(gw, ctl)
    failover_report(gw, from_hop=D, to_hop=None, reason="neighbor_lost")
    assert events_of(ctl, "local_failover")[0].data["outcome"] == "no_route"


def test_silence_is_not_a_failure_a_blind_controller_keeps_the_route():
    gw = FakeGateway()
    view = TopologyView(gw, TopologyConfig(report_expiry_ms=10_000))
    ctl = RouteController(
        view,
        gw,
        RoutingConfig(initial_settle_ms=1, no_route_grace_ms=10_000, backup_next_hops=True),
    )
    diamond_reports(gw, symmetric())
    gw.advance(2)  # the first route waits for the link set to settle
    active_route(gw, ctl)
    count = len(ctl.events)
    gw.advance(120_000)  # no reports at all: every link's information has expired
    assert len(ctl.events) == count  # no "no available route", no replacement, no hold-down
    assert ctl.route_state()["state"] == "active"
    diamond_reports(gw, symmetric(), seq=2)  # reports resume
    gw.advance(10_000)
    assert len(ctl.events) == count  # and nothing churns when they do
