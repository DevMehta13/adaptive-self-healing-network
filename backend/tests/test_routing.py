"""Route calculation and installation, driven by a fake gateway (no simulator involved)."""

import pytest

from iot_backend.controller import (
    RouteController,
    RoutingConfig,
    TopologyConfig,
    TopologyView,
    shortest_path,
)
from iot_backend.transports import (
    NeighborObservation,
    RouteInstallAckEvent,
    RouteUpdateCommand,
    TopologyReportEvent,
)

A, B, C, D = 1, 2, 3, 4
SESSIONS = {A: 1000, B: 2000, C: 3000, D: 4000}
DIAMOND = {A: (B, D), B: (A, C), C: (B, D), D: (A, C)}
FOREVER = 10**9


class FakeGateway:
    """Implements the Gateway protocol with a manual clock and a record of commands sent."""

    def __init__(self):
        self.now = 0
        self._handlers = []
        self._timers = []
        self.sent: list[tuple[int, RouteUpdateCommand]] = []

    def subscribe(self, handler):
        self._handlers.append(handler)

    def now_ms(self):
        return self.now

    def call_later(self, delay_ms, callback):
        self._timers.append((self.now + delay_ms, callback))

    def send_route_update(self, command):
        self.sent.append((self.now, command))

    def advance(self, ms):
        end = self.now + ms
        while True:
            due = sorted((t for t in self._timers if t[0] <= end), key=lambda t: t[0])
            if not due:
                break
            when, callback = due[0]
            self._timers.remove(due[0])
            self.now = when
            callback()
        self.now = end

    def emit(self, event):
        for handler in list(self._handlers):
            handler(event)

    # ---- helpers that feed events ----

    def report(self, origin, entries, seq=1, session=None):
        """entries: {neighbor: (state, rtt_ms or None)}"""
        self.emit(
            TopologyReportEvent(
                received_ms=self.now,
                origin=origin,
                session=session or SESSIONS[origin],
                seq=seq,
                neighbors=tuple(
                    NeighborObservation(n, state, SESSIONS[n], 100, rtt)
                    for n, (state, rtt) in entries.items()
                ),
            )
        )

    def ack(self, origin, command, status="installed", epoch=None, version=None, seq=1):
        self.emit(
            RouteInstallAckEvent(
                received_ms=self.now,
                origin=origin,
                session=SESSIONS[origin],
                seq=seq,
                epoch=command.epoch if epoch is None else epoch,
                version=command.version if version is None else version,
                destination=command.destination,
                status=status,
            )
        )


def diamond_reports(gw, rtt, down=(), seq=1):
    """Each node reports its diamond neighbors. rtt[(reporter, neighbor)] in ms (None = none).

    `down` lists nodes that stopped: nobody can hear them and they do not report.
    """
    for node, neighbors in DIAMOND.items():
        if node in down:
            continue
        entries = {}
        for n in neighbors:
            if n in down:
                entries[n] = ("lost", None)
            else:
                entries[n] = ("two_way", rtt.get((node, n)))
        gw.report(node, entries, seq=seq)


def symmetric(ab=10, bc=10, ad=30, dc=30):
    out = {}
    for (x, y), value in {(A, B): ab, (B, C): bc, (A, D): ad, (D, C): dc}.items():
        out[(x, y)] = out[(y, x)] = value
    return out


def make(config=None, **kw):
    gw = FakeGateway()
    view = TopologyView(gw, TopologyConfig(report_expiry_ms=FOREVER))
    config = config or RoutingConfig(initial_settle_ms=0, no_route_grace_ms=10_000, **kw)
    return gw, view, RouteController(view, gw, config)


def types(controller):
    return [e.type for e in controller.events]


def events_of(controller, type_):
    return [e for e in controller.events if e.type == type_]


def activate(gw, rounds=4):
    """Acknowledge every update the controller sends until nothing more is pending."""
    done = 0
    for _ in range(rounds):
        pending = gw.sent[done:]
        if not pending:
            return
        done = len(gw.sent)
        for _, command in pending:
            gw.ack(command.node, command)


# ---- Dijkstra ----


def test_dijkstra_picks_the_cheaper_path():
    links = [(A, B, 10), (B, C, 10), (A, D, 30), (D, C, 30)]
    result = shortest_path(links, A, C)
    assert (result.path, result.cost_ms) == ((A, B, C), 20)


def test_dijkstra_prefers_the_other_relay_when_it_is_cheaper():
    links = [(A, B, 50), (B, C, 50), (A, D, 20), (D, C, 20)]
    assert shortest_path(links, A, C).path == (A, D, C)


def test_dijkstra_tie_break_is_deterministic_and_independent_of_input_order():
    links = [(A, B, 10), (B, C, 10), (A, D, 10), (D, C, 10)]
    forward = shortest_path(links, A, C)
    backward = shortest_path(list(reversed(links)), A, C)
    swapped = shortest_path([(y, x, w) for x, y, w in links], A, C)
    assert forward.path == backward.path == swapped.path == (A, B, C)  # lower node id first


def test_dijkstra_tie_break_prefers_fewer_hops_before_lower_ids():
    links = [(A, C, 20), (A, B, 10), (B, C, 10)]  # a direct link costing the same as via B
    assert shortest_path(links, A, C).path == (A, C)


def test_dijkstra_no_path_and_validation():
    assert shortest_path([(A, B, 5)], A, C) is None
    assert shortest_path([], A, C) is None
    with pytest.raises(ValueError):
        shortest_path([(A, B, -1), (B, C, 1)], A, C)
    assert shortest_path([(A, B, 0), (B, C, 0)], A, C).cost_ms == 0  # zero is legal when measured


# ---- the graph is built from usable, measured links only ----


def test_route_is_computed_from_measured_two_way_links_and_costs_use_the_larger_end():
    gw, view, ctl = make()
    rtt = symmetric()
    rtt[(A, B)], rtt[(B, A)] = 8, 14  # the two ends disagree: the link costs 14
    diamond_reports(gw, rtt)
    (computed,) = events_of(ctl, "route_computed")
    assert computed.data["path"] == ["A", "B", "C"]
    assert computed.data["cost_ms"] == 14 + 10
    assert {"link": "A-B", "cost_ms": 14} in computed.data["graph"]


def test_an_unmeasured_link_is_never_a_zero_cost_link():
    gw, view, ctl = make()
    rtt = symmetric(ab=1, bc=1, ad=500, dc=500)  # via B would be far cheaper ...
    rtt[(B, A)] = None  # ... but B has not measured the A-B link
    diamond_reports(gw, rtt)
    (computed,) = events_of(ctl, "route_computed")
    assert computed.data["path"] == ["A", "D", "C"]  # the measured, more expensive path
    assert view.unmeasured_links() == [("A", "B")]
    assert all(g["link"] != "A-B" for g in computed.data["graph"])


def test_with_no_measurements_at_all_there_is_no_route_not_a_free_route():
    gw, view, ctl = make()
    diamond_reports(gw, {})  # every link two-way but nothing measured
    gw.advance(10_001)  # past the start-up grace period
    assert events_of(ctl, "route_computed") == []
    assert types(ctl)[0] == "no_available_route"
    assert gw.sent[0][1].next_hop is None


def test_one_way_and_stale_links_are_not_used():
    gw, view, ctl = make()
    rtt = symmetric()
    # A hears B but B does not hear A: one-way, so unusable even though measured
    for node, neighbors in DIAMOND.items():
        entries = {n: ("two_way", rtt[(node, n)]) for n in neighbors}
        if node == B:
            entries[A] = ("lost", None)
        gw.report(node, entries)
    (computed,) = events_of(ctl, "route_computed")
    assert computed.data["path"] == ["A", "D", "C"]


# ---- installation: order, acknowledgements, retries ----


def test_relays_are_installed_and_acknowledged_before_the_source_is_told():
    gw, view, ctl = make()
    diamond_reports(gw, symmetric())
    assert [(c.node, c.next_hop) for _, c in gw.sent] == [(B, C)]  # only the relay so far
    assert ctl.route_state()["state"] == "installing"
    gw.ack(B, gw.sent[0][1])
    assert [(c.node, c.next_hop) for _, c in gw.sent] == [(B, C), (A, B)]
    assert ctl.route_state()["state"] == "installing"  # not active until the source confirms
    assert "route_active" not in types(ctl)
    gw.ack(A, gw.sent[1][1])
    assert ctl.route_state()["state"] == "active"
    assert types(ctl) == [
        "route_computed",
        "route_installing",
        "route_installing",
        "route_active",
    ]
    assert events_of(ctl, "route_active")[0].data["confirmed_by"] == ["B", "A"]


def test_commands_carry_epoch_version_destination_and_no_backup():
    gw, view, ctl = make(epoch=777)
    diamond_reports(gw, symmetric())
    activate(gw)
    for _, command in gw.sent:
        assert (command.epoch, command.version, command.destination) == (777, 1, C)
        assert command.backup_next_hop is None  # Phase 4a never sends a backup


def test_an_acknowledgement_for_another_version_never_activates_a_route():
    gw, view, ctl = make()
    diamond_reports(gw, symmetric())
    command = gw.sent[0][1]
    gw.ack(B, command, version=command.version + 5)
    gw.ack(B, command, epoch=command.epoch + 1)
    assert len(gw.sent) == 1  # the source has not been told: the relay is not confirmed
    assert ctl.stats.acks_ignored == 2
    assert ctl.route_state()["state"] == "installing"


def test_unacknowledged_updates_are_retried_a_bounded_number_of_times():
    gw, view, ctl = make(install_timeout_ms=1000, install_max_attempts=3, replan_delay_ms=10**6)
    diamond_reports(gw, symmetric())
    gw.advance(10_000)
    assert [c.node for _, c in gw.sent] == [B, B, B]  # first send plus two retries
    assert [t for t, _ in gw.sent] == [0, 1000, 2000]
    failed = events_of(ctl, "route_install_failed")
    assert len(failed) == 1 and failed[0].data["state"] == "unconfirmed"
    assert failed[0].data["nodes"] == ["B"]
    assert ctl.route_state()["state"] == "unconfirmed"
    assert "route_active" not in types(ctl)  # never shown as active without acknowledgements
    assert all(c.node != A for _, c in gw.sent)  # the source was never told


def test_failed_installs_are_replanned_but_only_a_bounded_number_of_times():
    gw, view, ctl = make(
        install_timeout_ms=1000, install_max_attempts=2, replan_delay_ms=500, max_failed_plans=2
    )
    diamond_reports(gw, symmetric())
    gw.advance(60_000)
    sent = len(gw.sent)
    assert sent == 2 * (1 + 2)  # three plans (the first plus two replans), two sends each
    gw.advance(600_000)
    assert len(gw.sent) == sent  # nothing more, ever, until the topology changes
    assert ctl.route_state()["state"] == "unconfirmed"
    # a topology change starts afresh
    diamond_reports(gw, symmetric(), down=(D,), seq=2)
    gw.advance(1)
    assert len(gw.sent) > sent


def test_a_node_that_refuses_the_update_fails_the_route_immediately():
    gw, view, ctl = make(replan_delay_ms=10**6)
    diamond_reports(gw, symmetric())
    gw.ack(B, gw.sent[0][1], status="rejected")
    (failed,) = events_of(ctl, "route_install_failed")
    assert (failed.data["state"], failed.data["reason"]) == ("failed", "rejected by node")
    assert ctl.route_state()["state"] == "failed"


# ---- recalculation: topology changes, not RTT noise; hold-down ----


def test_rtt_changes_alone_do_not_trigger_a_new_route():
    gw, view, ctl = make()
    diamond_reports(gw, symmetric())
    activate(gw)
    assert ctl.stats.plans == 1
    for seq in range(2, 8):  # B's links get much worse, then better, then worse ...
        diamond_reports(gw, symmetric(ab=10 + 100 * (seq % 2), bc=10, ad=30, dc=30), seq=seq)
        gw.advance(1_000)
    assert ctl.stats.plans == 1 and len(events_of(ctl, "route_computed")) == 1


def test_a_lost_node_triggers_a_new_route_through_the_other_relay():
    gw, view, ctl = make()
    diamond_reports(gw, symmetric())
    activate(gw)
    diamond_reports(gw, symmetric(), down=(B,), seq=2)
    computed = events_of(ctl, "route_computed")[-1]
    assert computed.data["path"] == ["A", "D", "C"] and computed.data["version"] == 2
    assert computed.data["trigger"] == "topology_changed"
    assert gw.sent[-1][1].node == D  # relay first
    activate(gw)
    assert ctl.route_state()["path"] == ["A", "D", "C"]


def test_a_returning_link_is_held_down_before_routes_use_it():
    gw, view, ctl = make(hold_down_ms=5000)
    diamond_reports(gw, symmetric())
    activate(gw)
    diamond_reports(gw, symmetric(), down=(B,), seq=2)  # B lost; route moves to D
    activate(gw)
    gw.advance(1_000)
    returned_at = gw.now
    diamond_reports(gw, symmetric(), seq=3)  # B is back, cheaper than D, and fully measured
    held = events_of(ctl, "link_held_down")
    assert {h.data["link"] for h in held} == {"A-B", "B-C"}
    assert all(h.data["hold_down_ms"] == 5000 for h in held)
    gw.advance(4_999)
    assert events_of(ctl, "route_computed")[-1].data["path"] == ["A", "D", "C"]  # not yet
    gw.advance(1)
    computed = events_of(ctl, "route_computed")[-1]
    assert computed.data["path"] == ["A", "B", "C"]
    assert computed.time_ms == returned_at + 5000


def test_a_flapping_link_restarts_its_hold_down():
    gw, view, ctl = make(hold_down_ms=5000)
    diamond_reports(gw, symmetric())
    activate(gw)
    diamond_reports(gw, symmetric(), down=(B,), seq=2)
    activate(gw)
    gw.advance(1_000)
    diamond_reports(gw, symmetric(), seq=3)  # back ...
    gw.advance(3_000)
    diamond_reports(gw, symmetric(), down=(B,), seq=4)  # ... gone again after 3 s
    gw.advance(1_000)
    diamond_reports(gw, symmetric(), seq=5)  # ... and back: the clock starts over
    gw.advance(4_000)
    assert events_of(ctl, "route_computed")[-1].data["path"] == ["A", "D", "C"]
    gw.advance(1_000)
    assert events_of(ctl, "route_computed")[-1].data["path"] == ["A", "B", "C"]


def test_the_first_appearance_of_a_link_is_not_held_down():
    gw, view, ctl = make(hold_down_ms=5000)
    diamond_reports(gw, symmetric())
    assert events_of(ctl, "route_computed") and not events_of(ctl, "link_held_down")


def test_a_restarted_relay_on_the_route_gets_a_new_version():
    gw, view, ctl = make()
    diamond_reports(gw, symmetric())
    activate(gw)
    sessions = dict(SESSIONS)
    SESSIONS[B] = 2001  # B rebooted so fast that nobody declared it lost
    try:
        diamond_reports(gw, symmetric(), seq=2)
    finally:
        SESSIONS.update(sessions)
    computed = events_of(ctl, "route_computed")[-1]
    assert computed.data["version"] == 2 and computed.data["trigger"] == "node_restarted"
    assert gw.sent[-1][1].node == B  # its RAM is empty, so it is told again, relay first


# ---- no route ----


def test_no_path_reports_no_available_route_and_tells_the_source_explicitly():
    gw, view, ctl = make()
    diamond_reports(gw, symmetric())
    activate(gw)
    diamond_reports(gw, symmetric(), down=(B, D), seq=2)
    (event,) = events_of(ctl, "no_available_route")
    assert event.data["reason"] == "no usable measured path"
    command = gw.sent[-1][1]
    assert (command.node, command.next_hop, command.destination) == (A, None, C)
    assert ctl.route_state()["kind"] == "no_route" and ctl.route_state()["state"] == "installing"
    gw.ack(A, command)
    assert types(ctl)[-1] == "no_route_confirmed"
    assert ctl.route_state()["state"] == "active"


def test_telling_the_source_is_bounded_too():
    gw, view, ctl = make(install_max_attempts=3, install_timeout_ms=1000, max_failed_plans=1)
    diamond_reports(gw, symmetric())
    activate(gw)
    before = len(gw.sent)
    diamond_reports(gw, symmetric(), down=(B, D), seq=2)
    gw.advance(120_000)
    assert len(gw.sent) - before == 3 + 3  # one plan plus one replan, three sends each
    assert "no_route_confirmed" not in types(ctl)


def test_silence_right_after_start_is_discovery_not_no_route():
    gw = FakeGateway()
    view = TopologyView(gw, TopologyConfig(report_expiry_ms=FOREVER))
    ctl = RouteController(view, gw, RoutingConfig(no_route_grace_ms=5000, initial_settle_ms=0))
    gw.advance(4_000)
    assert ctl.events == []
    gw.advance(2_000)
    assert types(ctl)[:1] == ["no_available_route"]


def test_first_route_waits_for_the_set_of_links_to_settle():
    gw = FakeGateway()
    view = TopologyView(gw, TopologyConfig(report_expiry_ms=FOREVER))
    ctl = RouteController(view, gw, RoutingConfig(initial_settle_ms=1000, no_route_grace_ms=10_000))
    rtt = symmetric()
    # only the D side is measured at first; the cheaper B side shows up 300 ms later
    gw.report(A, {D: ("two_way", rtt[(A, D)])})
    gw.report(D, {A: ("two_way", rtt[(D, A)]), C: ("two_way", rtt[(D, C)])})
    gw.report(C, {D: ("two_way", rtt[(C, D)])})
    gw.advance(300)
    diamond_reports(gw, rtt, seq=2)
    assert events_of(ctl, "route_computed") == []  # still settling
    gw.advance(1_000)
    computed = events_of(ctl, "route_computed")
    assert [e.data["path"] for e in computed] == [["A", "B", "C"]]  # one route, the right one


# ---- versions and epochs at the controller ----


def test_a_newer_epoch_held_by_a_node_advances_the_controllers_epoch():
    gw, view, ctl = make(epoch=100)
    diamond_reports(gw, symmetric())
    first = gw.sent[0][1]
    gw.ack(B, first, status="stale", epoch=250, version=9)  # B holds epoch 250, version 9
    (advanced,) = events_of(ctl, "epoch_advanced")
    assert advanced.data == {"old_epoch": 100, "held_epoch": 250, "epoch": 251}
    assert ctl.epoch == 251
    latest = gw.sent[-1][1]
    assert (latest.epoch, latest.version) == (251, 1)  # versions start over under the new epoch
    activate(gw)
    assert ctl.route_state()["state"] == "active" and ctl.route_state()["version"] == 1


def test_a_node_holding_a_higher_version_in_our_epoch_makes_the_controller_catch_up():
    gw, view, ctl = make(epoch=100, replan_delay_ms=500)
    diamond_reports(gw, symmetric())
    gw.ack(B, gw.sent[0][1], status="stale", epoch=100, version=40)
    assert events_of(ctl, "route_install_failed")[0].data["reason"] == "node holds a newer version"
    gw.advance(1_000)
    assert gw.sent[-1][1].version == 41  # above what the node holds


def test_a_late_stale_acknowledgement_about_an_old_update_is_ignored():
    gw, view, ctl = make(epoch=100)
    diamond_reports(gw, symmetric())
    activate(gw)
    diamond_reports(gw, symmetric(), down=(B,), seq=2)  # version 2 now
    plan_version = ctl.route_state()["version"]
    # a node answers an old, retried update with its held pair, which is older than our plan
    gw.emit(RouteInstallAckEvent(gw.now, A, SESSIONS[A], 9, 100, 1, C, "stale"))
    assert ctl.route_state()["version"] == plan_version
    assert ctl.epoch == 100


def test_a_stopped_controller_sends_and_reacts_to_nothing():
    gw, view, ctl = make()
    ctl.stop()
    diamond_reports(gw, symmetric())
    gw.advance(10_000)
    assert gw.sent == [] and ctl.events == []


# ---- the controller is built on the gateway interface only ----


def test_routing_module_does_not_import_the_simulator():
    import ast
    from pathlib import Path

    import iot_backend.controller.routing as routing

    tree = ast.parse(Path(routing.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if "simulator" in name]
