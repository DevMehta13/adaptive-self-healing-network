"""The shared conformance cases for node C's bridge, and the rules they stand for.

Every case in tests/bridge-conformance/ is also run against the firmware's C++ bridge (through
tests/generated/bridge_conformance.h), so the simulator and the board cannot drift apart again.
"""

import json
import random

import pytest

from iot_backend import conformance
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
from iot_backend.transports.wire import FrameUp, GoneUp, HeartbeatDown, ReplayEndUp, ReplayStartUp

CASE_FILES = sorted(conformance.CASES_DIR.glob("*.json"))


def test_there_are_cases_and_the_files_and_header_are_current():
    assert len(CASE_FILES) >= 8
    assert conformance.main(["--check"]) == 0, "run: uv run python -m iot_backend.conformance"


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.stem)
def test_the_reference_bridge_produces_exactly_the_expected_lines(path):
    case = json.loads(path.read_text(encoding="utf-8"))
    lines, final = conformance.run_reference(case)
    assert lines == case["expect"]["emitted"]
    assert final == case["expect"]["final"]


# ---- the rules, stated directly (independent of the generated expectations) ----

A, B, C, D = 1, 2, 3, 4


def make(capacity, window=4):
    engine = Engine()
    out: list = []
    bridge = CBridge(engine, BridgeConfig(3000, capacity, 20, window), out.append)
    bridge.boot_up(0xBEEF)
    out.clear()
    return engine, bridge, out


def reading(seq):
    return Frame(A, C, 100, seq, 7, DataPayload(seq))


def report(origin, seq=1):
    return Frame(origin, C, 500 + origin, seq, 8, TopologyReportPayload((ReportEntry(B, 2, 1, 1),)))


def telemetry(origin, seq=1):
    return Frame(origin, C, 500 + origin, seq, 8, TelemetryPayload())


def failover(seq=40):
    return Frame(A, C, 100, seq, 8, LocalFailoverReportPayload(1000, 1, C, B, D, 2))


def silent(engine, bridge):
    """Controller heard once, then gone: unreachable after the timeout."""
    bridge.receive(HeartbeatDown(bridge.boot, 0))
    engine.run_until(engine.now_ms + 3500)
    assert not bridge.reachable


def test_a_reading_given_up_is_counted_as_a_reading_and_a_report_is_not():
    engine, bridge, _ = make(capacity=4, window=2)
    silent(engine, bridge)
    for seq in range(1, 5):
        bridge.from_radio(reading(seq), B)
    bridge.from_radio(report(A), B)  # newcomer worth less than every reading: dropped
    assert (bridge.counters.dropped_overflow, bridge.counters.dropped_readings) == (1, 0)
    bridge.from_radio(reading(5), B)  # now a reading must go: the oldest one
    assert (bridge.counters.dropped_overflow, bridge.counters.dropped_readings) == (2, 1)


def test_management_goes_before_readings_and_a_failover_report_is_never_evicted():
    engine, bridge, _ = make(capacity=6)
    silent(engine, bridge)
    bridge.from_radio(failover(), B)
    for node in (A, B, D):
        bridge.from_radio(report(node), B)
    for node in (A, B):
        bridge.from_radio(telemetry(node), B)
    for seq in range(1, 9):  # eight readings into a buffer that has one free slot at first
        bridge.from_radio(reading(seq), B)
    kinds = [kind for kind, _, _ in bridge.slots()]
    assert kinds.count("failover") == 1
    assert "report" not in kinds and "telemetry" not in kinds  # all given up first
    assert bridge.counters.dropped_readings == 8 - (6 - 1)  # five readings fit beside the failover
    assert bridge.counters.dropped_overflow == bridge.counters.dropped_readings + 5


def test_the_newest_report_telemetry_and_ack_per_node_is_the_one_kept():
    engine, bridge, _ = make(capacity=20)
    silent(engine, bridge)
    for seq in range(1, 6):
        bridge.from_radio(report(A, seq), B if seq % 2 else D)  # a flood arrives by both relays
        bridge.from_radio(telemetry(A, seq), D)
    assert bridge.size == 2  # one report and one telemetry for A
    assert bridge.counters.coalesced == 8
    assert bridge.counters.dropped_overflow == 0


def test_replay_markers_and_status_report_readings_lost_separately():
    engine, bridge, out = make(capacity=6)
    silent(engine, bridge)
    for seq in range(1, 9):
        bridge.from_radio(reading(seq), B)  # two more than fit
    bridge.from_radio(report(A), B)
    out.clear()
    for _ in range(6):  # the laptop returns and acknowledges what it receives
        bridge.receive(HeartbeatDown(bridge.boot, bridge.next_n - 1))
        engine.run_until(engine.now_ms + 1000)
    start = next(m for m in out if isinstance(m, ReplayStartUp))
    end = next(m for m in out if isinstance(m, ReplayEndUp))
    assert (start.dropped_overflow, start.dropped_readings) == (3, 2)
    assert (end.dropped_overflow, end.dropped_readings) == (3, 2)


@pytest.mark.parametrize("seed", range(40))
def test_no_reading_can_disappear_without_being_counted(seed):
    """Whatever mixture of readings and management traffic arrives, every reading is either
    handed up (once), or counted: given up while waiting (`dropped_readings`), or numbered,
    never acknowledged and then given up (named `R` when the laptop asks)."""
    rng = random.Random(seed)
    capacity = rng.choice([6, 12, 20, 50])
    window = min(rng.choice([2, 4, 16]), capacity - 1)
    engine, bridge, out = make(capacity, window)
    made = 0
    silent(engine, bridge)
    for _ in range(rng.randint(20, 120)):
        engine.run_until(engine.now_ms + 20 * rng.randint(1, 20))
        roll = rng.random()
        if roll < 0.55:
            made += 1
            bridge.from_radio(reading(made), rng.choice([B, D]), duplicate=False)
        elif roll < 0.75:
            bridge.from_radio(report(rng.choice([A, B, D]), rng.randint(1, 9)), rng.choice([B, D]))
        elif roll < 0.9:
            bridge.from_radio(
                telemetry(rng.choice([A, B, D]), rng.randint(1, 9)), rng.choice([B, D])
            )
        else:
            bridge.from_radio(failover(rng.randint(1, 5)), rng.choice([B, D]))
    out.clear()
    # The laptop returns and acknowledges everything it receives, asking for gaps as it goes.
    got: set[int] = set()
    for _ in range(400):
        bridge.receive(HeartbeatDown(bridge.boot, bridge.next_n - 1))
        engine.run_until(engine.now_ms + 1000)
        for message in out:
            if isinstance(message, FrameUp):
                frame = decode(message.frame)
                if frame.msg_type is MessageType.DATA:
                    got.add(frame.seq)
        out.clear()
        if bridge.held == 0 and not bridge.unacked:
            break
    assert bridge.held == 0
    # The controller was silent throughout, so nothing was numbered before the replay and every
    # reading made is either handed up exactly once or counted as given up. Nothing else.
    assert bridge.counters.evicted_sent == 0
    assert len(got) + bridge.counters.dropped_readings == made


def test_a_gone_answer_names_the_evicted_readings():
    engine, bridge, out = make(capacity=6, window=4)
    bridge.receive(HeartbeatDown(bridge.boot, 0))
    for seq in range(1, 5):
        bridge.from_radio(reading(seq), B)  # live: numbered 1..4
    engine.run_until(engine.now_ms + 4000)  # silence: unreachable
    for seq in range(5, 12):
        bridge.from_radio(reading(seq), B)  # evicts the numbered ones first
    out.clear()
    bridge.receive(HeartbeatDown(bridge.boot, 0))
    engine.run_until(engine.now_ms + 200)
    from iot_backend.transports.wire import ResendDown

    bridge.receive(ResendDown(bridge.boot, 1))
    gone = [m for m in out if isinstance(m, GoneUp)]
    assert gone and gone[0].kinds == "RRRR" and (gone[0].first, gone[0].last) == (1, 4)
    # C counts them itself too, so a laptop that never asks (a restarted controller never learns
    # the numbers) is still told that four readings were handed up and then given up unacknowledged
    assert bridge.counters.evicted_readings == 4
    status = next(m for m in out if hasattr(m, "evicted_readings") and hasattr(m, "capacity"))
    assert status.evicted_readings == 4
