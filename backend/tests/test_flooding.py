"""Bounded management flooding: reports and (from Phase 3) delivery acknowledgements."""

from collections import Counter

import pytest

from iot_backend.protocol import (
    Frame,
    NeighborCode,
    ReportEntry,
    TopologyReportPayload,
    encode,
)
from iot_backend.simulator import LinkConfig, ScenarioConfig, Simulation
from iot_backend.simulator.dedup import FloodCache
from iot_backend.simulator.trace import (
    ACK_IGNORED,
    ACK_RECEIVED,
    FRAME_DROPPED,
    FRAME_FORWARDED,
    FRAME_RECEIVED,
    FRAME_SENT,
    READING_LOST,
)

MANAGEMENT = {"TOPOLOGY_REPORT", "DELIVERY_ACK"}


def make_sim(seed=1, links=None, **kwargs) -> Simulation:
    return Simulation(ScenarioConfig(seed=seed, links=links or {}, **kwargs))


def drops(sim, reason):
    return [e for e in sim.trace.of_type(FRAME_DROPPED) if e.info["reason"] == reason]


# ---- the cache itself ----


def test_flood_cache_remembers_and_forgets_oldest_first():
    cache = FloodCache(size=3)
    assert [cache.seen((1, 1, i)) for i in range(3)] == [False, False, False]
    assert cache.seen((1, 1, 0)) is True
    assert cache.seen((1, 1, 3)) is False  # evicts (1, 1, 0), the oldest
    assert len(cache) == 3 and cache.evictions == 1
    assert cache.seen((1, 1, 0)) is False  # forgotten, so it would be handled again


def test_flood_cache_never_exceeds_its_size():
    cache = FloodCache(size=5)
    for i in range(1_000):
        cache.seen((1, 1, i))
    assert len(cache) == 5 and cache.evictions == 995


# ---- acknowledgements travel by flooding ----


def test_ack_reaches_a_by_both_relays_and_is_counted_once():
    sim = make_sim(discovery=False)  # isolate the ACK flood
    sim.run(3_000)
    ack_forwards = [
        e for e in sim.trace.of_type(FRAME_FORWARDED) if e.info["msg"] == "DELIVERY_ACK"
    ]
    assert {e.node for e in ack_forwards} == {"B", "D"}  # both relays pass it on
    assert len(sim.trace.of_type(ACK_RECEIVED)) == 1
    assert [e.info["reason"] for e in sim.trace.of_type(FRAME_DROPPED) if e.node == "A"] == [
        "flood_duplicate"
    ]  # the second copy was dropped by A's flood cache, not processed as a stale ACK
    assert sim.trace.of_type(ACK_IGNORED) == []


def test_ack_still_reaches_a_when_the_relay_on_the_return_path_is_dead():
    flooded = make_sim(discovery=False, links={("B", "A"): LinkConfig(down=True)})
    flooded.run(21_000)
    assert flooded.summary().acknowledged == 10 and flooded.summary().lost == 0

    # With the Phase 2 behaviour (ACKs follow the routing tables) the same failure loses them.
    routed = make_sim(
        discovery=False, ack_delivery="routed", links={("B", "A"): LinkConfig(down=True)}
    )
    routed.run(21_000)
    assert routed.summary().acknowledged == 0 and routed.summary().lost > 0


def test_ack_flooding_does_not_use_the_routing_tables():
    sim = make_sim(discovery=False)
    sim.set_route("C", "A", None)  # C has no route back to A at all
    sim.run(3_000)
    assert len(sim.trace.of_type(ACK_RECEIVED)) == 1
    assert drops(sim, "no_route") == []


# ---- flooding rules ----


def test_each_node_forwards_a_management_frame_at_most_once():
    sim = make_sim()
    sim.run(40_000)
    forwards = Counter(
        (e.node, e.identity)
        for e in sim.trace.of_type(FRAME_FORWARDED)
        if e.info["msg"] in MANAGEMENT
    )
    assert forwards  # flooding really happened
    # In the diamond every node has two neighbors; one is the sender, so one forward at most.
    assert max(forwards.values()) == 1
    per_frame = Counter(identity for (_, identity) in forwards)
    assert max(per_frame.values()) <= 3  # at most the three non-origin nodes relay it


def test_flooding_follows_permitted_links_only():
    sim = make_sim()
    sim.run(20_000)
    to_c = [e for e in sim.trace.of_type(FRAME_SENT) if e.node == "A" and e.info["to"] == "C"]
    assert to_c == []  # A never sends to C, even for management frames
    from_a = [
        e for e in sim.trace.of_type(FRAME_RECEIVED) if e.node == "C" and e.info["sender"] == "A"
    ]
    assert from_a == []


def test_a_nodes_own_frame_coming_back_is_dropped():
    sim = make_sim(discovery=False)
    report = Frame(
        src=1,  # A's own report ...
        dst=3,
        session=sim_session(sim, "A"),
        seq=500,
        ttl=7,
        payload=TopologyReportPayload((ReportEntry(2, NeighborCode.TWO_WAY, 5, 0),)),
    )
    sim.inject_frame("A", encode(report), at_ms=1_000, from_name="B")  # ... arrives back at A
    sim.run(1_500)
    (dropped,) = drops(sim, "own_frame")
    assert dropped.node == "A"
    assert [e for e in sim.trace.of_type(FRAME_FORWARDED) if e.identity == report.identity] == []


def sim_session(sim, name):
    boots = [e for e in sim.trace.events if e.event == "node_booted" and e.node == name]
    return int(boots[-1].info["session"], 16)


@pytest.mark.parametrize("cache_size", [1, 2, 8, 64])
def test_even_a_tiny_cache_cannot_cause_a_storm(cache_size):
    baseline = make_sim(report_interval_ms=1_000)
    baseline.run(30_000)
    small = make_sim(flood_cache_size=cache_size, report_interval_ms=1_000)
    small.run(30_000)
    # TTL and the one-forward-per-arrival rule keep traffic the same order of magnitude.
    assert small.summary().frames_sent <= baseline.summary().frames_sent * 1.5
    assert small.summary().frames_sent < 2_000


def test_ttl_ends_a_flood_even_with_no_cache_help():
    sim = make_sim(ttl=2, discovery=True)
    sim.run(10_000)
    # With ttl 2 a report is forwarded once and then dropped; nothing circulates.
    assert sim.summary().drops.get("ttl_expired", 0) > 0
    assert sim.summary().frames_sent < 1_000


def test_lost_flooded_ack_is_still_retried_end_to_end():
    links = {
        ("B", "A"): LinkConfig(down=True),
        ("D", "A"): LinkConfig(down=True),
    }
    sim = make_sim(discovery=False, links=links)
    sim.run(8_000)
    assert sim.trace.of_type(ACK_RECEIVED) == []  # no way back at all
    assert sim.trace.of_type(READING_LOST)  # so A reports readings lost after 3 attempts
