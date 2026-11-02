"""Frame records: what node C handed up, shown as the packets view needs it (P1, additive).

Built from the reference vectors in tests/protocol-examples/ and tests/serial-examples/, then
checked through the gateway, the dashboard model, the snapshot and the WebSocket, in simulation
and in hardware mode (against the fake C).
"""

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from iot_backend.api.ws import FRAME_BATCH_MAX
from iot_backend.config import Settings
from iot_backend.fake_c import FakeCServer
from iot_backend.live import LiveConfig, LiveSimulation, schema
from iot_backend.live.frames import HANDED_UP_TYPES, FrameLog, frame_record
from iot_backend.main import create_app
from iot_backend.protocol import decode
from iot_backend.transports.laptop_gateway import FrameObserved
from iot_backend.transports.serial_gateway import SerialGateway
from iot_backend.transports.wire import FrameUp

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_EXAMPLES = ROOT / "tests" / "protocol-examples"
SERIAL_EXAMPLES = ROOT / "tests" / "serial-examples"
SCENARIOS = ROOT / "scenarios"
NAMES = {1: "A", 2: "B", 3: "C", 4: "D"}
HANDED_UP = {t.name for t in HANDED_UP_TYPES}


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def protocol_vectors() -> list[dict[str, Any]]:
    """The valid reference frames of the types C hands up."""
    vectors = [load(p) for p in sorted(PROTOCOL_EXAMPLES.glob("valid-*.json"))]
    return [v for v in vectors if v["frame"]["type"] in HANDED_UP]


def observed(
    frame_hex: str, n: int = 1, via: int = 2, age_ms: int = 0, replayed: bool = False, dup=False
) -> FrameObserved:
    data = bytes.fromhex(frame_hex)
    up = FrameUp(boot=1, n=n, age_ms=age_ms, replayed=replayed, via=via, frame=data, duplicate=dup)
    return FrameObserved(up, 5_000 - age_ms, decode(data))


class Clock:
    def __init__(self, now_ms: int = 0) -> None:
        self.now = now_ms

    def now_ms(self) -> int:
        return self.now

    def call_later(self, delay_ms, callback) -> None:  # the uplink never needs a timer here
        pass


def serial_line(name: str) -> bytes:
    return load(SERIAL_EXAMPLES / f"{name}.json")["line"].encode("ascii") + b"\n"


def gateway_with_records(now_ms: int = 30_000):
    gateway = SerialGateway(Clock(now_ms), lambda data: None)
    records: list[dict[str, Any]] = []
    gateway.subscribe_frames(lambda o: records.append(frame_record(o, len(records) + 1)))
    return gateway, records


# ---- records from the protocol reference vectors ----


@pytest.mark.parametrize("vector", protocol_vectors(), ids=lambda v: v["name"])
def test_a_record_names_what_the_codec_decoded_from_the_reference_bytes(vector):
    record = frame_record(observed(vector["hex"], n=7, via=2), 41)
    frame = vector["frame"]
    assert schema.validate_frame_record(record) == []
    assert record["id"] == 41 and record["n"] == 7 and record["via"] == "B"
    assert record["hex"] == vector["hex"] and record["size"] == len(vector["hex"]) // 2
    assert record["header"] == {
        "version": frame["version"],
        "type": decode(bytes.fromhex(vector["hex"])).msg_type.value,
        "type_name": frame["type"],
        "src": NAMES[frame["src"]],
        "dst": NAMES[frame["dst"]],
        "session": frame["session"],
        "seq": frame["seq"],
        "ttl": frame["ttl"],
        "payload_len": record["size"] - 14,
    }
    assert record["rejected"] is None and record["payload"] and record["summary"]
    assert record["dup"] is (False if frame["type"] == "DATA" else None)


def test_the_reference_vectors_cover_every_type_c_hands_up():
    assert {v["frame"]["type"] for v in protocol_vectors()} == HANDED_UP


def test_payload_summaries_name_the_values_in_the_payload():
    by_name = {v["name"]: v for v in protocol_vectors()}

    def record(name: str) -> dict[str, Any]:
        return frame_record(observed(by_name[name]["hex"]), 1)

    data = record("data-negative-value")
    value = by_name["data-negative-value"]["frame"]["payload"]["value"]
    assert data["payload"] == {"value_hundredths": value, "value": value / 100}
    assert str(value) in data["summary"]

    telemetry = record("telemetry-source")
    assert telemetry["payload"]["counters"] == by_name["telemetry-source"]["frame"]["payload"]
    assert "created 120" in telemetry["summary"] and "delivered" not in telemetry["summary"]

    report = record("report-probe-stats-with-rtt")
    first, second = report["payload"]["neighbors"]
    assert (first["node"], first["state"], first["rtt_ms"]) == ("B", "two_way", 22)
    assert (first["probes_ok"], first["probes_total"]) == (19, 20)
    assert (second["node"], second["rtt_ms"]) == ("D", None)
    assert report["summary"] == "2 neighbors: B two_way, D two_way"

    stale = record("route-ack-stale")
    assert stale["payload"] == {
        "epoch": 1700000000,
        "version": 9,
        "destination": "C",
        "status": "stale",
    }
    assert stale["summary"] == "route version 9 for C: stale"

    failover = record("failover-report-no-backup")
    assert failover["payload"]["from"] == "B" and failover["payload"]["to"] is None
    assert failover["payload"]["reason"] == "neighbor_lost"
    assert failover["summary"] == "failover B -> no backup (neighbor_lost)"


# ---- records from the serial reference vectors, through the real gateway ----


def test_serial_vectors_become_records_with_c_s_own_facts():
    gateway, records = gateway_with_records(now_ms=30_000)
    names = [
        "valid-up-frame-reading",
        "valid-up-frame-reading-duplicate",
        "valid-up-frame-report",
        "valid-up-frame-route-ack",
        "valid-up-frame-telemetry",
        "valid-up-frame-failover-replayed",
    ]
    for name in names:
        gateway.feed(serial_line(name))
    assert len(records) == len(names)
    for name, record in zip(names, records, strict=True):
        fields = load(SERIAL_EXAMPLES / f"{name}.json")["fields"]
        assert schema.validate_frame_record(record) == []
        assert (record["c_boot"], record["n"], record["via"]) == (
            fields["b"],
            fields["n"],
            NAMES[fields["via"]],
        )
        assert record["hex"] == fields["hex"]
        assert record["delay_ms"] == fields["age"] and record["replayed"] is bool(fields["rp"])
        assert record["received_ms"] == 30_000 - fields["age"]
        assert record["dup"] == (bool(fields["dup"]) if "dup" in fields else None)
    assert [r["header"]["type_name"] for r in records] == [
        "DATA",
        "DATA",
        "TOPOLOGY_REPORT",
        "ROUTE_INSTALL_ACK",
        "TELEMETRY",
        "LOCAL_FAILOVER_REPORT",
    ]


def test_a_repeated_reading_is_a_record_with_dup_set_and_is_counted():
    gateway, records = gateway_with_records()
    gateway.feed(serial_line("valid-up-frame-reading"))
    gateway.feed(serial_line("valid-up-frame-reading-duplicate"))
    first, repeat = records
    assert (first["dup"], repeat["dup"]) == (False, True)
    # the same reading: one identity, two records, as C saw it twice on the radio
    assert first["header"]["session"] == repeat["header"]["session"]
    assert first["header"]["seq"] == repeat["header"]["seq"]
    assert first["payload"] == repeat["payload"] and first["n"] != repeat["n"]
    log = FrameLog()
    for record in records:
        log.add(record)
    assert log.duplicates == 1 and log.by_type["DATA"] == 2


def test_a_replayed_frame_carries_how_late_it_is_and_when_c_got_it():
    gateway, records = gateway_with_records(now_ms=30_000)
    gateway.feed(serial_line("valid-up-frame-failover-replayed"))
    (record,) = records
    assert record["replayed"] is True and record["delay_ms"] == 20_500
    # when C got it, on the laptop's clock: now minus the age, as for a late reading
    assert record["received_ms"] == 9_500
    assert record["header"]["type_name"] == "LOCAL_FAILOVER_REPORT"
    assert record["payload"]["from"] == "B" and record["payload"]["to"] == "D"


def test_a_frame_the_codec_refuses_is_kept_with_its_reason_and_bytes():
    gateway, records = gateway_with_records(now_ms=10_000)
    gateway.feed(serial_line("invalid-up-bad-frame"))
    (record,) = records
    fields = load(SERIAL_EXAMPLES / "invalid-up-bad-frame.json")
    hex_text = json.loads(fields["line"].rsplit("*", 1)[0])["hex"]
    assert schema.validate_frame_record(record) == []
    assert record["rejected"] == {"reason": "bad_version"}
    assert record["header"] is None and record["payload"] is None and record["dup"] is None
    assert record["hex"] == hex_text and record["size"] == len(hex_text) // 2
    assert (record["n"], record["via"], record["replayed"]) == (12, "B", False)
    assert record["summary"] == "rejected: bad_version"
    assert gateway.lines["rejected:bad_frame"] == 1
    # the controller was never given it
    assert gateway.stats.frames == 0


def test_lines_rejected_for_other_reasons_are_not_frame_records():
    gateway, records = gateway_with_records()
    for name in ("invalid-up-bad-checksum-dropped-digit", "invalid-up-unexpected-frame"):
        gateway.feed(serial_line(name))
    assert records == []  # not a frame handed up: counted as lines only


# ---- the bounded log ----


def test_the_log_keeps_the_newest_records_and_counts_everything():
    by_name = {v["name"]: v for v in protocol_vectors()}
    log = FrameLog(capacity=3)
    plan = [
        ("data-basic", False, False),
        ("data-basic", True, False),
        ("telemetry-source", False, True),
        ("report-two-entries", False, False),
        ("data-basic", False, True),
    ]
    for index, (name, dup, replayed) in enumerate(plan, start=1):
        seen = observed(by_name[name]["hex"], n=index, replayed=replayed, dup=dup)
        log.add(frame_record(seen, index))
    bad = FrameObserved(
        FrameUp(1, 9, 0, False, 2, bytes.fromhex("02010103457c32632d0000000704dd070000")),
        1,
        None,
        "bad_version",
    )
    log.add(frame_record(bad, 6))
    section = log.describe(last_id=6, source="test")
    assert [r["id"] for r in section["recent"]] == [4, 5, 6]  # bounded: the newest three
    assert section["capacity"] == 3 and section["last_id"] == 6
    assert section["counts"] == {
        "total": 6,
        "by_type": {
            "DATA": 3,
            "TOPOLOGY_REPORT": 1,
            "ROUTE_INSTALL_ACK": 0,
            "LOCAL_FAILOVER_REPORT": 0,
            "TELEMETRY": 1,
        },
        "duplicates": 1,
        "replayed": 2,
        "rejected": 1,
        "rejected_by_reason": {"bad_version": 1},
    }
    assert schema.validate_frames(section) == []


# ---- the dashboard model: both modes, same shape ----


def simulation(transport: str = "direct") -> LiveSimulation:
    return LiveSimulation(
        LiveConfig(scenarios_dir=SCENARIOS, scenario="live-demo", transport=transport)
    )


@pytest.mark.parametrize("transport", ["direct", "serial"])
def test_simulation_snapshot_has_a_bounded_frames_section(transport):
    live = simulation(transport)
    ids: list[int] = []
    live.subscribe_frames(lambda r: ids.append(r["id"]))
    for _ in range(40):
        live.advance(10_000)
        if len(ids) > 250:
            break
    frames = live.snapshot()["frames"]
    assert schema.validate_frames(frames) == []
    assert frames["source"].startswith("simulation")
    assert frames["counts"]["total"] == len(ids) > frames["capacity"] == 200
    assert len(frames["recent"]) == 200  # memory is bounded
    assert [r["id"] for r in frames["recent"]] == ids[-200:]
    assert ids == list(range(1, len(ids) + 1)) and frames["last_id"] == ids[-1]
    counts = frames["counts"]
    assert sum(counts["by_type"].values()) == counts["total"] and counts["rejected"] == 0
    # route acknowledgements come only at the start, so they are in the counts, not the last 200
    by_type = counts["by_type"]
    for kind in ("DATA", "TOPOLOGY_REPORT", "ROUTE_INSTALL_ACK", "TELEMETRY"):
        assert by_type[kind] > 0, kind
    # records never enter the state-changing history
    assert "frames" not in {e.type for e in live.events_after(0, 10_000)[0]}


def test_a_reset_starts_the_counts_again_but_not_the_record_ids():
    live = simulation()
    live.advance(20_000)
    before = live.snapshot()["frames"]
    assert before["counts"]["total"] > 0
    live.reset("live-demo")
    after = live.snapshot()["frames"]
    assert after["counts"]["total"] == 0 and after["recent"] == []
    assert after["last_id"] == before["last_id"]  # ids never restart: a client can compare them
    live.advance(5_000)
    assert live.snapshot()["frames"]["recent"][0]["id"] == before["last_id"] + 1


def test_a_late_frame_in_simulation_is_marked_replayed_with_its_delay():
    live = simulation()
    live.advance(10_000)
    live.set_gateway(False)  # the controller is away: C holds what it would hand up
    live.advance(30_000)
    live.set_gateway(True)
    live.advance(10_000)
    late = [r for r in live.snapshot()["frames"]["recent"] if r["replayed"]]
    assert late and all(r["delay_ms"] > 0 for r in late)
    for record in late:  # when C got it is earlier than when the laptop heard it
        assert record["received_ms"] <= live.sim_ms - record["delay_ms"]
    assert live.snapshot()["frames"]["counts"]["replayed"] >= len(late)


def test_local_failover_report_is_a_record_after_stop_b():
    live = simulation()
    live.advance(10_000)
    live.stop_node("B")
    live.advance(20_000)
    failovers = [
        r
        for r in live.snapshot()["frames"]["recent"]
        if r["header"] and r["header"]["type_name"] == "LOCAL_FAILOVER_REPORT"
    ]
    assert failovers
    assert (failovers[0]["header"]["src"], failovers[0]["payload"]["from"]) == ("A", "B")


# ---- the WebSocket ----


def make_client(**overrides: Any) -> TestClient:
    settings = Settings(
        _env_file=None, live_autostart=False, heartbeat_interval_seconds=0.05, **overrides
    )
    return TestClient(create_app(settings))


def collect_frames(ws, until_id: int, limit: int = 5000) -> list[dict[str, Any]]:
    """Every `frames` message, read until one reaches `until_id`."""
    messages = []
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == "frames":
            messages.append(message)
            if message["data"]["last_id"] >= until_id:
                return messages
    raise AssertionError("the frames never arrived")


def test_frames_arrive_as_batched_non_state_changing_messages():
    with make_client() as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "status"
            client.post("/api/control/step", json={"ms": 60_000})
            snapshot = client.get("/api/snapshot").json()
            messages = collect_frames(ws, snapshot["frames"]["last_id"])
    assert snapshot["frames"]["counts"]["total"] > FRAME_BATCH_MAX  # the batching is exercised
    for message in messages:
        assert schema.validate_envelope(message) == [], message
        assert "event_id" not in message  # like a heartbeat: not state-changing
        data = message["data"]
        assert 1 <= len(data["records"]) <= FRAME_BATCH_MAX
        assert data["last_id"] == data["records"][-1]["id"] and data["dropped"] == 0
        assert data["run_id"] == snapshot["run_id"]
        for record in data["records"]:
            assert schema.validate_frame_record(record) == []
    records = [r for m in messages for r in m["data"]["records"]]
    assert [r["id"] for r in records] == list(range(1, len(records) + 1))  # no gaps, in order
    assert any(len(m["data"]["records"]) > 1 for m in messages)  # records are batched
    # the snapshot holds the same records the stream announced
    assert records[-len(snapshot["frames"]["recent"]) :] == snapshot["frames"]["recent"]
    seqs = [m["seq"] for m in messages]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_a_flood_is_bounded_and_the_loss_is_reported():
    with make_client() as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            for _ in range(20):  # one burst far larger than the waiting room
                client.post("/api/control/step", json={"ms": 600_000})
                if client.get("/api/snapshot").json()["frames"]["last_id"] > 700:
                    break
            last_id = client.get("/api/snapshot").json()["frames"]["last_id"]
            messages = collect_frames(ws, last_id)
    assert last_id > 700
    records = [r for m in messages for r in m["data"]["records"]]
    dropped = sum(m["data"]["dropped"] for m in messages)
    assert dropped > 0 and len(records) <= 500  # at most the waiting room is sent
    assert records[0]["id"] - 1 == dropped  # the ids make the gap visible
    assert all(len(m["data"]["records"]) <= FRAME_BATCH_MAX for m in messages)


# ---- hardware mode against the fake C: the same shape ----


def wait_until(check, timeout_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def shape(value: Any) -> Any:
    """The keys of a structure, with types for leaves (None leaves are allowed anywhere)."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    return None if value is None else type(value).__name__


def test_hardware_mode_has_the_same_frame_shape_as_simulation():
    fake = FakeCServer(SCENARIOS / "live-demo.json", port=0, echo=lambda text: None)
    fake.start_in_thread()
    try:
        settings = Settings(
            _env_file=None, mode="hardware", serial_port=f"socket://127.0.0.1:{fake.port}"
        )
        with TestClient(create_app(settings)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                snap = lambda: client.get("/api/snapshot").json()["frames"]  # noqa: E731
                wait_until(
                    lambda: all(snap()["counts"]["by_type"][k] for k in ("DATA", "TELEMETRY")),
                    30,
                    "readings and telemetry from the fake C",
                )
                streamed = collect_frames(ws, 1)
            hardware = snap()
    finally:
        fake.stop()
    assert schema.validate_frames(hardware) == []
    assert hardware["source"].startswith("hardware") and hardware["counts"]["rejected"] == 0
    assert streamed and "event_id" not in streamed[0]
    live = simulation()
    live.advance(30_000)
    sim = live.snapshot()["frames"]
    assert set(sim) == set(hardware) and set(sim["counts"]) == set(hardware["counts"])
    assert sim["source"].startswith("simulation")
    for kind in ("DATA", "TOPOLOGY_REPORT", "TELEMETRY"):  # the same record, field for field
        a = next(r for r in sim["recent"] if r["header"]["type_name"] == kind)
        b = next(r for r in hardware["recent"] if r["header"]["type_name"] == kind)
        assert shape(a) == shape(b), kind
