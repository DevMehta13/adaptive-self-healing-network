"""HELLO discovery, neighbor tables and loss-of-contact timeouts (Phase 3)."""

import pytest

from iot_backend.protocol import (
    Frame,
    HelloPayload,
    encode,
)
from iot_backend.simulator import LinkConfig, ScenarioConfig, Simulation
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.nodes import Node, NodeSettings
from iot_backend.simulator.trace import (
    ACK_RECEIVED,
    DUPLICATE_RECEIVED,
    FRAME_FORWARDED,
    FRAME_RECEIVED,
    FRAME_REJECTED,
    FRAME_SENT,
    NEIGHBOR_CHANGED,
    NEIGHBOR_RESTARTED,
    READING_DELIVERED,
    Trace,
)


def make_sim(seed=1, links=None, **kwargs) -> Simulation:
    return Simulation(ScenarioConfig(seed=seed, links=links or {}, **kwargs))


def changes(sim, node=None, neighbor=None, new=None):
    events = sim.trace.of_type(NEIGHBOR_CHANGED)
    return [
        e
        for e in events
        if (node is None or e.node == node)
        and (neighbor is None or e.info["neighbor"] == neighbor)
        and (new is None or e.info["new"] == new)
    ]


def hellos_sent(sim, node):
    return [e for e in sim.trace.of_type(FRAME_SENT) if e.node == node and e.info["msg"] == "HELLO"]


def test_every_permitted_neighbor_pair_becomes_two_way():
    sim = make_sim()
    sim.run(5_000)
    pairs = {(e.node, e.info["neighbor"]) for e in changes(sim, new="two_way")}
    # the diamond: each of the four links, in both directions
    assert pairs == {
        ("A", "B"),
        ("B", "A"),
        ("B", "C"),
        ("C", "B"),
        ("A", "D"),
        ("D", "A"),
        ("D", "C"),
        ("C", "D"),
    }


def test_hearing_a_hello_proves_only_one_direction():
    # A->B is down from the start: B never hears A, but A hears B.
    sim = make_sim(links={("A", "B"): LinkConfig(down=True)})
    sim.run(8_000)
    # A hears B's HELLOs, but B's HELLOs never list A, so A can only say "one_way".
    assert {e.info["new"] for e in changes(sim, node="A", neighbor="B")} == {"one_way"}
    # B has heard nothing from A at all: no entry, so nothing to confirm.
    assert changes(sim, node="B", neighbor="A") == []


def test_hello_is_local_and_only_goes_to_permitted_neighbors():
    sim = make_sim()
    sim.run(6_000)
    assert [e for e in sim.trace.of_type(FRAME_FORWARDED) if e.info["msg"] == "HELLO"] == []
    for node, allowed in {
        "A": {"B", "D"},
        "B": {"A", "C"},
        "C": {"B", "D"},
        "D": {"A", "C"},
    }.items():
        sent = hellos_sent(sim, node)
        assert sent and {e.info["to"] for e in sent} == allowed
    for e in sim.trace.of_type(FRAME_RECEIVED):
        if e.info.get("msg") == "HELLO":
            assert e.info["sender"] == {1: "A", 2: "B", 3: "C", 4: "D"}[e.identity.src]


def test_contact_is_lost_exactly_one_timeout_after_the_last_hello():
    sim = make_sim()
    sim.stop_node("B", at_ms=8_000)
    sim.run(14_000)
    for observer in ("A", "C"):
        last_hello = max(
            e.time_ms
            for e in sim.trace.of_type(FRAME_RECEIVED)
            if e.node == observer and e.info.get("msg") == "HELLO" and e.info["sender"] == "B"
        )
        (lost,) = changes(sim, node=observer, neighbor="B", new="lost")
        assert lost.time_ms == last_hello + 3_000
        assert lost.info["silent_ms"] == 3_000
        assert last_hello <= 8_000  # nothing heard after B stopped


def test_timeout_and_interval_are_configurable():
    sim = make_sim(hello_interval_ms=500, neighbor_timeout_ms=1_800)
    sim.stop_node("B", at_ms=5_000)
    sim.run(9_000)
    (lost,) = changes(sim, node="A", neighbor="B", new="lost")
    assert lost.info["silent_ms"] == 1_800


def test_a_restarted_neighbor_is_recognised_by_its_new_session():
    sim = make_sim()
    sim.stop_node("B", at_ms=6_000)
    sim.start_node("B", at_ms=9_000)
    sim.run(15_000)
    restarted = [e for e in sim.trace.of_type(NEIGHBOR_RESTARTED) if e.node in ("A", "C")]
    assert {e.node for e in restarted} == {"A", "C"}
    for e in restarted:
        assert e.info["neighbor"] == "B" and e.info["old_session"] != e.info["new_session"]
    # and the links come back to two-way
    for observer in ("A", "C"):
        assert changes(sim, node=observer, neighbor="B")[-1].info["new"] == "two_way"


def test_hello_interval_has_seeded_jitter_and_nodes_do_not_start_in_step():
    sim = make_sim()
    sim.run(30_000)
    gaps = []
    for node in "ABCD":
        times = [e.time_ms for e in hellos_sent(sim, node) if e.info["to"] in ("B", "C")]
        times = sorted(set(times))
        gaps += [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(850 <= g <= 1_150 for g in gaps)  # 1000 ms +/- 100 ms jitter (+ queueing)
    assert len(set(gaps)) > 5  # not a fixed period
    first = {n: min(e.time_ms for e in hellos_sent(sim, n)) for n in "ABCD"}
    assert len(set(first.values())) > 1  # booted together, but not transmitting together


def test_hello_from_a_non_permitted_neighbor_is_rejected_and_not_recorded():
    sim = make_sim()
    hello = Frame(src=4, dst=2, session=77, seq=1, ttl=1, payload=HelloPayload(()))
    sim.inject_frame("B", encode(hello), at_ms=500, from_name="D")  # D is not B's neighbor
    sim.run(1_000)
    rejected = [e for e in sim.trace.of_type(FRAME_REJECTED) if e.node == "B"]
    assert [e.info["reason"] for e in rejected] == ["not_permitted_neighbor"]
    assert changes(sim, node="B", neighbor="D") == []


def test_hello_that_is_not_addressed_to_the_receiver_is_rejected():
    sim = make_sim()
    hello = Frame(src=1, dst=3, session=77, seq=1, ttl=1, payload=HelloPayload(()))  # A -> C
    sim.inject_frame("B", encode(hello), at_ms=500, from_name="A")  # arrives at B
    sim.run(1_000)
    reasons = [e.info["reason"] for e in sim.trace.of_type(FRAME_REJECTED) if e.node == "B"]
    assert reasons == ["hello_not_local"]


def test_neighbor_table_size_is_bounded_by_the_protocol_limit():
    engine, trace = Engine(), Trace()
    with pytest.raises(ValueError):
        Node(
            1,
            engine,
            trace,
            lambda *a: None,
            0,
            permitted=set(range(2, 12)),  # 10 neighbors; a HELLO can list at most 8
            routes={},
            settings=NodeSettings(),
        )


def test_discovery_can_be_switched_off():
    sim = make_sim(discovery=False)
    sim.run(5_000)
    assert sim.summary().hellos == 0 and sim.summary().reports == 0
    assert changes(sim) == []


def test_same_seed_gives_identical_discovery_trace():
    first, second = make_sim(seed=9), make_sim(seed=9)
    for sim in (first, second):
        sim.stop_node("B", at_ms=7_000)
        sim.run(15_000)
    assert first.trace.events == second.trace.events
    other = make_sim(seed=10)
    other.stop_node("B", at_ms=7_000)
    other.run(15_000)
    assert [e.time_ms for e in hellos_sent(other, "A")] != [
        e.time_ms for e in hellos_sent(first, "A")
    ]


def test_management_frames_share_the_sequence_counter_and_dedup_still_works():
    # Drop the ACK of reading #2 on both flooded return paths, so A must retry it.
    ack_only = {"delivery_ack": "DELIVERY_ACK"}
    links = {
        ("B", "A"): LinkConfig(drop_frames=frozenset({2}), drop_msg=ack_only["delivery_ack"]),
        ("D", "A"): LinkConfig(drop_frames=frozenset({2}), drop_msg=ack_only["delivery_ack"]),
    }
    sim = make_sim(links=links)
    sim.run(21_000)

    data_frames = [
        e for e in sim.trace.of_type(FRAME_SENT) if e.info["msg"] == "DATA" and e.node == "A"
    ]
    seqs = sorted({e.identity.seq for e in data_frames})
    # HELLOs and reports use the same counter, so reading numbers have gaps
    assert all(b - a > 1 for a, b in zip(seqs, seqs[1:], strict=False))

    delivered = [e.identity for e in sim.trace.of_type(READING_DELIVERED) if e.node == "C"]
    assert len(delivered) == len(set(delivered))  # each counted once
    assert len(sim.trace.of_type(DUPLICATE_RECEIVED)) == 1  # the retried reading #2
    s = sim.summary()
    assert s.delivered_unique == s.acknowledged == s.readings_created == 10
    assert (s.lost, s.unresolved) == (0, 0)
    assert s.attempts == 11  # exactly one retry
    assert sim.trace.of_type(ACK_RECEIVED)
