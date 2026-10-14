"""The recorded demo run in tests/dashboard-fixtures/ stays valid against the current shapes."""

import json
from pathlib import Path

import pytest

from iot_backend.live import record, schema
from iot_backend.watch import describe, summarize_snapshot

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "dashboard-fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_fixture_files_are_exactly_what_the_current_code_records():
    # If a shape changed on purpose, regenerate with:  uv run python -m iot_backend.live.record
    assert record.main(["--check"]) == 0


@pytest.mark.parametrize("name", ["demo-run.snapshot-initial.json", "demo-run.snapshot-mid.json"])
def test_snapshots_match_the_documented_shape(name):
    snapshot = load(name)
    assert schema.validate_snapshot(snapshot) == []
    assert snapshot["mode"] == "simulation" and "SIMULATION" in snapshot["label"]


def test_every_recorded_message_matches_the_documented_shape_and_is_in_order():
    document = load("demo-run.events.json")
    events = document["events"]
    assert events[0]["type"] == "status" and events[0]["seq"] == 1
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    for envelope in events:
        assert schema.validate_envelope(envelope) == [], envelope
    ids = [e["event_id"] for e in events if "event_id" in e]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert all("event_id" not in e for e in events if e["type"] == "heartbeat")
    assert load("demo-run.snapshot-initial.json")["last_event_id"] == 0
    mid = load("demo-run.snapshot-mid.json")["last_event_id"]
    assert any(i > mid for i in ids) and any(i <= mid for i in ids)  # the discard rule is exercised


def test_the_recorded_story_is_the_one_we_promise():
    events = load("demo-run.events.json")["events"]
    story = [(e["type"], e["data"]) for e in events if "event_id" in e]

    def first(predicate):
        for index, (kind, data) in enumerate(story):
            if predicate(kind, data):
                return index
        raise AssertionError("event missing from the recorded run")

    normal = first(lambda k, d: k == "route_active" and d["path"] == ["A", "B", "C"])
    stop = first(lambda k, d: k == "environment" and d["action"] == "stop_node")
    failover = first(lambda k, d: k == "local_failover")
    confirmed = first(
        lambda k, d: (
            k == "route_active"
            and d["trigger"] == "local_failover_report"
            and d["path"] == ["A", "D", "C"]
        )
    )
    lost = first(lambda k, d: k == "node_lost_contact")
    start = first(lambda k, d: k == "environment" and d["action"] == "start_node")
    held = first(lambda k, d: k == "link_held_down" and d["sim_ms"] > story[start][1]["sim_ms"])
    back = first(
        lambda k, d: (
            k == "route_active"
            and d["path"] == ["A", "B", "C"]
            and d["sim_ms"] > story[start][1]["sim_ms"]
        )
    )
    assert normal < stop < failover < confirmed
    assert failover < lost  # A moved before the controller's own detection
    assert start < held < back
    # B is used again only after its links were held down
    assert story[back][1]["sim_ms"] - story[held][1]["sim_ms"] >= 4_000
    via_d = [
        d
        for k, d in story
        if k == "reading_received"
        and story[failover][1]["sim_ms"] < d["sim_ms"] < story[start][1]["sim_ms"]
    ]
    assert via_d and {d["via"] for d in via_d} == {"D"}


def test_the_watcher_can_describe_every_recorded_event():
    events = load("demo-run.events.json")["events"]
    lines = [describe(e) for e in events]
    assert len(lines) == len(events) and all(isinstance(line, str) and line for line in lines)
    joined = "\n".join(lines)
    assert "LOCAL FAILOVER A: B -> D" in joined
    assert "ENV   Stop B (injected by presenter, simulation)" in joined
    assert "ROUTE ACTIVE" in joined
    summary = summarize_snapshot(load("demo-run.snapshot-mid.json"))
    assert "route:" in summary and "nodes:" in summary


# ---- the outage run (Phase 5b) ----


@pytest.mark.parametrize(
    "name", ["outage-run.snapshot-initial.json", "outage-run.snapshot-mid.json"]
)
def test_outage_snapshots_match_the_documented_shape(name):
    snapshot = load(name)
    assert schema.validate_snapshot(snapshot) == []
    assert snapshot["gateway"]["state"] in ("connecting", "connected", "lost", "replaying")


def test_every_outage_message_matches_the_documented_shape_and_is_in_order():
    events = load("outage-run.events.json")["events"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    for envelope in events:
        assert schema.validate_envelope(envelope) == [], envelope
    ids = [e["event_id"] for e in events if "event_id" in e]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    mid = load("outage-run.snapshot-mid.json")
    assert mid["gateway"]["state"] == "lost"  # the mid snapshot is taken during the outage
    assert mid["readings"]["recent"][-1]["replayed"] is False


def test_the_recorded_outage_story_is_the_one_we_promise():
    events = load("outage-run.events.json")["events"]
    story = [(e["type"], e["data"]) for e in events if "event_id" in e]

    def first(predicate):
        for index, (kind, data) in enumerate(story):
            if predicate(kind, data):
                return index
        raise AssertionError("event missing from the recorded outage run")

    detach = first(lambda k, d: k == "environment" and d["action"] == "gateway")
    lost = first(lambda k, d: k == "gateway_link_lost")
    stop = first(lambda k, d: k == "environment" and d["action"] == "stop_node")
    attach = first(
        lambda k, d: k == "environment" and d["action"] == "gateway" and d["detail"]["attached"]
    )
    restored = first(lambda k, d: k == "gateway_link_restored")
    started = first(lambda k, d: k == "replay_started")
    finished = first(lambda k, d: k == "replay_finished")
    failover = first(lambda k, d: k == "local_failover")
    confirmed = first(lambda k, d: k == "route_active" and d["trigger"] == "local_failover_report")
    assert detach < lost < stop < attach < restored <= started < failover < finished
    assert finished < confirmed
    # nothing about B or the failover reached the controller while it was away
    assert not [k for k, d in story[lost:attach] if k in ("local_failover", "node_lost_contact")]
    # the replayed readings are marked and carry how late they are
    replayed = [d for k, d in story[started:finished] if k == "reading_received"]
    assert len(replayed) >= 10
    assert all(d["replayed"] and d["delay_ms"] > 0 for d in replayed)
    assert sorted(d["received_ms"] for d in replayed) == [d["received_ms"] for d in replayed]
    # and live readings after the replay are not
    live = [d for k, d in story[finished:] if k == "reading_received"]
    assert live and not any(d["replayed"] for d in live)
