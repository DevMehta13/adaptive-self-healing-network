"""Phase 9: the two routing policies and the hysteresis rule, driven by a fake gateway.

No simulator is involved: reports are fed by hand on a manual clock, so every number in an
assertion is one the test put there.
"""

import math

import pytest
from test_routing import DIAMOND, FOREVER, SESSIONS, A, B, C, D, FakeGateway, activate

from iot_backend.controller import (
    RouteController,
    RoutingConfig,
    TopologyConfig,
    TopologyView,
)
from iot_backend.controller.quality import LinkQuality
from iot_backend.controller.routing import (
    POLICIES,
    RELIABILITY_FLOOR,
    path_delivery,
    reliability_cost,
)
from iot_backend.transports import NeighborObservation, TopologyReportEvent

EVAL_MS = 5_000


def observation(neighbor, rtt, ok_total, state="two_way"):
    ok, total = ok_total if ok_total else (None, None)
    return NeighborObservation(neighbor, state, SESSIONS[neighbor], 100, rtt, ok, total)


class Net:
    """A diamond whose link RTTs and probe results the test sets, reported on demand."""

    def __init__(self, policy, ab=(10, (20, 20)), bc=(10, (20, 20)), ad=(30, (20, 20)), dc=None):
        self.gw = FakeGateway()
        self.view = TopologyView(self.gw, TopologyConfig(report_expiry_ms=FOREVER))
        self.config = RoutingConfig(
            initial_settle_ms=1_000,
            no_route_grace_ms=10_000,
            policy=policy,
            backup_next_hops=False,
        )
        self.controller = RouteController(self.view, self.gw, self.config)
        self.link = {(A, B): ab, (B, C): bc, (A, D): ad, (D, C): dc or ad}
        self.seq = 0
        self.down = set()

    def set(self, x, y, rtt, ok_total):
        self.link[(x, y) if (x, y) in self.link else (y, x)] = (rtt, ok_total)

    def report_all(self, sessions=None):
        self.seq += 1
        for node, neighbors in DIAMOND.items():
            if node in self.down:
                continue
            entries = []
            for n in neighbors:
                if n in self.down:
                    entries.append(observation(n, None, None, state="lost"))
                    continue
                rtt, stats = self.link.get((node, n)) or self.link[(n, node)]
                entries.append(observation(n, rtt, stats))
            self.gw.emit(
                TopologyReportEvent(
                    received_ms=self.gw.now,
                    origin=node,
                    session=(sessions or SESSIONS)[node],
                    seq=self.seq,
                    neighbors=tuple(entries),
                )
            )

    def settle(self):
        """Report, let the controller act, acknowledge installs."""
        self.report_all()
        self.gw.advance(1_500)  # past the controller's start-up settle time
        activate(self.gw)

    def tick(self):
        """Fresh reports now, then run to the controller's next evaluation (a multiple of 5 s)."""
        self.report_all()
        self.gw.advance(EVAL_MS - self.gw.now % EVAL_MS)
        activate(self.gw)

    @property
    def path(self):
        return self.controller.route_state().get("path")

    def events(self, type_):
        return [e for e in self.controller.events if e.type == type_]


# ---- costs ----


def test_reliability_cost_is_never_negative_and_finite_for_every_probability():
    for p in [0.0, 0.001, RELIABILITY_FLOOR, 0.25, 0.5, 0.95, 1.0, 1.5, -1.0]:
        cost = reliability_cost(p)
        assert isinstance(cost, int)
        assert cost >= 0


def test_reliability_cost_is_minus_log_p_and_falls_as_p_rises():
    assert reliability_cost(1.0) == 0
    assert reliability_cost(0.5) == round(-math.log(0.5) * 1000)
    costs = [reliability_cost(p / 20) for p in range(1, 21)]
    assert costs == sorted(costs, reverse=True)
    assert reliability_cost(0.0) == reliability_cost(RELIABILITY_FLOOR)  # floored, not infinite


def test_adding_costs_multiplies_probabilities():
    # the reason for -log p: the cheapest path is the one most likely to get through
    two_links = reliability_cost(0.9) + reliability_cost(0.8)
    assert path_delivery(two_links) == pytest.approx(0.72, abs=0.001)


# ---- the estimates ----


def test_ewma_starts_at_the_first_sample_and_moves_by_the_smoothing_factor():
    quality = LinkQuality(alpha=0.2)
    quality.observe(A, 1, (observation(B, 100, None),))
    quality.observe(B, 1, (observation(A, 100, None),))
    assert quality.smoothed_rtt_ms(A, B) == 100
    quality.observe(A, 1, (observation(B, 200, None),))
    assert quality.smoothed_rtt_ms(A, B) == 120  # A's end: 0.2 * 200 + 0.8 * 100; B's still 100
    quality.observe(A, 1, (observation(B, 200, None),))
    assert quality.smoothed_rtt_ms(A, B) == 136  # 0.2 * 200 + 0.8 * 120
    quality.observe(B, 1, (observation(A, 300, None),))
    assert quality.smoothed_rtt_ms(A, B) == 140  # B's end: 0.2 * 300 + 0.8 * 100; the larger wins
    assert LinkQuality(alpha=1.0).alpha == 1.0


def test_a_link_needs_both_ends_and_takes_the_larger_rtt_and_the_smaller_probability():
    quality = LinkQuality()
    quality.observe(A, 1, (observation(B, 30, (18, 20)),))
    assert quality.smoothed_rtt_ms(A, B) is None  # one end only: never a default
    assert quality.delivery_probability(A, B) is None
    quality.observe(B, 1, (observation(A, 50, (20, 20)),))
    assert quality.smoothed_rtt_ms(A, B) == 50
    assert quality.delivery_probability(A, B) == pytest.approx(0.9)


def test_an_unmeasured_report_forgets_the_estimate_and_a_restart_starts_over():
    quality = LinkQuality()
    quality.observe(A, 1, (observation(B, 30, (10, 10)),))
    quality.observe(B, 1, (observation(A, 30, (10, 10)),))
    assert quality.smoothed_rtt_ms(A, B) == 30
    quality.observe(A, 1, (observation(B, None, None),))
    assert quality.smoothed_rtt_ms(A, B) is None and quality.delivery_probability(A, B) is None
    quality.observe(A, 1, (observation(B, 90, (5, 10)),))
    assert quality.smoothed_rtt_ms(A, B) == 90  # started again from the new sample
    quality.observe(A, 2, (observation(B, 40, (10, 10)),))  # A restarted: a new session
    assert quality.smoothed_rtt_ms(A, B) == 40
    quality.observe(A, 2, (observation(B, 40, (10, 10), state="lost"),))
    assert quality.delivery_probability(A, B) is None  # a lost neighbor has no link to rate


def test_invalid_smoothing_factor_is_refused():
    for bad in (0, -0.1, 1.5):
        with pytest.raises(ValueError):
            LinkQuality(alpha=bad)


def test_routing_config_validates_its_policy_settings():
    with pytest.raises(ValueError):
        RoutingConfig(policy="fastest")
    with pytest.raises(ValueError):
        RoutingConfig(hysteresis_evaluations=0)
    with pytest.raises(ValueError):
        RoutingConfig(policy_eval_interval_ms=0)
    assert RoutingConfig().policy == "immediate_rtt"  # Phase 4 behaviour stays the default
    assert set(POLICIES) == {"immediate_rtt", "lowest_delay", "highest_reliability"}


# ---- lowest delay ----


def test_lowest_delay_starts_on_the_cheaper_path_and_names_its_policy():
    net = Net("lowest_delay")
    net.settle()
    assert net.path == ["A", "B", "C"]
    computed = net.events("route_computed")[0].data
    assert computed["policy"] == "lowest_delay"
    assert computed["cost_ms"] == 20 and computed["delivery_probability"] is None
    assert net.controller.route_state()["policy"] == "lowest_delay"


def test_a_better_route_must_stay_better_for_three_evaluations():
    net = Net("lowest_delay")
    net.settle()
    net.gw.advance(EVAL_MS)  # settle in the controller's own timer cycle
    net.set(A, B, 100, (20, 20))  # B's links become slow: D (30 + 30) is now cheaper
    net.set(B, C, 100, (20, 20))
    seen = []
    while net.path == ["A", "B", "C"] and len(seen) < 60:
        net.tick()
        candidates = net.events("route_candidate")
        seen.append(candidates[-1].data["evaluations"] if candidates else 0)
    assert net.path == ["A", "D", "C"]
    # it counted 1, 2, 3 in a row and switched on the third
    run = [n for n in seen if n]
    assert run[-3:] == [1, 2, 3]
    switched = net.events("route_computed")[-1].data
    assert switched["trigger"] == "policy_better_route"
    assert switched["policy"] == "lowest_delay"


def test_a_dip_that_does_not_last_three_evaluations_never_changes_the_route():
    net = Net("lowest_delay")
    net.settle()
    assert net.path == ["A", "B", "C"]
    # One slow report: each B link's smoothed RTT moves 10 -> 32, so B (64) is worse than D (60)
    # for exactly one evaluation, then recovers (32 -> 27.6 each) and is better again.
    net.set(A, B, 120, (20, 20))
    net.set(B, C, 120, (20, 20))
    net.tick()
    assert net.controller.route_state()["pending_switch"]["evaluations"] == 1
    net.set(A, B, 10, (20, 20))
    net.set(B, C, 10, (20, 20))
    for _ in range(12):
        net.tick()
    assert net.path == ["A", "B", "C"]
    assert len(net.events("route_computed")) == 1  # never moved
    dropped = net.events("route_candidate_dropped")
    assert [d.data["evaluations"] for d in dropped] == [1]


def test_smoothing_makes_a_long_dip_outlast_its_cause():
    # The reported RTT is already a median of five probes and the controller smooths it again
    # with a factor of 0.2, so a dip fades slowly: after two slow reports the smoothed values
    # are still above D's even once the links have recovered, and the switch happens.
    net = Net("lowest_delay")
    net.settle()
    net.set(A, B, 120, (20, 20))
    net.set(B, C, 120, (20, 20))
    net.tick()
    net.tick()
    net.set(A, B, 10, (20, 20))
    net.set(B, C, 10, (20, 20))
    for _ in range(4):
        net.tick()
    assert net.path == ["A", "D", "C"]


def test_a_broken_route_is_replaced_at_once_without_waiting_for_evaluations():
    net = Net("lowest_delay")
    net.settle()
    net.gw.advance(EVAL_MS * 2)
    before = len(net.events("route_computed"))
    net.down.add(B)  # B stops: its neighbors report it lost
    net.gw.advance(100)
    net.report_all()
    net.gw.advance(1)
    activate(net.gw)
    assert net.path == ["A", "D", "C"]
    switched = net.events("route_computed")[-1]
    assert len(net.events("route_computed")) == before + 1
    assert switched.data["trigger"] == "topology_changed"
    assert switched.time_ms < 2 * EVAL_MS * 2  # on the very next report, not after three ticks


def test_a_restart_of_a_node_on_the_route_is_treated_as_broken_and_reinstalled_at_once():
    net = Net("lowest_delay")
    net.settle()
    first_version = net.controller.version
    sessions = dict(SESSIONS)
    sessions[B] = 2222  # B rebooted: its table is gone
    net.report_all(sessions=sessions)
    net.gw.advance(1)
    assert net.controller.version > first_version
    assert net.events("route_computed")[-1].data["trigger"] == "node_restarted"


def test_a_restart_on_the_route_is_replaced_at_once_even_by_a_route_that_is_only_better():
    net = Net("lowest_delay")
    net.settle()
    net.set(A, B, 120, (20, 20))  # B's links dip: D (60) is now narrowly better than B (64)
    net.set(B, C, 120, (20, 20))
    net.tick()
    assert net.controller.route_state()["pending_switch"]["evaluations"] == 1
    sessions = dict(SESSIONS)
    sessions[B] = 2222  # and B reboots: its table is gone, the route through it is broken
    net.report_all(sessions=sessions)
    net.gw.advance(1)
    activate(net.gw)
    assert net.path == ["A", "D", "C"]  # at once, not after three evaluations
    assert net.events("route_computed")[-1].data["trigger"] == "topology_changed"


def test_a_returning_better_link_waits_for_the_evaluations_too():
    net = Net("lowest_delay")
    net.settle()
    net.down.add(B)
    net.report_all()
    net.gw.advance(1)
    activate(net.gw)
    assert net.path == ["A", "D", "C"]
    net.down.clear()
    changed_at = net.gw.now
    # B is back and cheaper; after the hold-down it has to win three evaluations in a row
    for _ in range(30):
        net.tick()
        if net.path == ["A", "B", "C"]:
            break
    assert net.path == ["A", "B", "C"]
    switched = net.events("route_computed")[-1]
    assert switched.data["trigger"] == "policy_better_route"
    assert switched.time_ms - changed_at >= 3 * EVAL_MS  # three evaluations, not "as soon as up"


def test_immediate_policy_still_switches_the_moment_a_better_link_returns():
    net = Net("immediate_rtt")
    net.settle()
    net.down.add(B)
    net.report_all()
    net.gw.advance(1)
    activate(net.gw)
    assert net.path == ["A", "D", "C"]
    net.down.clear()
    net.report_all()
    net.gw.advance(6_000)  # hold-down 5 s, then back at once
    net.report_all()
    activate(net.gw)
    assert net.path == ["A", "B", "C"]
    assert not net.events("route_candidate")  # no evaluation machinery in this mode


# ---- highest reliability ----


def test_highest_reliability_prefers_the_lossy_free_path_even_though_it_is_slower():
    net = Net("highest_reliability", ab=(10, (14, 20)), bc=(10, (15, 20)))
    net.settle()
    assert net.path == ["A", "D", "C"]
    computed = net.events("route_computed")[0].data
    assert computed["policy"] == "highest_reliability"
    assert computed["cost_ms"] is None  # the cost is -log p, not milliseconds
    assert computed["delivery_probability"] == 1.0
    graph = {g["link"]: g for g in computed["graph"]}
    assert graph["A-B"]["delivery_probability"] == pytest.approx(0.7, abs=0.001)
    assert graph["A-D"]["delivery_probability"] == 1.0


def test_with_equal_probabilities_nothing_is_better_so_the_route_never_moves():
    net = Net("highest_reliability")  # every link 20/20
    net.settle()
    assert net.path == ["A", "B", "C"]  # the documented tie-break, not a judgement
    for _ in range(12):
        net.tick()
    assert len(net.events("route_computed")) == 1
    assert not net.events("route_candidate")


def test_a_tie_is_not_better_so_a_route_in_use_is_not_swapped_for_an_equal_one():
    net = Net("highest_reliability", ab=(10, (12, 20)), bc=(10, (13, 20)))
    net.settle()
    assert net.path == ["A", "D", "C"]
    net.set(A, B, 10, (20, 20))  # B recovers: every link is now 20/20, so both paths cost 0
    net.set(B, C, 10, (20, 20))
    for _ in range(14):  # past the hold-down and many evaluations
        net.tick()
    assert net.path == ["A", "D", "C"]  # the tie-break prefers B, but equal is not better
    assert not net.events("route_candidate")
    assert len(net.events("route_computed")) == 1


def test_a_single_lost_probe_has_to_persist_to_move_the_route():
    net = Net("highest_reliability")
    net.settle()
    net.set(A, B, 10, (19, 20))  # one probe lost on A-B
    net.tick()
    net.tick()
    assert net.path == ["A", "B", "C"]
    assert net.controller.route_state()["pending_switch"]["evaluations"] == 2
    net.set(A, B, 10, (20, 20))  # the loss rolls out of the window
    net.tick()
    assert net.path == ["A", "B", "C"]
    assert net.controller.route_state()["pending_switch"] is None
    assert [e.data["reason"] for e in net.events("route_candidate_dropped")] == ["no_longer_better"]


def test_sustained_loss_moves_the_route_after_three_evaluations_and_it_is_reported():
    net = Net("highest_reliability")
    net.settle()
    net.set(A, B, 10, (12, 20))
    net.set(B, C, 10, (13, 20))
    for _ in range(3):
        net.tick()
    assert net.path == ["A", "D", "C"]
    assert net.events("route_computed")[-1].data["trigger"] == "policy_better_route"
    active = net.events("route_active")[-1].data
    assert active["policy"] == "highest_reliability" and active["delivery_probability"] == 1.0


def test_a_link_without_probe_statistics_is_not_routable_under_reliability():
    net = Net("highest_reliability", ab=(10, None), bc=(10, None))
    net.settle()
    assert net.path == ["A", "D", "C"]  # the only path whose links have been rated
    net2 = Net("highest_reliability", ab=(10, None), bc=(10, None), ad=(30, None))
    net2.settle()
    assert net2.controller.route_state()["state"] == "waiting"  # nothing rated: nothing chosen


# ---- changing the policy while running ----


def test_changing_policy_is_reported_and_waits_for_the_evaluations_like_any_better_route():
    net = Net("lowest_delay", ab=(10, (12, 20)), bc=(10, (13, 20)))
    net.settle()
    assert net.path == ["A", "B", "C"]
    assert net.controller.set_policy("highest_reliability") is True
    assert net.controller.set_policy("highest_reliability") is False
    changed = net.events("policy_changed")
    assert [(e.data["old"], e.data["new"]) for e in changed] == [
        ("lowest_delay", "highest_reliability")
    ]
    assert net.path == ["A", "B", "C"]  # the route in use is intact: no instant jump
    ticks = 0
    while net.path == ["A", "B", "C"] and ticks < 10:
        net.tick()
        ticks += 1
    assert net.path == ["A", "D", "C"]
    counts = [e.data["evaluations"] for e in net.events("route_candidate")]
    assert counts == [1, 2, 3]  # three evaluations in a row, then the switch
    state = net.controller.policy_state()
    assert state["current"] == "highest_reliability" and state["hysteresis_evaluations"] == 3


def test_an_unknown_policy_is_refused_and_changes_nothing():
    net = Net("lowest_delay")
    with pytest.raises(ValueError):
        net.controller.set_policy("fastest")
    assert net.controller.policy == "lowest_delay"


def test_switching_to_reliability_with_no_statistics_keeps_the_intact_route():
    net = Net("immediate_rtt", ab=(10, None), bc=(10, None), ad=(30, None))
    net.settle()
    assert net.path == ["A", "B", "C"]
    net.controller.set_policy("highest_reliability")
    net.report_all()
    net.gw.advance(EVAL_MS * 4)
    assert net.path == [
        "A",
        "B",
        "C",
    ]  # nothing to compare: it stays, it does not announce "no route"
    assert not net.events("no_available_route")


def test_the_controller_stopping_ends_the_evaluations():
    net = Net("lowest_delay")
    net.settle()
    net.controller.stop()
    before = len(net.controller.events)
    net.set(A, B, 500, (20, 20))
    net.set(B, C, 500, (20, 20))
    for _ in range(10):
        net.tick()
    assert len(net.controller.events) == before
