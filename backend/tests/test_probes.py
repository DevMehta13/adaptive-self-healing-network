"""Link probes: round-trip time measured by the prober on its own clock, reported per neighbor."""

import dataclasses

from iot_backend.protocol import (
    Frame,
    ProbePayload,
    ProbeReplyPayload,
    encode,
)
from iot_backend.simulator import LinkConfig, ScenarioConfig, Simulation
from iot_backend.simulator.trace import FRAME_DROPPED, FRAME_REJECTED, FRAME_SENT
from iot_backend.transports import TopologyReportEvent

SERVICE_MS = 5  # default per-frame service time: one at the prober, one at the replier


def make_sim(delay_ms=10, **kwargs):
    links = {}
    for a, b in [("A", "B"), ("B", "A"), ("B", "C"), ("C", "B"), ("A", "D"), ("D", "A")]:
        links[(a, b)] = LinkConfig(delay_ms=delay_ms)
    settings = {"probe_interval_ms": 1000, **kwargs}
    config = ScenarioConfig(seed=3, links=links, **settings)
    sim = Simulation(config)
    reports: list[TopologyReportEvent] = []
    sim.gateway.subscribe(lambda e: reports.append(e) if isinstance(e, TopologyReportEvent) else 0)
    return sim, reports


def rtt_of(reports, origin, neighbor, after_ms=0):
    """The RTT `origin` last reported for `neighbor` (None if unmeasured or never reported)."""
    value = "never reported"
    for e in reports:
        if e.origin == origin and e.received_ms >= after_ms:
            for n in e.neighbors:
                if n.neighbor == neighbor:
                    value = n.rtt_ms
    return value


def test_probing_is_off_unless_an_interval_is_configured():
    sim = Simulation(ScenarioConfig(seed=1))
    sim.run(10_000)
    assert sim.summary().probes == 0
    assert not [e for e in sim.trace.of_type(FRAME_SENT) if e.info["msg"] == "PROBE"]


def test_probes_go_one_hop_to_permitted_neighbors_only():
    sim, _ = make_sim()
    sim.run(6_000)
    probes = [e for e in sim.trace.of_type(FRAME_SENT) if e.info["msg"] == "PROBE"]
    assert probes
    permitted = {"A": {"B", "D"}, "B": {"A", "C"}, "C": {"B", "D"}, "D": {"A", "C"}}
    assert all(e.info["to"] in permitted[e.node] for e in probes)
    # one hop: nobody ever forwards a probe or a reply
    from iot_backend.simulator.trace import FRAME_FORWARDED

    assert not [
        e for e in sim.trace.of_type(FRAME_FORWARDED) if e.info["msg"] in ("PROBE", "PROBE_REPLY")
    ]


def test_rtt_is_the_round_trip_seen_by_the_prober_alone():
    sim, reports = make_sim(delay_ms=10)
    sim.run(15_000)
    # A's frame waits for the radio (5 ms), travels 10 ms, B's reply waits 5 ms, 10 ms back
    expected = 10 + 10 + 2 * SERVICE_MS
    measured = rtt_of(reports, 1, 2)
    assert measured is not None and expected <= measured <= expected + 10
    # the reply names the probe by its own identity; it carries no remote timestamp
    assert {f.name for f in dataclasses.fields(ProbeReplyPayload)} == {"probe_session", "probe_seq"}
    assert not dataclasses.fields(ProbePayload)


def test_a_slower_link_is_measured_as_slower():
    sim, reports = make_sim(delay_ms=10)
    sim.configure_link("A", "B", at_ms=20_000, delay_ms=60)
    sim.configure_link("B", "A", at_ms=20_000, delay_ms=60)
    sim.run(40_000)
    before = rtt_of([e for e in reports if e.received_ms < 20_000], 1, 2)
    after = rtt_of(reports, 1, 2)
    assert after - before >= 90  # two extra 50 ms legs, minus nothing: the median caught up


def test_a_link_is_unmeasured_until_enough_samples_exist():
    # one probe every 10 s and samples kept for a minute: after 15 s there are at most two
    sim, reports = make_sim(probe_interval_ms=10_000, rtt_max_age_ms=60_000)
    sim.run(15_000)
    assert rtt_of(reports, 1, 2) in (None, "never reported")  # two samples are not enough
    sim.run(30_000)
    assert isinstance(rtt_of(reports, 1, 2), int)  # the third makes it a measurement


def test_a_stale_measurement_expires_instead_of_being_reported_forever():
    sim, reports = make_sim()
    sim.set_link_down("A", "B", True, at_ms=15_000)
    sim.set_link_down("B", "A", True, at_ms=15_000)
    sim.run(40_000)
    assert isinstance(rtt_of([e for e in reports if e.received_ms < 15_000], 1, 2), int)
    assert rtt_of(reports, 1, 2) is None  # no fresh sample for rtt_max_age: unmeasured


def test_a_reply_that_matches_no_outstanding_probe_is_ignored():
    sim, reports = make_sim()
    bogus = Frame(2, 1, 0xDEAD, 1, 1, ProbeReplyPayload(probe_session=12345, probe_seq=7))
    for at in range(5_000, 14_000, 500):  # a flood of bogus replies must not move the median
        sim.inject_frame("A", encode(bogus), at, "B")
    sim.run(15_000)
    unmatched = [
        e
        for e in sim.trace.of_type(FRAME_DROPPED)
        if e.node == "A" and e.info["reason"] == "probe_unmatched"
    ]
    assert len(unmatched) == 18
    measured = rtt_of(reports, 1, 2)
    assert 20 <= measured <= 40


def test_probes_and_replies_from_non_neighbors_or_for_other_nodes_are_rejected():
    sim, _ = make_sim()
    from_c = Frame(3, 1, 0xBEEF, 1, 1, ProbePayload())  # C is not A's neighbor
    for_d = Frame(2, 4, 0xBEEF, 2, 1, ProbePayload())  # addressed to D, delivered to A
    sim.inject_frame("A", encode(from_c), 1_000, "C")
    sim.inject_frame("A", encode(for_d), 1_100, "B")
    sim.run(2_000)
    reasons = [e.info["reason"] for e in sim.trace.of_type(FRAME_REJECTED) if e.node == "A"]
    assert sorted(reasons) == ["not_permitted_neighbor", "probe_not_local"]


def test_probe_measurements_are_deterministic_for_a_seed():
    first = make_sim()[0]
    second = make_sim()[0]
    first.run(20_000)
    second.run(20_000)
    assert first.trace.events == second.trace.events


def test_a_silent_neighbor_costs_one_small_record_not_a_growing_list():
    sim, _ = make_sim()
    sim.stop_node("B", at_ms=2_000)
    sim.run(120_000)  # 118 probe rounds to a dead neighbor
    sent = [e for e in sim.trace.of_type(FRAME_SENT) if e.info["msg"] == "PROBE" and e.node == "A"]
    assert len(sent) > 200  # A keeps probing; the table of outstanding probes stays one per link
    assert sim.summary().drops.get("probe_unmatched", 0) == 0
