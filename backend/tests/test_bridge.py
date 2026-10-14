"""Node C's laptop-link bridge in isolation: heartbeat watching, buffering, coalescing, replay."""

from iot_backend.simulator.bridge import BridgeConfig, CBridge
from iot_backend.simulator.engine import Engine
from iot_backend.transports import (
    GatewayStatusEvent,
    LocalFailoverEvent,
    NeighborObservation,
    ReadingEvent,
    ReplayEndEvent,
    ReplayStartEvent,
    TelemetryEvent,
    TopologyReportEvent,
)

A, B, C, D = 1, 2, 3, 4


def make(timeout=3000, capacity=50, spacing=20):
    engine = Engine()
    out: list = []
    bridge = CBridge(engine, BridgeConfig(timeout, capacity, spacing), out.append)
    return engine, bridge, out


def reading(seq, origin=A, session=100, at=0, duplicate=False):
    return ReadingEvent(at, origin, session, seq, 2000 + seq, B, 7, 2, duplicate)


def report(origin, seq=1, at=0, state="two_way"):
    return TopologyReportEvent(
        at, origin, 500 + origin, seq, (NeighborObservation(B, state, 1, 100, 30),)
    )


def telemetry(origin, created=0, at=0):
    return TelemetryEvent(at, origin, 500 + origin, 1, created, 0, 0, 0, 0, 0, 0, 0, 0)


def failover(origin=A, repeat=0):
    return LocalFailoverEvent(0, origin, 100, 40 + repeat, 1000, 1, 3, B, D, "send_failures")


def kinds(messages):
    return [type(m).__name__ for m in messages]


def beat(engine, bridge, until_ms, every=1000):
    """Deliver heartbeats every `every` ms up to `until_ms`."""
    while engine.now_ms + every <= until_ms:
        engine.run_until(engine.now_ms + every)
        bridge.heartbeat()
    engine.run_until(until_ms)


def test_with_no_timeout_configured_everything_passes_straight_up():
    engine, bridge, out = make(timeout=0)
    bridge.up(reading(1))
    bridge.up(report(A))
    assert kinds(out) == ["ReadingEvent", "TopologyReportEvent"]
    assert bridge.size == 0 and bridge.counters.buffered == 0
    bridge.heartbeat()  # still answered, so the laptop can see C
    assert kinds(out)[-1] == "GatewayStatusEvent"


def test_events_before_the_first_heartbeat_are_held_then_replayed_with_ages():
    engine, bridge, out = make()
    engine.run_until(1_000)
    bridge.up(reading(1))
    engine.run_until(1_500)
    bridge.up(reading(2))
    assert out == [] and bridge.held == 2  # nobody is listening yet
    engine.run_until(4_000)
    bridge.heartbeat()
    engine.run_until(5_000)
    assert kinds(out) == [
        "GatewayStatusEvent",
        "ReplayStartEvent",
        "ReadingEvent",
        "ReadingEvent",
        "ReplayEndEvent",
    ]
    first, second = out[2], out[3]
    # ages are C's own differences: when it arrived at C versus when it was handed up
    assert (first.replayed, second.replayed) == (True, True)
    assert first.age_ms == 3_020 and second.age_ms == 2_540  # 4_020 - 1_000, 4_040 - 1_500
    assert [first.seq, second.seq] == [1, 2]  # order preserved
    start = out[1]
    assert isinstance(start, ReplayStartEvent) and (start.entries, start.held) == (2, 2)
    end = out[-1]
    assert isinstance(end, ReplayEndEvent) and end.replayed == 2


def test_an_age_is_never_taken_from_a_timestamp_inside_the_event():
    engine, bridge, out = make()
    engine.run_until(10_000)
    bridge.heartbeat()
    engine.run_until(20_000)  # unreachable by now
    wild = reading(1, at=987_654_321)  # a nonsense "received_ms": C must not trust it
    bridge.up(wild)
    engine.run_until(25_000)
    bridge.heartbeat()
    engine.run_until(26_000)
    replayed = [m for m in out if isinstance(m, ReadingEvent)]
    assert replayed[-1].age_ms == 5_020  # 25_020 - 20_000: C's own clock only


def test_c_declares_the_controller_unreachable_exactly_one_timeout_after_the_last_heartbeat():
    engine, bridge, out = make(timeout=3000)
    beat(engine, bridge, 5_000)
    assert bridge.reachable
    engine.run_until(7_999)
    assert bridge.reachable  # last heartbeat at 5_000: not yet
    engine.run_until(8_001)
    assert not bridge.reachable and bridge.counters.outages == 1


def test_a_late_heartbeat_inside_the_timeout_keeps_it_reachable():
    engine, bridge, out = make(timeout=3000)
    beat(engine, bridge, 4_000)
    engine.run_until(6_900)
    bridge.heartbeat()  # 2.9 s after the previous one
    engine.run_until(9_000)
    assert bridge.reachable and bridge.counters.outages == 0


def test_readings_reach_the_laptop_live_while_it_is_reachable():
    engine, bridge, out = make()
    bridge.heartbeat()
    out.clear()
    bridge.up(reading(1))
    assert kinds(out) == ["ReadingEvent"] and not out[0].replayed and out[0].age_ms == 0


def test_reports_and_telemetry_coalesce_to_one_slot_per_origin():
    engine, bridge, out = make()
    for n in range(300):
        engine.run_until(n * 10)
        for origin in (A, B, D):
            bridge.up(report(origin, seq=n))
            bridge.up(telemetry(origin, created=n))
    assert bridge.size == 6  # three origins, two kinds
    assert bridge.counters.coalesced == 3 * 2 * 299
    bridge.heartbeat()
    engine.run_until(5_000)
    replayed_reports = [m for m in out if isinstance(m, TopologyReportEvent)]
    assert sorted(r.origin for r in replayed_reports) == [A, B, D]
    assert all(r.seq == 299 for r in replayed_reports)  # the newest of each, nothing older


def test_failover_reports_are_always_kept_and_repeats_keep_the_first_age():
    engine, bridge, out = make(capacity=8)
    engine.run_until(1_000)
    bridge.up(failover(A, repeat=0))
    for n in range(1, 6):
        engine.run_until(1_000 + n * 5_000)
        bridge.up(failover(A, repeat=0))  # the node repeats the same report
    for n in range(50):  # then a flood of readings that overflows the buffer
        bridge.up(reading(n))
    engine.run_until(40_000)
    bridge.heartbeat()
    engine.run_until(41_000)
    replayed = [m for m in out if isinstance(m, LocalFailoverEvent)]
    assert len(replayed) == 1  # one slot for the repeats, and it survived the overflow
    assert replayed[0].age_ms >= 39_000  # the age of the *first* copy


def test_the_oldest_least_valuable_entry_is_dropped_first_and_readings_are_preferred():
    engine, bridge, out = make(capacity=5)
    bridge.up(report(A))
    bridge.up(report(B))
    bridge.up(telemetry(A))
    for n in range(1, 9):
        bridge.up(reading(n))  # 8 readings into a 5-slot buffer
    slots = bridge.slots()
    assert [k for k, _, _ in slots] == ["reading"] * 5  # every report and telemetry gave way
    assert [key[3] for _, key, _ in slots] == [4, 5, 6, 7, 8]  # and the oldest readings went next
    # overflow is counted, and only held entries count
    assert bridge.counters.dropped_overflow == 3 + 3  # 3 non-readings, then 3 readings


def test_a_report_never_displaces_a_reading_from_a_full_buffer():
    engine, bridge, out = make(capacity=3)
    for n in range(1, 4):
        bridge.up(reading(n))
    before = bridge.slots()
    bridge.up(report(A))
    assert bridge.slots() == before
    assert bridge.counters.dropped_overflow == 1  # the report itself, counted


def test_replay_happens_in_arrival_order_between_markers_with_counts():
    engine, bridge, out = make(capacity=6, spacing=10)
    for n in range(1, 10):
        engine.run_until(n * 100)
        bridge.up(reading(n))
    engine.run_until(10_000)
    bridge.heartbeat()
    engine.run_until(11_000)
    start = next(m for m in out if isinstance(m, ReplayStartEvent))
    end = next(m for m in out if isinstance(m, ReplayEndEvent))
    seqs = [m.seq for m in out if isinstance(m, ReadingEvent)]
    assert seqs == [4, 5, 6, 7, 8, 9]  # the six newest, in order
    assert (start.entries, start.dropped_overflow) == (6, 3)
    assert (end.replayed, end.dropped_overflow) == (6, 3)
    assert kinds(out).index("ReplayStartEvent") < kinds(out).index("ReadingEvent")


def test_events_arriving_during_a_replay_go_after_the_older_ones():
    engine, bridge, out = make(spacing=100)
    for n in range(1, 4):
        bridge.up(reading(n))
    bridge.heartbeat()
    engine.run_until(150)  # replay under way (one entry out)
    bridge.up(reading(99))  # a live reading arrives mid-replay
    engine.run_until(2_000)
    assert [m.seq for m in out if isinstance(m, ReadingEvent)] == [1, 2, 3, 99]
    assert kinds(out)[-1] == "ReplayEndEvent"


def test_losing_the_controller_again_during_a_replay_keeps_the_rest_for_next_time():
    engine, bridge, out = make(spacing=1_000, timeout=2_500)
    for n in range(1, 6):
        bridge.up(reading(n))
    bridge.heartbeat()
    engine.run_until(2_500)  # two entries out; then silence for the timeout
    engine.run_until(6_000)
    assert not bridge.reachable and not bridge.replaying
    sent_so_far = [m.seq for m in out if isinstance(m, ReadingEvent)]
    assert sent_so_far == [1, 2] and bridge.size == 3
    bridge.heartbeat()
    beat(engine, bridge, 12_000)  # the controller stays up this time
    assert [m.seq for m in out if isinstance(m, ReadingEvent)] == [1, 2, 3, 4, 5]


def test_in_normal_operation_the_buffer_holds_only_the_last_two_heartbeat_intervals():
    engine, bridge, out = make()
    bridge.heartbeat()
    for second in range(1, 31):
        engine.run_until(second * 1000)
        bridge.heartbeat()
        for n in range(5):
            bridge.up(reading(second * 10 + n))
    assert bridge.held == 0
    assert bridge.size <= 12  # about two seconds of entries, not thirty


def test_status_replies_carry_the_counters():
    engine, bridge, out = make(capacity=4)
    for n in range(10):
        bridge.up(reading(n))
    bridge.heartbeat()
    status = next(m for m in out if isinstance(m, GatewayStatusEvent))
    assert status.controller_reachable and status.held == 4 and status.capacity == 4
    assert status.dropped_overflow == 6
