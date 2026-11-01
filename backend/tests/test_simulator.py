"""Four-node simulator tests. They assert on the public trace and summary, never on config."""

import pytest

from iot_backend.protocol import DataPayload, Frame, encode
from iot_backend.simulator import LinkConfig, ScenarioConfig, Simulation, Trace, observed_paths
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.trace import (
    ACK_IGNORED,
    ACK_RECEIVED,
    ACK_TIMEOUT,
    DUPLICATE_RECEIVED,
    FRAME_DROPPED,
    FRAME_DROPPED_BY_LINK,
    FRAME_FORWARDED,
    FRAME_RECEIVED,
    FRAME_REJECTED,
    FRAME_SENT,
    NODE_BOOTED,
    READING_DELIVERED,
    READING_LOST,
)

TWENTY_ONE_S = 21_000  # readings at 2, 4, ..., 20 s; the last ACK is back by 20.1 s


# These tests isolate the Phase 2 data plane: no HELLOs or reports, and ACKs routed through the
# manual tables. Discovery and flooding have their own tests (test_discovery.py, ...).
PHASE2 = {"discovery": False, "ack_delivery": "routed"}


def make_sim(seed=1, links=None, **kwargs) -> Simulation:
    return Simulation(ScenarioConfig(seed=seed, links=links or {}, **{**PHASE2, **kwargs}))


def data_identities(sim):
    """Identities of the readings A created, in order (one entry per reading, not attempt)."""
    seen = []
    for e in sim.trace.of_type(FRAME_SENT):
        if e.info["msg"] == "DATA" and e.identity not in seen:
            seen.append(e.identity)
    return seen


def delivered(sim, node="C"):
    return [e.identity for e in sim.trace.of_type(READING_DELIVERED) if e.node == node]


def received(sim, node, msg):
    return [e for e in sim.trace.of_type(FRAME_RECEIVED) if e.node == node and e.info["msg"] == msg]


def paths(sim, identity):
    return observed_paths(sim.trace.events, identity)


# ---- normal delivery and forwarding ----


def test_normal_delivery_follows_a_b_c_and_ack_returns_c_b_a():
    sim = make_sim()
    sim.run(TWENTY_ONE_S)
    readings = data_identities(sim)
    assert len(readings) == 10
    for identity in readings:
        assert paths(sim, identity) == [["A", "B", "C"]]
    assert delivered(sim) == readings  # each exactly once, in order
    assert [e.identity for e in sim.trace.of_type(ACK_RECEIVED)] == readings

    acks = [e for e in sim.trace.of_type(FRAME_SENT) if e.info["msg"] == "DELIVERY_ACK"]
    assert {e.node for e in acks} == {"C"}  # only the destination acknowledges; relays never do
    for ack in acks:
        assert paths(sim, ack.identity) == [["C", "B", "A"]]

    s = sim.summary()
    assert (s.readings_created, s.attempts, s.delivered_unique) == (10, 10, 10)
    assert (s.duplicates, s.acknowledged, s.lost, s.unresolved) == (0, 10, 0, 0)
    assert s.forwarded == 20  # B relays 10 DATA and 10 ACK frames
    assert s.drops == {}


def test_relay_decrements_ttl_and_destination_sees_fewer_hops_left():
    sim = make_sim(ttl=8)
    sim.run(3_000)
    assert received(sim, "B", "DATA")[0].info["ttl"] == 8
    assert received(sim, "C", "DATA")[0].info["ttl"] == 7
    (forwarded,) = [e for e in sim.trace.of_type(FRAME_FORWARDED) if e.info["msg"] == "DATA"]
    assert (forwarded.node, forwarded.info["sender"], forwarded.info["to"]) == ("B", "A", "C")


def test_manual_route_change_moves_traffic_to_d():
    sim = make_sim()
    sim.set_route("A", "C", "D", at_ms=9_000)
    sim.set_route("C", "A", "D", at_ms=9_000)
    sim.run(TWENTY_ONE_S)
    readings = data_identities(sim)
    before = [r for r in readings if paths(sim, r) == [["A", "B", "C"]]]
    after = [r for r in readings if paths(sim, r) == [["A", "D", "C"]]]
    assert len(before) == 4 and len(after) == 6
    assert before + after == readings
    assert sim.summary().lost == 0
    # the ACK of a later reading also came back through D
    late_ack = [e for e in sim.trace.of_type(FRAME_SENT) if e.info["msg"] == "DELIVERY_ACK"][-1]
    assert paths(sim, late_ack.identity) == [["C", "D", "A"]]


def test_missing_route_drops_with_no_route_and_counts_it():
    sim = make_sim()
    sim.set_route("B", "C", None)  # B no longer knows how to reach C
    sim.run(TWENTY_ONE_S)
    s = sim.summary()
    arrived_at_b = len(received(sim, "B", "DATA"))
    assert arrived_at_b > 0
    assert s.drops["no_route"] == arrived_at_b
    assert s.delivered_unique == 0


# ---- physical reachability vs logical topology ----


def test_a_misconfigured_to_send_directly_to_c_is_rejected_by_c():
    config = ScenarioConfig(seed=1, **PHASE2)
    config.permitted["A"] = {"B", "C", "D"}  # A believes C is a neighbor ...
    config.routes["A"] = {"C": "C"}  # ... and routes straight to it
    sim = Simulation(config)
    sim.run(10_000)

    arrivals = [e for e in sim.trace.of_type(FRAME_RECEIVED) if e.node == "C"]
    assert arrivals and all(e.info["sender"] == "A" for e in arrivals)  # the radio did reach C
    rejected = [e for e in sim.trace.of_type(FRAME_REJECTED) if e.node == "C"]
    assert len(rejected) == len(arrivals)
    assert {e.info["reason"] for e in rejected} == {"not_permitted_neighbor"}
    assert delivered(sim) == []  # nothing was accepted
    s = sim.summary()
    assert s.delivered_unique == 0 and s.acknowledged == 0
    assert s.drops["not_permitted_neighbor"] == len(arrivals)


def test_a_cannot_send_to_a_non_permitted_neighbor_even_if_routed():
    config = ScenarioConfig(seed=1, **PHASE2)
    config.routes["A"] = {"C": "C"}  # route says C, but A's permitted list does not include C
    sim = Simulation(config)
    sim.run(5_000)
    assert sim.trace.of_type(FRAME_SENT) == []  # nothing ever left A
    dropped = sim.trace.of_type(FRAME_DROPPED)
    assert {e.info["reason"] for e in dropped} == {"not_permitted_neighbor"}
    assert sim.summary().frames_sent == 0


def test_frame_from_a_non_permitted_neighbor_is_rejected_by_a_relay():
    sim = make_sim()
    frame = Frame(src=4, dst=3, session=9, seq=1, ttl=8, payload=DataPayload(5))
    sim.inject_frame("B", encode(frame), at_ms=1_000, from_name="D")  # D is not B's neighbor
    sim.run(1_500)
    (event,) = [e for e in sim.trace.of_type(FRAME_REJECTED) if e.node == "B"]
    assert event.info["reason"] == "not_permitted_neighbor"
    assert sim.trace.of_type(FRAME_FORWARDED) == []


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_nothing_reaches_c_without_passing_through_b_or_d(seed):
    link = LinkConfig(loss=0.2)
    keys = [("A", "B"), ("B", "C"), ("C", "B"), ("A", "D"), ("D", "C"), ("C", "D")]
    sim = make_sim(seed=seed, links={k: link for k in keys})
    sim.set_route("A", "C", "D", at_ms=30_000)
    sim.set_route("C", "A", "D", at_ms=30_000)
    sim.run(60_000)
    unique = set(delivered(sim))
    assert unique
    for identity in unique:
        reaching_c = [p for p in paths(sim, identity) if p[-1] == "C"]
        assert reaching_c
        for path in reaching_c:
            assert path in (["A", "B", "C"], ["A", "D", "C"])
    # and no frame was ever received at C straight from A
    assert all(e.info["sender"] != "A" for e in sim.trace.of_type(FRAME_RECEIVED) if e.node == "C")


# ---- reliability: retries and duplicates ----


def test_lost_ack_makes_a_retry_with_same_identity_and_c_counts_once_and_reacks():
    sim = make_sim(links={("B", "A"): LinkConfig(drop_frames=frozenset({2}))})
    sim.run(TWENTY_ONE_S)
    readings = data_identities(sim)
    second = readings[1]

    attempts = [e for e in sim.trace.of_type(FRAME_SENT) if e.identity == second]
    assert [e.info["attempt"] for e in attempts] == [1, 2]  # same identity, two transmissions
    assert len(sim.trace.of_type(ACK_TIMEOUT)) == 1

    assert delivered(sim).count(second) == 1  # counted once
    assert [e.identity for e in sim.trace.of_type(DUPLICATE_RECEIVED)] == [second]
    c_acks = [
        e
        for e in sim.trace.of_type(FRAME_SENT)
        if e.node == "C" and e.info["msg"] == "DELIVERY_ACK"
    ]
    assert len(c_acks) == len(readings) + 1  # one ACK per reading plus one for the duplicate

    (ack,) = [e for e in sim.trace.of_type(ACK_RECEIVED) if e.identity == second]
    assert ack.info["attempts"] == 2
    s = sim.summary()
    assert (s.readings_created, s.attempts, s.delivered_unique, s.duplicates) == (10, 11, 10, 1)
    assert (s.acknowledged, s.lost) == (10, 0)


def test_data_lost_on_every_attempt_is_marked_lost_and_not_retried_forever():
    sim = make_sim()
    sim.set_link_down("A", "B", True)
    sim.set_link_down("A", "B", False, at_ms=5_500)
    sim.run(10_000)
    first, second = data_identities(sim)[:2]
    attempts = [e for e in sim.trace.of_type(FRAME_SENT) if e.identity == first]
    assert len(attempts) == 3  # max_attempts, then no more
    assert [e.identity for e in sim.trace.of_type(READING_LOST)][0] == first
    assert paths(sim, first) == [["A"], ["A"], ["A"]]  # never got past the first link
    # the next reading's third attempt got through after the link recovered
    (ack,) = [e for e in sim.trace.of_type(ACK_RECEIVED) if e.identity == second]
    assert ack.info["attempts"] == 3
    assert {e.info["reason"] for e in sim.trace.of_type(FRAME_DROPPED_BY_LINK)} == {"link_down"}


def test_late_ack_after_giving_up_is_ignored_not_double_counted():
    sim = make_sim(links={("B", "A"): LinkConfig(delay_ms=4_000)})  # ACKs arrive too late
    sim.run(10_000)
    first = data_identities(sim)[0]
    assert first in [e.identity for e in sim.trace.of_type(READING_LOST)]
    assert first in [e.identity for e in sim.trace.of_type(ACK_IGNORED)]
    s = sim.summary()
    assert s.acknowledged == 0 and s.lost >= 1
    assert s.delivered_unique > s.acknowledged  # delivered, yet reported lost: only ACKs failed


# ---- loops and TTL ----


def test_routing_loop_ends_by_ttl_and_run_terminates():
    config = ScenarioConfig(seed=1, **PHASE2)
    config.routes["B"] = {"C": "A"}  # B hands frames for C back to A
    sim = Simulation(config)
    sim.run(10_000)
    s = sim.summary()
    assert s.drops["ttl_expired"] > 0
    assert s.delivered_unique == 0
    assert s.frames_sent < 300  # bounded: no infinite circulation
    first = data_identities(sim)[0]
    for path in paths(sim, first):
        assert path == ["A", "B", "A", "B", "A", "B", "A", "B", "A"]  # 8 transmissions, ttl 8


def test_ttl_rule_frame_arriving_with_ttl_1_is_not_forwarded():
    config = ScenarioConfig(seed=1, ttl=3, max_attempts=1, **PHASE2)
    config.routes["B"] = {"C": "A"}
    sim = Simulation(config)
    sim.run(2_500)
    first = data_identities(sim)[0]
    # A sends ttl 3, B forwards ttl 2, A forwards ttl 1, B receives ttl 1 and drops it
    assert paths(sim, first) == [["A", "B", "A", "B"]]
    (drop,) = [e for e in sim.trace.of_type(FRAME_DROPPED) if e.info["reason"] == "ttl_expired"]
    assert (drop.node, drop.info["ttl"]) == ("B", 1)


# ---- failures: nodes and links ----


def test_b_stopped_without_route_change_loses_readings_then_recovers_on_restart():
    sim = make_sim()
    sim.stop_node("B", at_ms=7_000)
    sim.start_node("B", at_ms=15_000)
    sim.run(26_000)
    readings = data_identities(sim)
    lost = {e.identity for e in sim.trace.of_type(READING_LOST)}
    assert lost  # honest failure: nothing heals yet
    # D never carried anything, because nothing changed the routes
    assert not [e for e in sim.trace.of_type(FRAME_RECEIVED) if e.node == "D"]
    assert {e.node for e in sim.trace.of_type(FRAME_DROPPED)} == {"B"}
    # after B returns, new readings are delivered and acknowledged again
    last = readings[-3]
    assert last not in lost
    assert paths(sim, last) == [["A", "B", "C"]]
    assert last in [e.identity for e in sim.trace.of_type(ACK_RECEIVED)]
    assert sim.summary().drops["node_stopped"] > 0
    boots = [e for e in sim.trace.of_type(NODE_BOOTED) if e.node == "B"]
    assert len(boots) == 2 and boots[0].info["session"] != boots[1].info["session"]


def test_restarted_a_gets_new_session_and_c_does_not_confuse_it_with_the_old_one():
    sim = make_sim()
    # restart right after reading #1 is sent; its ACK is still in flight (arrives at 2.1 s)
    sim.restart_node("A", at_ms=2_010)
    sim.run(9_000)

    boots = [e for e in sim.trace.of_type(NODE_BOOTED) if e.node == "A"]
    assert len(boots) == 2 and boots[0].info["session"] != boots[1].info["session"]

    readings = data_identities(sim)
    old_session = readings[0].session
    new = [r for r in readings if r.session != old_session]
    assert readings[0].seq == 1 and new[0].seq == 1  # numbering restarted
    assert new[0] != readings[0]  # same seq, different identity

    # the in-flight ACK from the old session was ignored by the restarted A
    assert [e.identity for e in sim.trace.of_type(ACK_IGNORED)] == [readings[0]]
    # C counted both sessions' "reading #1" as separate unique readings, no duplicates
    assert delivered(sim) == readings
    assert sim.summary().duplicates == 0
    assert [e.identity for e in sim.trace.of_type(ACK_RECEIVED)] == new


def test_one_direction_down_shows_which_direction_failed():
    # B->A carries only the ACKs on their last hop. Take it down; A->B stays up.
    sim = make_sim()
    sim.set_link_down("B", "A", True, at_ms=5_000)
    sim.run(TWENTY_ONE_S)
    link_drops = sim.trace.of_type(FRAME_DROPPED_BY_LINK)
    assert link_drops
    assert {e.info["link"] for e in link_drops} == {"B->A"}  # only that direction failed
    assert {e.info["reason"] for e in link_drops} == {"link_down"}
    # the data direction kept working: C accepted every reading, but A heard no ACK after 5 s
    s = sim.summary()
    assert s.delivered_unique == s.readings_created
    assert s.acknowledged == 2
    assert s.duplicates > 0  # retries of readings C already had
    assert s.lost > 0  # A cannot know they arrived
    assert s.drops["link_down"] == len(link_drops)


def test_data_direction_down_means_c_receives_nothing_new():
    sim = make_sim()
    sim.set_link_down("A", "B", True, at_ms=5_000)
    sim.run(TWENTY_ONE_S)
    assert {e.info["link"] for e in sim.trace.of_type(FRAME_DROPPED_BY_LINK)} == {"A->B"}
    assert len(delivered(sim)) == 2  # only the readings sent before 5 s


def test_delivery_time_counts_from_the_first_attempt_not_the_last():
    sim = make_sim(links={("B", "A"): LinkConfig(drop_frames=frozenset({1}))})
    sim.run(5_000)
    (first,) = [e for e in sim.trace.of_type(ACK_RECEIVED) if e.info["attempts"] == 2]
    assert first.info["delivery_time_ms"] >= 1_000  # waited a full ACK timeout before retrying
    assert "rtt_ms" not in first.info


def test_link_delay_can_change_at_runtime():
    sim = make_sim()
    sim.configure_link("B", "C", at_ms=5_000, delay_ms=300)
    sim.run(8_000)
    times = [e.info["delivery_time_ms"] for e in sim.trace.of_type(ACK_RECEIVED)]
    assert times[0] == times[1] == 100  # readings at 2 s and 4 s (single attempt each)
    assert times[2] == 100 + 280  # reading at 6 s: B->C is 280 ms slower


# ---- queues ----


def test_queue_overflow_is_bounded_and_counted():
    sim = make_sim(reading_interval_ms=10, queue_capacity=3, service_ms=100, max_attempts=1)
    sim.run(2_000)
    s = sim.summary()
    assert s.drops["queue_full"] > 0
    # each radio serves one frame per service time, so output is bounded whatever the input
    assert s.frames_sent <= 2_000 // 100 * 4
    assert s.readings_created > 100  # lots of input, little output


# ---- determinism ----


def lossy(seed):
    link = LinkConfig(loss=0.3)
    keys = [("A", "B"), ("B", "C"), ("C", "B"), ("B", "A")]
    sim = make_sim(seed=seed, links={k: link for k in keys})
    sim.run(81_000)
    return sim


def test_same_seed_gives_identical_trace_and_different_seed_differs():
    first, second = lossy(7), lossy(7)
    assert first.trace.events == second.trace.events
    assert first.summary() == second.summary()

    def loss_pattern(sim):
        return [
            (e.time_ms, e.info["link"], e.info["ordinal"])
            for e in sim.trace.of_type(FRAME_DROPPED_BY_LINK)
        ]

    assert loss_pattern(first)
    assert loss_pattern(lossy(8)) != loss_pattern(first)


# ---- wraparound ----


def test_duplicate_detection_survives_sequence_wraparound_in_simulation():
    config = ScenarioConfig(
        seed=1,
        **PHASE2,
        initial_seq={"A": 2**32 - 2},
        links={("B", "A"): LinkConfig(drop_frames=frozenset({3}))},  # ACK for the 3rd reading
    )
    sim = Simulation(config)
    sim.run(11_000)
    readings = data_identities(sim)
    assert [r.seq for r in readings] == [2**32 - 2, 2**32 - 1, 0, 1, 2]
    assert delivered(sim) == readings  # each once, even across the wrap
    assert [e.identity for e in sim.trace.of_type(DUPLICATE_RECEIVED)] == [readings[2]]
    s = sim.summary()
    assert (s.delivered_unique, s.duplicates, s.acknowledged, s.lost) == (5, 1, 5, 0)


# ---- malformed input, summary, engine ----


def test_malformed_frames_are_rejected_recorded_and_do_not_stop_the_simulation():
    sim = make_sim()
    bad_version = bytearray(encode(Frame(1, 3, 5, 1, 8, DataPayload(1))))
    bad_version[0] = 9
    sim.inject_frame("B", b"\x01\x02\x03", at_ms=3_000, from_name="A")
    sim.inject_frame("B", bytes(bad_version), at_ms=3_500, from_name="A")
    sim.inject_frame("A", b"", at_ms=3_600, from_name="B")
    sim.run(11_000)
    rejected = [(e.node, e.info["reason"]) for e in sim.trace.of_type(FRAME_REJECTED)]
    assert rejected == [("B", "truncated"), ("B", "bad_version"), ("A", "truncated")]
    s = sim.summary()
    assert s.readings_created == 5 and s.acknowledged == 5
    assert s.drops["truncated"] == 2 and s.drops["bad_version"] == 1


def test_summary_counts_survive_a_trace_that_overflowed():
    sim = make_sim(trace_max_events=5)
    sim.run(TWENTY_ONE_S)
    assert sim.trace.dropped > 0
    s = sim.summary()
    assert (s.readings_created, s.attempts, s.acknowledged) == (10, 10, 10)


def test_stopped_node_ignores_frames_and_forgets_pending_readings():
    sim = make_sim()
    sim.stop_node("C", at_ms=3_000)
    sim.run(9_000)
    dropped = sim.trace.of_type(FRAME_DROPPED)
    assert {e.node for e in dropped} == {"C"}
    assert {e.info["reason"] for e in dropped} == {"node_stopped"}
    assert sim.summary().acknowledged == 1  # only the reading before C stopped


def test_unknown_names_in_config_are_refused():
    with pytest.raises(ValueError):
        Simulation(ScenarioConfig(links={("A", "Z"): LinkConfig()}, **PHASE2))
    with pytest.raises(ValueError):
        Simulation(ScenarioConfig(routes={"A": {"C": "Q"}}, **PHASE2))


def test_link_config_validation():
    with pytest.raises(ValueError):
        LinkConfig(loss=1.5)
    with pytest.raises(ValueError):
        LinkConfig(delay_ms=-1)
    with pytest.raises(ValueError):
        LinkConfig(drop_frames=frozenset({0}))


def test_engine_runs_in_time_order_without_real_waiting():
    engine = Engine()
    order = []
    engine.schedule(500, lambda: order.append("late"))
    engine.schedule(100, lambda: order.append("early"))
    engine.schedule(100, lambda: order.append("early-second"))
    engine.run_until(3_600_000)  # one simulated hour completes instantly
    assert order == ["early", "early-second", "late"]
    assert engine.now_ms == 3_600_000


def test_engine_refuses_the_past():
    with pytest.raises(ValueError):
        Engine().schedule(-1, lambda: None)


def test_trace_is_bounded_and_counts_what_it_dropped():
    trace = Trace(max_events=3)
    for i in range(5):
        trace.record(i, "A", "x")
    assert [e.time_ms for e in trace.events] == [2, 3, 4]
    assert trace.dropped == 2
