"""Local failover at the nodes (Phase 4b), without any controller.

Routes with backups are injected as C would originate them; everything after that is the nodes'
own decision, read from the trace.
"""

from iot_backend.protocol import (
    DataPayload,
    Frame,
    HelloNeighbor,
    HelloPayload,
    MessageType,
    RouteUpdatePayload,
    decode,
    encode,
)
from iot_backend.simulator import LinkConfig, ScenarioConfig, Simulation
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.nodes import Node, NodeSettings
from iot_backend.simulator.trace import (
    FRAME_RECEIVED,
    FRAME_REROUTED,
    FRAME_SENT,
    LOCAL_FAILOVER,
    LOCAL_NO_ROUTE,
    NEIGHBOR_CHANGED,
    SEND_FAILED,
    Trace,
)
from iot_backend.transports import LocalFailoverEvent

A, B, C, D = 1, 2, 3, 4
_seq = iter(range(5_000_000, 6_000_000))


def update(dst, version, next_hop, backup=None, epoch=1000, seq=None) -> bytes:
    payload = RouteUpdatePayload(epoch, version, C, next_hop, backup)
    return encode(Frame(C, dst, 0xC0FFEE01, next(_seq) if seq is None else seq, 8, payload))


def make_sim(links=None, install_at=5_000, **settings):
    """Four nodes, discovery on, no controller; A routes to C via B with D as its backup."""
    config = ScenarioConfig(
        seed=5,
        routing="controller",
        links=links or {},
        **{"local_failover": True, **settings},
    )
    sim = Simulation(config)
    events: list[LocalFailoverEvent] = []
    sim.gateway.subscribe(lambda e: events.append(e) if isinstance(e, LocalFailoverEvent) else 0)
    # fixed sequence numbers: two simulations built the same way are identical
    sim.inject_frame("B", update(B, 1, C, seq=9_000_001), install_at, "C")
    sim.inject_frame("D", update(D, 1, C, seq=9_000_002), install_at, "C")
    sim.inject_frame("A", update(A, 1, B, backup=D, seq=9_000_003), install_at + 50, "B")
    return sim, events


def failovers(sim, node="A"):
    return [e for e in sim.trace.of_type(LOCAL_FAILOVER) if e.node == node]


def test_nothing_happens_when_local_failover_is_off_even_with_a_backup_installed():
    sim, events = make_sim(local_failover=False)
    sim.stop_node("B", at_ms=10_000)
    sim.run(30_000)
    assert failovers(sim) == [] and events == []
    state = sim.forwarding_state("A", "C")
    assert state["backup"] == "D" and state["current"] == "B" and not state["failed_over"]


def test_a_healthy_network_has_no_failures_and_no_failover():
    sim, events = make_sim()
    sim.run(60_000)
    assert sim.trace.of_type(SEND_FAILED) == [] and failovers(sim) == [] and events == []
    assert sim.forwarding_state("A", "C")["current"] == "B"


# ---- T1: the neighbor is no longer two-way ----


def test_t1_fires_when_the_neighbor_is_lost():
    sim, events = make_sim(failover_t2_k=0)  # T2 off: T1 alone
    sim.stop_node("B", at_ms=10_000)
    sim.run(20_000)
    (fail,) = failovers(sim)
    assert fail.info["trigger"] == "T1_neighbor_state" and fail.info["state"] == "lost"
    assert fail.info["to"] == "D" and fail.info["failed"] == "B"
    assert 10_000 + 1_900 <= fail.time_ms <= 10_000 + 3_200  # the 3 s timeout, minus HELLO age
    assert sim.forwarding_state("A", "C")["current"] == "D"


def test_t1_fires_on_a_one_way_failure_where_a_timeout_only_rule_would_never_fire():
    sim, events = make_sim(failover_t2_k=0)
    sim.set_link_down("A", "B", True, at_ms=10_000)  # A cannot reach B; A still hears B
    sim.run(30_000)
    (fail,) = failovers(sim)
    assert fail.info["trigger"] == "T1_neighbor_state" and fail.info["state"] == "one_way"
    # A never stopped hearing B, so its timeout never fired: B was never "lost" at A
    changes = [
        e
        for e in sim.trace.of_type(NEIGHBOR_CHANGED)
        if e.node == "A" and e.info["neighbor"] == "B" and e.time_ms > 5_000
    ]
    assert [c.info["new"] for c in changes] == ["one_way"]
    heard_b = [
        e
        for e in sim.trace.of_type(FRAME_RECEIVED)
        if e.node == "A" and e.info["sender"] == "B" and e.info["msg"] == "HELLO"
    ]
    assert max(e.time_ms for e in heard_b) > 28_000  # still hearing B at the end
    assert sim.forwarding_state("A", "C")["current"] == "D"


def test_t1_can_be_switched_off():
    sim, _ = make_sim(failover_t1=False, failover_t2_k=0)
    sim.stop_node("B", at_ms=10_000)
    sim.run(30_000)
    assert failovers(sim) == []  # nothing left to trigger on


# ---- T2: consecutive link-layer delivery failures ----

# Frames are counted in the order they are offered to the A->B link: every unicast A sends to
# B (HELLO, PROBE, DATA, forwarded management frames) is one. T1 is off to isolate T2.


def t2_sim(drops, k=3):
    links = {("A", "B"): LinkConfig(delay_ms=10, drop_frames=frozenset(drops))}
    return make_sim(links=links, failover_t1=False, failover_t2_k=k)


def test_k_minus_one_consecutive_failures_do_not_trigger():
    sim, events = t2_sim({60, 61})
    sim.run(40_000)
    failed = [e for e in sim.trace.of_type(SEND_FAILED) if e.node == "A" and e.info["to"] == "B"]
    assert [e.info["consecutive"] for e in failed] == [1, 2]  # it really did fail twice
    assert failovers(sim) == [] and events == []


def test_k_consecutive_failures_trigger_and_report_the_trigger_and_its_timing():
    sim, events = t2_sim({60, 61, 62})
    sim.run(40_000)
    (fail,) = failovers(sim)
    assert fail.info["trigger"] == "T2_send_failures"
    assert fail.info["consecutive_failures"] == 3
    assert fail.info["since_first_failure_ms"] >= 0
    assert fail.info["to"] == "D" and fail.info["failed"] == "B"
    assert [(e.reason, e.to_next_hop) for e in events][:1] == [("send_failures", D)]
    # the neighbor is fine: T2 was a false alarm, which is why the controller decides the way back
    assert sim.forwarding_state("A", "C")["failed_over"] is True


def test_a_success_resets_the_count():
    sim, events = t2_sim({60, 61, 63, 64, 66, 67})  # two failures, a success, two ... again
    sim.run(40_000)
    assert failovers(sim) == []


def test_k_is_configurable():
    sim, _ = t2_sim({60, 61}, k=2)
    sim.run(40_000)
    assert len(failovers(sim)) == 1 and failovers(sim)[0].info["consecutive_failures"] == 2


def test_the_link_layer_can_report_failure_for_a_frame_that_did_arrive():
    # B -> A is down: A's frames reach B, but their acknowledgements cannot come back
    sim, events = make_sim(failover_t1=False, failover_t2_k=3)
    sim.set_link_down("B", "A", True, at_ms=10_000)
    sim.run(15_000)
    reported_failed = [
        e
        for e in sim.trace.of_type(SEND_FAILED)
        if e.node == "A" and e.info["to"] == "B" and e.time_ms > 10_000
    ]
    assert reported_failed and all(e.info["delivered"] is True for e in reported_failed[:3])
    arrived = [
        e
        for e in sim.trace.of_type(FRAME_RECEIVED)
        if e.node == "B" and e.info["sender"] == "A" and e.time_ms > 10_000
    ]
    assert arrived  # the frames did reach B
    assert len(failovers(sim)) == 1  # ... and A still concluded the next hop had failed


def test_a_frame_is_never_confirmed_delivered_by_the_link_layer_alone():
    """The callback is a next-hop signal: DATA sent to a relay that drops it is still 'ok'."""
    sim, _ = make_sim()
    # B cannot forward (its route to C is removed), yet its radio acknowledges A's frames
    sim.inject_frame("B", update(B, 2, None), 5_000, "C")
    sim.run(30_000)
    assert sim.trace.of_type(SEND_FAILED) == []  # every send to B was acknowledged by its radio
    assert sim.summary().delivered_unique < 3  # while nothing past B was ever delivered


# ---- what a failover does ----


def test_frames_already_queued_for_the_primary_are_rerouted_at_transmit_time():
    """Node level: four DATA frames wait in A's queue for B when B stops being two-way."""
    engine, trace, sent = Engine(), Trace(), []
    node = Node(
        A,
        engine,
        trace,
        lambda frm, to, data, ident, msg: sent.append((engine.now_ms, to, decode(data))),
        seed=1,
        permitted={B, D},
        routes={},
        settings=NodeSettings(
            local_failover=True,
            service_ms=100,
            failover_t2_k=0,
            hello_interval_ms=10**6,
            report_interval_ms=10**6,
            hello_jitter_ms=0,
        ),
    )
    node.boot()

    def hello(src, lists_us):
        listed = (HelloNeighbor(A, node.session),) if lists_us else ()
        frame = Frame(src, A, 0x1000 + src, 1, 1, HelloPayload(listed))
        node.receive(encode(frame), src)

    hello(B, True)
    hello(D, True)  # both two-way
    node.receive(update(A, 1, B, backup=D, seq=1), B)  # relayed by B
    data = [Frame(7, C, 77, n, 8, DataPayload(n)) for n in range(1, 5)]
    for frame in data:
        node.receive(encode(frame), D)  # A relays four readings: they queue for B
    assert node.queue_length >= 4  # plus the route acknowledgement being flooded
    hello(B, False)  # B no longer lists A: one-way, so T1 fires and the queue is not touched
    assert node.forwarding_state(C)["failed_over"] is True
    engine.run_until(2_000)
    data_sends = [(to, f) for _, to, f in sent if f.msg_type is MessageType.DATA]
    assert [to for to, _ in data_sends] == [D, D, D, D]  # all four went to the backup
    assert len(trace.of_type(FRAME_REROUTED)) == 4


def test_the_failover_is_reported_by_flooding_and_the_data_follows_the_backup():
    sim, events = make_sim()
    sim.stop_node("B", at_ms=10_000)
    sim.run(30_000)
    assert events and events[0].origin == A and events[0].to_next_hop == D
    assert events[0].from_next_hop == B and (events[0].epoch, events[0].version) == (1000, 1)
    after = [
        e
        for e in sim.trace.of_type(FRAME_SENT)
        if e.node == "A" and e.info["msg"] == "DATA" and e.time_ms > fail_time(sim)
    ]
    assert after and {e.info["to"] for e in after} <= {"D"}
    assert sim.summary().delivered_unique >= 10


def fail_time(sim):
    return failovers(sim)[0].time_ms + 100


def test_a_failed_over_node_never_switches_back_by_itself():
    sim, _ = make_sim()
    sim.stop_node("B", at_ms=10_000)
    sim.start_node("B", at_ms=15_000)
    sim.run(60_000)
    # B is back and two-way again, but A's table still says D
    assert sim.forwarding_state("A", "C")["current"] == "D"
    sends_to_b = [
        e
        for e in sim.trace.of_type(FRAME_SENT)
        if e.node == "A"
        and e.info["msg"] == "DATA"
        and e.info["to"] == "B"
        and e.time_ms > fail_time(sim)
    ]
    assert sends_to_b == []
    # (B itself lost its entry when it rebooted; only a controller can change this)


def test_only_a_newer_version_ends_a_failover():
    sim, _ = make_sim()
    sim.stop_node("B", at_ms=10_000)
    sim.inject_frame("A", update(A, 1, B, backup=D), 20_000, "D")  # the same version again
    sim.run(21_000)
    assert sim.forwarding_state("A", "C")["failed_over"] is True  # a duplicate changes nothing
    sim.inject_frame("A", update(A, 2, D), 22_000, "D")
    sim.run(23_000)
    state = sim.forwarding_state("A", "C")
    assert (state["version"], state["failed_over"], state["current"]) == (2, False, "D")


def test_a_controller_reinstalling_a_dead_primary_cannot_pull_the_node_back():
    sim, _ = make_sim(failover_t2_k=3)
    sim.stop_node("B", at_ms=10_000)
    sim.run(16_000)
    assert len(failovers(sim)) == 1
    # the controller (not knowing yet) installs version 2 pointing at B again
    sim.inject_frame("A", update(A, 2, B, backup=D), 16_500, "D")
    sim.run(20_000)
    fails = failovers(sim)
    assert len(fails) == 2 and fails[1].time_ms < 16_700  # abandoned again at once, on install
    assert sim.forwarding_state("A", "C")["current"] == "D"


# ---- the backup is also unusable ----


def test_if_the_backup_is_dead_too_the_node_has_no_route_locally_and_says_so():
    sim, events = make_sim(failover_report_repeats=3)
    sim.stop_node("B", at_ms=10_000)
    sim.stop_node("D", at_ms=10_000)
    sim.run(120_000)
    no_route = [e for e in sim.trace.of_type(LOCAL_NO_ROUTE) if e.node == "A"]
    assert len(no_route) == 1
    state = sim.forwarding_state("A", "C")
    assert state["local_no_route"] is True and state["current"] is None
    # readings created afterwards are dropped immediately with a reason, not queued
    s = sim.summary()
    assert s.drops["no_route"] > 20 and s.unresolved <= 33
    # the report is bounded: the first copy plus at most the configured repeats per event
    assert sim.node_counters("A").failover_reports <= 2 * (1 + 3)


def test_a_node_with_no_backup_at_all_goes_local_no_route_when_its_primary_dies():
    sim, events = make_sim()
    # B's only way to C is C itself and it has no backup; cut the B-C link in both directions
    sim.set_link_down("B", "C", True, at_ms=12_000)
    sim.set_link_down("C", "B", True, at_ms=12_000)
    sim.run(30_000)
    b_events = [e for e in sim.trace.of_type(LOCAL_NO_ROUTE) if e.node == "B"]
    assert len(b_events) == 1 and b_events[0].info["why"] == "no_backup"
    reports = [e for e in events if e.origin == B]
    assert not reports or reports[0].to_next_hop is None  # reported as "no route" if it got out


def test_the_whole_run_is_deterministic_for_a_seed():
    def run():
        sim, events = make_sim()
        sim.stop_node("B", at_ms=10_000)
        sim.run(30_000)
        return sim.trace.events, events

    assert run() == run()
