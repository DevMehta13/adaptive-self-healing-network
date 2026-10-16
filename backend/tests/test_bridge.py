"""Node C's laptop-link bridge in isolation: heartbeat watching, buffering, coalescing, replay,
and the reliable uplink (numbering, cumulative acknowledgement, window, resend, gone)."""

from iot_backend.protocol import (
    DataPayload,
    Frame,
    LocalFailoverReportPayload,
    MessageType,
    ReportEntry,
    TelemetryPayload,
    TopologyReportPayload,
    decode,
)
from iot_backend.simulator.bridge import BridgeConfig, CBridge
from iot_backend.simulator.engine import Engine
from iot_backend.transports.wire import (
    AckDown,
    FrameUp,
    GoneUp,
    HeartbeatDown,
    HelloUp,
    ReplayEndUp,
    ReplayStartUp,
    ResendDown,
    StatusUp,
    TransmitDown,
)

A, B, C, D = 1, 2, 3, 4
BOOT = 0xC0FFEE


def make(timeout=3000, capacity=50, spacing=20, window=16, originate=None):
    engine = Engine()
    out: list = []
    bridge = CBridge(engine, BridgeConfig(timeout, capacity, spacing, window), out.append)
    if originate is not None:
        bridge.attach_originator(originate)
    bridge.boot_up(BOOT)
    out.clear()  # the hello
    return engine, bridge, out


def reading(seq, origin=A, session=100):
    return Frame(origin, C, session, seq, 7, DataPayload(2000 + seq))


def report(origin, seq=1):
    return Frame(
        origin, C, 500 + origin, seq, 8, TopologyReportPayload((ReportEntry(B, 2, 1, 100),))
    )


def telemetry(origin, created=0):
    return Frame(origin, C, 500 + origin, 1, 8, TelemetryPayload(created=created))


def failover(origin=A, seq=40):
    return Frame(origin, C, 100, seq, 8, LocalFailoverReportPayload(1000, 1, C, B, D, 2))


def up(bridge, frame, via=B, duplicate=False):
    bridge.from_radio(frame, via, duplicate)


def hb(bridge, ack=None):
    bridge.receive(HeartbeatDown(bridge.boot, bridge.acked if ack is None else ack))


def kinds(messages):
    return [type(m).__name__ for m in messages]


def frames(messages):
    """(n, decoded frame, message) for every frame handed up."""
    return [(m.n, decode(m.frame), m) for m in messages if isinstance(m, FrameUp)]


def seqs(messages):
    return [f.seq for _, f, _ in frames(messages) if f.msg_type is MessageType.DATA]


def beat(engine, bridge, until_ms, every=1000, ack=True):
    """Heartbeats every `every` ms up to `until_ms`, acknowledging everything sent so far."""
    while engine.now_ms + every <= until_ms:
        engine.run_until(engine.now_ms + every)
        hb(bridge, bridge.next_n - 1 if ack else None)
    engine.run_until(until_ms)


# ---- Phase 5b behaviour, unchanged in substance ----


def test_with_no_timeout_configured_everything_passes_straight_up_numbered():
    engine, bridge, out = make(timeout=0)
    up(bridge, reading(1))
    up(bridge, report(A))
    assert kinds(out) == ["FrameUp", "FrameUp"] and [m.n for m in out] == [1, 2]
    assert bridge.size == 0 and bridge.counters.buffered == 0  # nothing is kept
    hb(bridge)  # still answered, so the laptop can see C
    assert isinstance(out[-1], StatusUp) and out[-1].capacity == 0


def test_events_before_the_first_heartbeat_are_held_then_replayed_with_ages():
    engine, bridge, out = make()
    engine.run_until(1_000)
    up(bridge, reading(1))
    engine.run_until(1_500)
    up(bridge, reading(2))
    assert out == [] and bridge.held == 2  # nobody is listening yet
    engine.run_until(4_000)
    hb(bridge)
    engine.run_until(5_000)
    assert kinds(out) == ["StatusUp", "ReplayStartUp", "FrameUp", "FrameUp", "ReplayEndUp"]
    first, second = out[2], out[3]
    # ages are C's own differences: when it arrived at C versus when it was handed up
    assert (first.replayed, second.replayed) == (True, True)
    assert first.age_ms == 3_020 and second.age_ms == 2_540  # 4_020 - 1_000, 4_040 - 1_500
    assert seqs(out) == [1, 2] and [first.n, second.n] == [1, 2]  # order preserved
    start = out[1]
    assert isinstance(start, ReplayStartUp) and (start.entries, start.held) == (2, 2)
    end = out[-1]
    assert isinstance(end, ReplayEndUp) and end.replayed == 2


def test_an_age_is_only_ever_taken_from_c_own_clock():
    engine, bridge, out = make()
    engine.run_until(10_000)
    hb(bridge)
    engine.run_until(20_000)  # unreachable by now
    up(bridge, reading(1))
    engine.run_until(25_000)
    hb(bridge)
    engine.run_until(26_000)
    assert frames(out)[-1][2].age_ms == 5_020  # 25_020 - 20_000


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
    hb(bridge)  # 2.9 s after the previous one
    engine.run_until(9_000)
    assert bridge.reachable and bridge.counters.outages == 0


def test_readings_reach_the_laptop_live_while_it_is_reachable():
    engine, bridge, out = make()
    hb(bridge)
    out.clear()
    up(bridge, reading(1))
    assert kinds(out) == ["FrameUp"] and not out[0].replayed and out[0].age_ms == 0
    assert out[0].via == B and out[0].boot == BOOT and out[0].n == 1


def test_reports_and_telemetry_coalesce_to_one_slot_per_origin():
    engine, bridge, out = make()
    for n in range(300):
        engine.run_until(n * 10)
        for origin in (A, B, D):
            up(bridge, report(origin, seq=n))
            up(bridge, telemetry(origin, created=n))
    assert bridge.size == 6  # three origins, two kinds
    assert bridge.counters.coalesced == 3 * 2 * 299
    hb(bridge)
    engine.run_until(5_000)
    replayed = [f for _, f, _ in frames(out) if f.msg_type is MessageType.TOPOLOGY_REPORT]
    assert sorted(f.src for f in replayed) == [A, B, D]
    assert all(f.seq == 299 for f in replayed)  # the newest of each, nothing older


def test_failover_reports_are_always_kept_and_repeats_keep_the_first_age():
    engine, bridge, out = make(capacity=8, window=4)
    engine.run_until(1_000)
    up(bridge, failover(A))
    for n in range(1, 6):
        engine.run_until(1_000 + n * 5_000)
        up(bridge, failover(A, seq=40 + n))  # the node repeats the same report
    for n in range(50):  # then a flood of readings that overflows the buffer
        up(bridge, reading(n))
    engine.run_until(40_000)
    beat(engine, bridge, 45_000)
    replayed = [m for _, f, m in frames(out) if f.msg_type is MessageType.LOCAL_FAILOVER_REPORT]
    assert len(replayed) == 1  # one slot for the repeats, and it survived the overflow
    assert replayed[0].age_ms >= 39_000  # the age of the *first* copy


def test_the_oldest_least_valuable_entry_is_dropped_first_and_readings_are_preferred():
    engine, bridge, out = make(capacity=5, window=2)
    up(bridge, report(A))
    up(bridge, report(B))
    up(bridge, telemetry(A))
    for n in range(1, 9):
        up(bridge, reading(n))  # 8 readings into a 5-slot buffer
    slots = bridge.slots()
    assert [k for k, _, _ in slots] == ["reading"] * 5  # every report and telemetry gave way
    assert [key[3] for _, key, _ in slots] == [4, 5, 6, 7, 8]  # and the oldest readings went next
    assert bridge.counters.dropped_overflow == 3 + 3  # 3 non-readings, then 3 readings


def test_a_report_never_displaces_a_reading_from_a_full_buffer():
    engine, bridge, out = make(capacity=3, window=2)
    for n in range(1, 4):
        up(bridge, reading(n))
    before = bridge.slots()
    up(bridge, report(A))
    assert bridge.slots() == before
    assert bridge.counters.dropped_overflow == 1  # the report itself, counted


def test_replay_happens_in_arrival_order_between_markers_with_counts():
    engine, bridge, out = make(capacity=6, spacing=10, window=4)
    for n in range(1, 10):
        engine.run_until(n * 100)
        up(bridge, reading(n))
    engine.run_until(10_000)
    beat(engine, bridge, 13_000, every=200)  # acknowledgements keep the window open
    start = next(m for m in out if isinstance(m, ReplayStartUp))
    end = next(m for m in out if isinstance(m, ReplayEndUp))
    assert seqs(out) == [4, 5, 6, 7, 8, 9]  # the six newest, in order
    assert (start.entries, start.dropped_overflow) == (6, 3)
    assert (end.replayed, end.dropped_overflow) == (6, 3)
    assert kinds(out).index("ReplayStartUp") < kinds(out).index("FrameUp")


def test_events_arriving_during_a_replay_go_after_the_older_ones():
    engine, bridge, out = make(spacing=100)
    for n in range(1, 4):
        up(bridge, reading(n))
    hb(bridge)
    engine.run_until(150)  # replay under way (one entry out)
    up(bridge, reading(99))  # a live reading arrives mid-replay
    engine.run_until(2_000)
    assert seqs(out) == [1, 2, 3, 99]
    assert kinds(out)[-1] == "ReplayEndUp"


def test_losing_the_controller_again_during_a_replay_keeps_the_rest_for_next_time():
    engine, bridge, out = make(spacing=1_000, timeout=2_500)
    for n in range(1, 6):
        up(bridge, reading(n))
    hb(bridge)
    engine.run_until(2_500)  # two entries out; then silence for the timeout
    engine.run_until(6_000)
    assert not bridge.reachable and not bridge.replaying
    assert seqs(out) == [1, 2] and bridge.held == 3
    hb(bridge, ack=2)
    beat(engine, bridge, 12_000)  # the controller stays up this time
    assert seqs(out) == [1, 2, 3, 4, 5]


def test_status_replies_carry_the_counters_and_the_numbering():
    engine, bridge, out = make(capacity=4, window=2)
    for n in range(10):
        up(bridge, reading(n))
    hb(bridge)
    status = next(m for m in out if isinstance(m, StatusUp))
    assert status.controller_reachable and status.held == 4 and status.capacity == 4
    assert status.dropped_overflow == 6 and status.boot == BOOT
    assert (status.last, status.low, status.window) == (0, 1, 2)  # nothing numbered yet


# ---- the reliable uplink ----


def test_an_acknowledgement_frees_exactly_what_it_covers():
    engine, bridge, out = make()
    hb(bridge)
    for n in range(1, 6):
        up(bridge, reading(n))
    assert bridge.unacked == 5 and [m.n for m in out if isinstance(m, FrameUp)] == [1, 2, 3, 4, 5]
    bridge.receive(AckDown(BOOT, 3))
    assert bridge.unacked == 2 and [key[3] for _, key, _ in bridge.slots()] == [4, 5]
    bridge.receive(AckDown(BOOT, 2))  # older news changes nothing
    assert bridge.unacked == 2
    bridge.receive(AckDown(BOOT, 9))  # a number C never sent is ignored, not trusted
    assert bridge.unacked == 2
    bridge.receive(AckDown(BOOT + 1, 5))  # about another boot of C: ignored
    assert bridge.unacked == 2
    bridge.receive(AckDown(BOOT, 5))
    assert bridge.size == 0


def test_the_window_stops_c_running_ahead_of_a_silent_laptop_and_reopens_on_ack():
    engine, bridge, out = make(window=4)
    hb(bridge)
    out.clear()
    for n in range(1, 9):
        up(bridge, reading(n))
    assert [m.n for m in out] == [1, 2, 3, 4]  # four sent, four waiting
    assert bridge.unacked == 4 and bridge.held == 4
    bridge.receive(AckDown(BOOT, 4))
    engine.run_until(1_000)
    assert [m.n for m in out] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(m.replayed for m in out[4:])  # they had to wait, and say so with their age


def test_a_resend_request_gets_the_held_numbers_again_in_order():
    engine, bridge, out = make()
    hb(bridge)
    for n in range(1, 6):
        up(bridge, reading(n))
    out.clear()
    bridge.receive(ResendDown(BOOT, 3))
    engine.run_until(1_000)
    again = frames(out)
    assert [n for n, _, _ in again] == [3, 4, 5] and all(m.replayed for _, _, m in again)
    assert bridge.counters.retransmitted == 3
    out.clear()
    bridge.receive(ResendDown(BOOT, 3))  # asked twice (the first answer was lost): sent again
    engine.run_until(2_000)
    assert [n for n, _, _ in frames(out)] == [3, 4, 5]


def test_numbers_c_no_longer_holds_are_named_as_gone_with_their_kinds():
    engine, bridge, out = make(capacity=6, window=4, timeout=3000)
    hb(bridge)
    up(bridge, report(A))  # n=1
    up(bridge, reading(1))  # n=2
    up(bridge, reading(2))  # n=3  (the laptop, say, never saw these three)
    engine.run_until(4_000)  # no heartbeat: C notices the outage
    for n in range(3, 12):
        up(bridge, reading(n))  # fills the buffer: the numbered entries go first
    assert bridge.counters.evicted_sent == 3
    out.clear()
    hb(bridge, ack=0)
    bridge.receive(ResendDown(BOOT, 1))
    engine.run_until(5_000)
    gone = [m for m in out if isinstance(m, GoneUp)]
    assert [(g.first, g.last, g.kinds) for g in gone] == [(1, 3, "MRR")]
    assert bridge.counters.gone == 3


def test_numbers_freed_by_an_acknowledgement_are_gone_of_unknown_kind():
    engine, bridge, out = make()
    hb(bridge)
    for n in range(1, 4):
        up(bridge, reading(n))
    bridge.receive(AckDown(BOOT, 3))
    out.clear()
    bridge.receive(ResendDown(BOOT, 2))  # a confused laptop asks for what it acknowledged
    assert [(m.first, m.last, m.kinds) for m in out if isinstance(m, GoneUp)] == [(2, 3, "??")]


def test_a_reboot_forgets_everything_and_numbers_from_one_again():
    engine, bridge, out = make()
    hb(bridge)
    for n in range(1, 4):
        up(bridge, reading(n))
    bridge.power_off()
    up(bridge, reading(4))  # C is off: nothing happens
    hb(bridge)
    assert out[-1].n == 3 and bridge.size == 0
    bridge.boot_up(BOOT + 7)
    assert isinstance(out[-1], HelloUp) and out[-1].boot == BOOT + 7
    hb(bridge)
    engine.run_until(100)
    up(bridge, reading(5))
    assert out[-1].n == 1 and out[-1].boot == BOOT + 7


def test_c_builds_the_header_for_a_command_and_refuses_types_it_may_not_send():
    sent = []

    def originate(dst, msg_type, payload):
        sent.append((dst, msg_type, payload))
        return None

    engine, bridge, out = make(originate=originate)
    bridge.receive(TransmitDown(B, MessageType.ROUTE_UPDATE, b"\x01" * 11))
    assert sent == [(B, MessageType.ROUTE_UPDATE, b"\x01" * 11)]

    engine, bridge, out = make(originate=lambda dst, mt, p: "type_not_allowed")
    bridge.receive(TransmitDown(B, MessageType.DATA, b"\x00" * 4))
    assert bridge.counters.refused == 1


def test_in_normal_operation_memory_stays_at_about_one_acknowledgement_interval():
    engine, bridge, out = make()
    hb(bridge)
    for second in range(1, 31):
        engine.run_until(second * 1000)
        hb(bridge, bridge.next_n - 1)  # the laptop acknowledges what it got
        for n in range(5):
            up(bridge, reading(second * 10 + n))
    assert bridge.held == 0
    assert bridge.size <= 5  # what arrived since the last acknowledgement
