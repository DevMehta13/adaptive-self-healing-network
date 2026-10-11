"""Simulator tests. They assert on the public event trace, not on node internals."""

import pytest

from iot_backend.protocol import MAX_FRAME_SIZE, DataPayload, Frame, encode
from iot_backend.simulator import LinkConfig, ScenarioConfig, Simulation, Trace
from iot_backend.simulator.__main__ import main
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.trace import (
    ACK_IGNORED,
    ACK_RECEIVED,
    ACK_TIMEOUT,
    FRAME_DROPPED,
    FRAME_DROPPED_BY_LINK,
    FRAME_RECEIVED,
    FRAME_REJECTED,
    FRAME_SENT,
    NODE_BOOTED,
    READING_DELIVERED,
)


def make_sim(seed=1, a_to_b=None, b_to_a=None, **kwargs) -> Simulation:
    links = {}
    if a_to_b is not None:
        links[("A", "B")] = a_to_b
    if b_to_a is not None:
        links[("B", "A")] = b_to_a
    return Simulation(ScenarioConfig(seed=seed, links=links, **kwargs))


def sent_readings(sim):
    return [e.identity for e in sim.trace.of_type(FRAME_SENT) if e.info["msg"] == "DATA"]


# ---- the five required scenarios ----


def test_normal_delivery_every_reading_once_and_acknowledged():
    sim = make_sim()
    sim.run(21_000)  # readings at 2, 4, ..., 20 s; the last ACK arrives at 20.04 s
    readings = sent_readings(sim)
    delivered = [e.identity for e in sim.trace.of_type(READING_DELIVERED)]
    acked = [e.identity for e in sim.trace.of_type(ACK_RECEIVED)]
    assert len(readings) == 10
    assert delivered == readings  # each exactly once, in order
    assert acked == readings
    assert sim.trace.of_type(ACK_TIMEOUT) == []
    s = sim.summary()
    assert (s.readings_created, s.delivered, s.acknowledged, s.unacknowledged) == (10, 10, 10, 0)
    assert s.unresolved == 0


def test_dropped_data_leaves_that_reading_unacknowledged_and_undelivered():
    sim = make_sim(a_to_b=LinkConfig(drop_frames=frozenset({3})))
    sim.run(21_000)
    readings = sent_readings(sim)
    lost = readings[2]
    drops = sim.trace.of_type(FRAME_DROPPED_BY_LINK)
    assert [(d.identity, d.info["reason"]) for d in drops] == [(lost, "scripted")]
    assert [e.identity for e in sim.trace.of_type(ACK_TIMEOUT)] == [lost]
    delivered = [e.identity for e in sim.trace.of_type(READING_DELIVERED)]
    assert lost not in delivered
    assert delivered == [r for r in readings if r != lost]
    s = sim.summary()
    assert (s.delivered, s.acknowledged, s.unacknowledged) == (9, 9, 1)


def test_dropped_ack_reading_was_delivered_but_sender_cannot_know():
    sim = make_sim(b_to_a=LinkConfig(drop_frames=frozenset({2})))
    sim.run(21_000)
    readings = sent_readings(sim)
    unconfirmed = readings[1]
    # B accepted it ...
    assert unconfirmed in [e.identity for e in sim.trace.of_type(READING_DELIVERED)]
    # ... but A never saw an ACK and reports it unacknowledged.
    assert unconfirmed not in [e.identity for e in sim.trace.of_type(ACK_RECEIVED)]
    assert [e.identity for e in sim.trace.of_type(ACK_TIMEOUT)] == [unconfirmed]
    s = sim.summary()
    assert (s.delivered, s.acknowledged, s.unacknowledged) == (10, 9, 1)


def lossy(seed):
    link = LinkConfig(loss=0.4)
    sim = make_sim(seed=seed, a_to_b=link, b_to_a=link)
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

    assert loss_pattern(first)  # losses actually happened
    assert loss_pattern(lossy(8)) != loss_pattern(first)


def test_malformed_frames_are_rejected_recorded_and_do_not_stop_the_simulation():
    sim = make_sim()
    bad_version = bytearray(encode(Frame(1, 2, 5, 1, 8, DataPayload(1))))
    bad_version[0] = 9
    sim.inject_frame("B", b"\x01\x02\x03", at_ms=3_000, from_name="A")
    sim.inject_frame("B", bytes(bad_version), at_ms=3_500, from_name="A")
    sim.inject_frame("A", b"", at_ms=3_600, from_name="B")
    sim.run(11_000)
    rejected = [(e.node, e.info["reason"]) for e in sim.trace.of_type(FRAME_REJECTED)]
    assert rejected == [("B", "truncated"), ("B", "bad_version"), ("A", "truncated")]
    # readings before and after the garbage are still delivered and acknowledged
    s = sim.summary()
    assert s.readings_created == 5
    assert s.frames_sent == 5
    assert s.acknowledged == 5
    assert s.frames_rejected == 3


def test_valid_frame_for_another_node_is_rejected_as_wrong_destination():
    sim = make_sim()
    frame = Frame(src=3, dst=4, session=9, seq=1, ttl=8, payload=DataPayload(5))
    sim.inject_frame("B", encode(frame), at_ms=1_000, from_name="A")
    sim.run(1_500)
    (event,) = sim.trace.of_type(FRAME_REJECTED)
    assert event.info["reason"] == "wrong_destination"
    assert sim.summary().delivered == 0


def test_restarted_a_has_new_session_and_sequence_restarts_without_confusion():
    # Restart just after reading #1 is sent. Its ACK is still in flight and reaches the
    # restarted A, which must not mistake it for anything of the new session.
    sim = make_sim()
    sim.restart_node("A", at_ms=2_010)
    sim.run(9_000)

    boots = [e for e in sim.trace.of_type(NODE_BOOTED) if e.node == "A"]
    assert len(boots) == 2
    assert boots[0].info["session"] != boots[1].info["session"]

    readings = sent_readings(sim)
    old_session = readings[0].session
    new = [r for r in readings if r.session != old_session]
    assert readings[0].seq == 1
    assert new[0].seq == 1  # numbering restarted
    assert new[0].session != old_session
    assert new[0] != readings[0]  # same seq, different identity

    # The old reading's ACK was ignored, not matched to the new session's reading #1.
    ignored = sim.trace.of_type(ACK_IGNORED)
    assert [e.identity for e in ignored] == [readings[0]]
    # No stale timer from before the restart fires after it.
    assert readings[0] not in [e.identity for e in sim.trace.of_type(ACK_TIMEOUT)]
    # New readings are acknowledged normally under the new session.
    acked = [e.identity for e in sim.trace.of_type(ACK_RECEIVED)]
    assert acked == new
    # Both sessions' readings were recorded by B as separate packets.
    delivered = [e.identity for e in sim.trace.of_type(READING_DELIVERED)]
    assert delivered == readings


# ---- supporting behaviour ----


def test_frames_on_the_wire_are_valid_protocol_frames_within_the_limit():
    sim = make_sim()
    sim.run(5_000)
    sizes = {e.info["msg"]: e.info["size"] for e in sim.trace.of_type(FRAME_SENT)}
    assert sizes == {"DATA": 18, "DELIVERY_ACK": 23}
    assert all(size <= MAX_FRAME_SIZE for size in sizes.values())


def test_delay_is_applied_in_virtual_time():
    sim = make_sim(a_to_b=LinkConfig(delay_ms=100), b_to_a=LinkConfig(delay_ms=250))
    sim.run(3_000)
    (acked,) = sim.trace.of_type(ACK_RECEIVED)
    assert acked.time_ms == 2_000 + 100 + 250
    assert acked.info["rtt_ms"] == 350


def test_ack_slower_than_timeout_is_reported_unacknowledged_then_ignored():
    sim = make_sim(b_to_a=LinkConfig(delay_ms=1_500))
    sim.run(4_000)
    first = sent_readings(sim)[0]
    assert [e.identity for e in sim.trace.of_type(ACK_TIMEOUT)] == [first]
    assert [e.identity for e in sim.trace.of_type(ACK_IGNORED)] == [first]
    assert sim.trace.of_type(ACK_RECEIVED) == []


def test_unknown_link_in_config_is_refused():
    with pytest.raises(ValueError):
        Simulation(ScenarioConfig(links={("A", "C"): LinkConfig()}))


def test_link_config_validation():
    with pytest.raises(ValueError):
        LinkConfig(loss=1.5)
    with pytest.raises(ValueError):
        LinkConfig(delay_ms=-1)
    with pytest.raises(ValueError):
        LinkConfig(drop_frames=frozenset({0}))


# ---- engine and trace ----


def test_engine_runs_in_time_order_without_real_waiting():
    engine = Engine()
    order = []
    engine.schedule(500, lambda: order.append("late"))
    engine.schedule(100, lambda: order.append("early"))
    engine.schedule(100, lambda: order.append("early-second"))  # same time: schedule order
    engine.run_until(3_600_000)  # one simulated hour completes instantly
    assert order == ["early", "early-second", "late"]
    assert engine.now_ms == 3_600_000


def test_engine_actions_can_schedule_more_actions_and_future_ones_wait():
    engine = Engine()
    seen = []
    engine.schedule(10, lambda: engine.schedule(10, lambda: seen.append(engine.now_ms)))
    engine.schedule(100, lambda: seen.append(engine.now_ms))
    engine.run_until(50)
    assert seen == [20]
    engine.run_until(100)
    assert seen == [20, 100]


def test_engine_refuses_the_past():
    with pytest.raises(ValueError):
        Engine().schedule(-1, lambda: None)


def test_trace_is_bounded_and_counts_what_it_dropped():
    trace = Trace(max_events=3)
    for i in range(5):
        trace.record(i, "A", "x")
    assert [e.time_ms for e in trace.events] == [2, 3, 4]
    assert trace.dropped == 2


# ---- command line ----


def test_cli_prints_trace_and_summary(capsys):
    assert main(["--seed", "42", "--duration", "5", "--drop", "A-B:1"]) == 0
    out = capsys.readouterr().out
    assert "frame_dropped_by_link" in out
    assert "Summary" in out
    assert "readings created by A ..... 2" in out


def test_receive_reports_the_link_layer_sender():
    sim = make_sim()
    sim.run(3_000)
    received = sim.trace.of_type(FRAME_RECEIVED)
    assert [(e.node, e.info["sender"]) for e in received] == [("B", "A"), ("A", "B")]


def test_summary_counts_survive_a_trace_that_overflowed():
    sim = make_sim(trace_max_events=5)  # tiny trace: most events are dropped
    sim.run(21_000)
    assert sim.trace.dropped > 0
    s = sim.summary()
    assert (s.readings_created, s.frames_sent, s.acknowledged) == (10, 10, 10)


def test_stopped_node_ignores_frames_and_forgets_pending_readings():
    sim = make_sim()
    sim.stop_node("B", at_ms=3_000)  # reading #1 was acknowledged at 2.04 s
    sim.run(9_000)
    dropped = sim.trace.of_type(FRAME_DROPPED)
    assert {e.node for e in dropped} == {"B"}
    assert {e.info["reason"] for e in dropped} == {"node_stopped"}
    s = sim.summary()
    assert s.acknowledged == 1  # only the first reading, before B stopped
    assert s.frames_dropped_while_stopped == 3  # readings at 4, 6, 8 s
    assert s.unacknowledged == 3


def test_stopping_a_forgets_its_pending_timers_and_start_boots_a_new_session():
    sim = make_sim(b_to_a=LinkConfig(delay_ms=900))  # ACKs arrive at 2.92 s
    sim.stop_node("A", at_ms=2_500)  # reading #1 pending; its timeout would fire at 3.0 s
    sim.start_node("A", at_ms=6_500)
    sim.run(10_000)
    assert sim.trace.of_type(ACK_TIMEOUT) == []  # the old timer did nothing
    boots = [e.info["session"] for e in sim.trace.of_type(NODE_BOOTED) if e.node == "A"]
    assert len(boots) == 2 and boots[0] != boots[1]
    # the first reading of the new session is created one interval after the start
    new_session = int(boots[1], 16)
    first_new = [r for r in sent_readings(sim) if r.session == new_session][0]
    assert first_new.seq == 1
    assert sim.summary().unresolved >= 1  # reading #1 was forgotten, not "resolved"
