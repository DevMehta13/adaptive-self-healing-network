"""Phase 10: the SQLite run history, its bounds, CSV export, the API and the command line."""

import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from iot_backend.config import Settings
from iot_backend.history import main as history_main
from iot_backend.live.hardware import HardwareConfig, LiveHardware
from iot_backend.main import create_app
from iot_backend.storage import HistoryError, RunStore, StoreLimits, csv_lines, export_run

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


def reading(seq, via="B", path=("A", "B", "C"), replayed=False):
    return {
        "origin": "A",
        "session": 7,
        "seq": seq,
        "value_hundredths": 2150 + seq,
        "via": via,
        "hops": len(path) - 1,
        "ttl": 7,
        "observed_path": list(path),
        "received_ms": seq * 2000,
        "replayed": replayed,
        "delay_ms": 0 if not replayed else 4500,
    }


# ---- the store ----


def test_a_run_is_stored_with_its_mode_scenario_and_configuration(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3")
    run = store.start_run("simulation", "live-demo", 1, {"seed": 7, "controller": {"policy": "x"}})
    store.add_event(run, 1, 100, "node_seen", {"sim_ms": 100, "node": "A"})
    store.add_reading(run, 2, reading(1))
    detail = store.run(run)
    assert detail["mode"] == "simulation" and detail["scenario"] == "live-demo"
    assert detail["config"] == {"seed": 7, "controller": {"policy": "x"}}
    assert detail["events"] == 1 and detail["readings"] == 1
    assert detail["ended_utc"] is None
    store.end_run(run)
    assert store.run(run)["ended_utc"] is not None
    store.close()


def test_readings_and_events_come_back_in_order_with_their_fields(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3")
    run = store.start_run("simulation", "s", 1, {})
    for i in (3, 1, 2):
        store.add_reading(run, i, reading(i))
    store.add_event(run, 9, 5, "link_state_changed", {"sim_ms": 5, "link": "A-B"})
    rows = list(store.readings(run))
    assert [r[0] for r in rows] == [1, 2, 3]  # by event id
    assert rows[0][1:4] == ("A", 7, 1) and rows[0][-1] == "A>B>C"
    assert list(store.events(run))[0][:3] == (9, 5, "link_state_changed")
    store.close()


def test_a_relay_the_gateway_cannot_name_is_stored_as_a_question_mark(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3")
    run = store.start_run("hardware", None, 1, {})
    store.add_reading(run, 1, reading(1, path=("A", None, "B", "C")))
    assert list(store.readings(run))[0][-1] == "A>?>B>C"
    store.close()


def test_an_unknown_run_is_an_error(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3")
    with pytest.raises(HistoryError):
        store.run(99)
    with pytest.raises(HistoryError):
        list(store.readings(99))
    store.close()


def test_the_history_survives_closing_and_reopening(tmp_path):
    path = tmp_path / "h.sqlite3"
    store = RunStore(path)
    run = store.start_run("simulation", "s", 1, {"a": 1})
    store.add_reading(run, 1, reading(1))
    store.close()
    again = RunStore(path)
    assert again.run(run)["readings"] == 1
    assert again.start_run("simulation", "s", 1, {}) == run + 1  # ids keep counting
    again.close()


def test_a_file_that_is_not_a_history_or_has_another_format_is_refused(tmp_path):
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"this is not a database at all" * 20)
    with pytest.raises(HistoryError):
        RunStore(junk)
    other = tmp_path / "other.sqlite3"
    db = sqlite3.connect(other)
    db.execute("PRAGMA user_version = 99")
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    with pytest.raises(HistoryError, match="format 99"):
        RunStore(other)


def test_limits_must_be_positive():
    with pytest.raises(ValueError):
        StoreLimits(max_runs=0)


# ---- the bounds ----


def test_only_the_newest_runs_are_kept_and_an_old_runs_rows_go_with_it(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3", StoreLimits(max_runs=3))
    ids = []
    for i in range(5):
        run = store.start_run("simulation", f"s{i}", i, {})
        store.add_event(run, 1, 0, "node_seen", {"sim_ms": 0})
        store.add_reading(run, 2, reading(1))
        ids.append(run)
    store.flush()
    assert [r["run_id"] for r in store.runs()] == ids[:1:-1]  # newest first: 5, 4, 3
    for gone in ids[:2]:
        with pytest.raises(HistoryError):
            store.run(gone)
    db = sqlite3.connect(tmp_path / "h.sqlite3")
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 3
    db.close()
    store.close()


def test_a_run_keeps_only_its_newest_events_and_readings_and_says_it_was_cut(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3", StoreLimits(max_events=50, max_readings=20))
    run = store.start_run("simulation", "s", 1, {})
    for i in range(1, 1001):
        store.add_event(run, i, i, "telemetry", {"sim_ms": i})
        store.add_reading(run, 2000 + i, reading(i))
    detail = store.run(run)
    assert detail["events"] == 50 and detail["readings"] == 20
    assert detail["events_evicted"] == 950 and detail["readings_evicted"] == 980
    assert detail["complete"] is False
    assert [r[0] for r in store.events(run)] == list(range(951, 1001))  # the newest 50
    assert [r[3] for r in store.readings(run)] == list(range(981, 1001))
    store.close()


def test_a_run_within_its_limits_is_complete(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3")
    run = store.start_run("simulation", "s", 1, {})
    store.add_event(run, 1, 0, "node_seen", {"sim_ms": 0})
    assert store.run(run)["complete"] is True
    store.close()


def test_the_file_stays_small_however_long_the_run(tmp_path):
    path = tmp_path / "h.sqlite3"
    store = RunStore(path, StoreLimits(max_runs=2, max_events=500, max_readings=500))
    for _ in range(3):
        run = store.start_run("simulation", "s", 1, {})
        for i in range(1, 20_001):
            store.add_event(run, i, i, "telemetry", {"sim_ms": i, "note": "x" * 60})
    store.close()
    assert path.stat().st_size < 2_000_000  # 60 000 rows were written; at most 1 000 are held


# ---- CSV ----


def parse(lines):
    return list(csv.reader(io.StringIO("".join(lines))))


def test_readings_csv_has_a_header_values_in_sensor_units_and_one_row_per_reading(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3")
    run = store.start_run("simulation", "s", 1, {})
    store.add_reading(run, 1, reading(1))
    store.add_reading(run, 2, reading(2, via="D", path=("A", "D", "C"), replayed=True))
    rows = parse(csv_lines(store, run, "readings"))
    assert rows[0] == [
        "event_id",
        "origin",
        "session",
        "seq",
        "value",
        "via",
        "hops",
        "ttl",
        "received_ms",
        "replayed",
        "delay_ms",
        "observed_path",
    ]
    assert rows[1] == ["1", "A", "7", "1", "21.51", "B", "2", "7", "2000", "false", "0", "A>B>C"]
    assert rows[2][5] == "D" and rows[2][9] == "true" and rows[2][10] == "4500"
    assert len(rows) == 3
    store.close()


def test_events_csv_carries_each_events_data_as_json(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3")
    run = store.start_run("simulation", "s", 1, {})
    store.add_event(run, 4, 250, "route_active", {"sim_ms": 250, "path": ["A", "B", "C"]})
    rows = parse(csv_lines(store, run, "events"))
    assert rows[0] == ["event_id", "sim_ms", "type", "data"]
    assert rows[1][:3] == ["4", "250", "route_active"]
    assert json.loads(rows[1][3]) == {"path": ["A", "B", "C"], "sim_ms": 250}
    store.close()


def test_export_writes_both_files(tmp_path):
    store = RunStore(tmp_path / "h.sqlite3")
    run = store.start_run("simulation", "s", 1, {})
    store.add_reading(run, 1, reading(1))
    paths = export_run(store, run, tmp_path / "out" / "deeper")
    assert [p.name for p in paths] == [f"run-{run}-readings.csv", f"run-{run}-events.csv"]
    assert all(p.exists() and p.read_text().startswith("event_id") for p in paths)
    with pytest.raises(ValueError):
        csv_lines(store, run, "everything")
    store.close()


# ---- through the application ----


def make_client(tmp_path, **overrides) -> TestClient:
    settings = Settings(
        _env_file=None,
        live_autostart=False,
        heartbeat_interval_seconds=0.05,
        history_db=str(tmp_path / "history.sqlite3"),
        **overrides,
    )
    return TestClient(create_app(settings))


def step(client, ms):
    assert client.post("/api/control/step", json={"ms": ms}).status_code == 200


def test_a_live_run_is_recorded_exactly_as_the_dashboard_saw_it(tmp_path):
    with make_client(tmp_path) as client:
        step(client, 60_000)
        snap = client.get("/api/snapshot").json()
        events = client.get("/api/events?after_id=0&limit=1000").json()["events"]
        runs = client.get("/api/runs").json()["runs"]
        assert len(runs) == 1
        run = client.get(f"/api/runs/{runs[0]['run_id']}").json()
        assert run["mode"] == "simulation" and run["scenario"] == "live-demo"
        assert run["ended_utc"] is None  # still running
        assert run["readings"] == snap["readings"]["delivered_unique"] > 0
        non_reading = [e for e in events if e["type"] != "reading_received"]
        assert run["events"] == len(non_reading)
        assert run["complete"] is True


def test_the_stored_configuration_is_enough_to_repeat_the_run(tmp_path):
    with make_client(tmp_path, routing_policy="lowest_delay") as client:
        step(client, 1_000)
        run = client.get("/api/runs/1").json()
        assert run["config"]["scenario"] == "live-demo"
        assert run["config"]["scenario_file"]["seed"] == 7  # the scenario file as it was
        assert run["config"]["controller"]["policy"] == "lowest_delay"
        assert run["config"]["controller"]["backup_next_hops"] is True


def test_a_reset_starts_a_new_run_and_closes_the_old_one(tmp_path):
    with make_client(tmp_path) as client:
        step(client, 20_000)
        first_events = client.get("/api/runs/1").json()["events"]
        assert (
            client.post("/api/control/reset", json={"scenario": "normal-via-b"}).status_code == 200
        )
        step(client, 10_000)
        runs = client.get("/api/runs").json()["runs"]
        assert [r["run_id"] for r in runs] == [2, 1]
        first, second = runs[1], runs[0]
        assert first["ended_utc"] is not None and second["ended_utc"] is None
        assert second["scenario"] == "normal-via-b"
        assert first["events"] == first_events  # nothing from the new run leaked into it
        assert second["live_run_id"] == first["live_run_id"] + 1


def test_the_run_is_closed_and_kept_when_the_backend_stops(tmp_path):
    with make_client(tmp_path) as client:
        step(client, 15_000)
    store = RunStore(tmp_path / "history.sqlite3")
    run = store.run(1)
    assert run["ended_utc"] is not None and run["events"] > 0
    store.close()
    with make_client(tmp_path) as client:  # a second start of the backend
        step(client, 5_000)
        runs = client.get("/api/runs").json()["runs"]
        assert [r["run_id"] for r in runs] == [2, 1]
        assert runs[1]["ended_utc"] is not None


def test_readings_export_matches_what_the_dashboard_counted(tmp_path):
    with make_client(tmp_path) as client:
        step(client, 40_000)
        delivered = client.get("/api/snapshot").json()["readings"]["delivered_unique"]
        response = client.get("/api/runs/1/export?kind=readings")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "run-1-readings.csv" in response.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(response.text)))
        assert len(rows) - 1 == delivered
        assert {row[5] for row in rows[1:]} <= {"B", "D"}  # via
        assert response.text.count("\r") == 0  # plain \n line ends


def test_events_export_lists_every_state_change_with_its_json(tmp_path):
    with make_client(tmp_path) as client:
        step(client, 20_000)
        response = client.get("/api/runs/1/export?kind=events")
        rows = list(csv.reader(io.StringIO(response.text)))
        types = {row[2] for row in rows[1:]}
        assert {"route_active", "telemetry"} <= types
        assert "reading_received" not in types  # readings have their own file
        for row in rows[1:]:
            assert json.loads(row[3])["sim_ms"] == int(row[1])


def test_export_and_run_errors_are_structured(tmp_path):
    with make_client(tmp_path) as client:
        step(client, 1_000)
        bad = client.get("/api/runs/1/export?kind=everything")
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_request"
        missing = client.get("/api/runs/99/export?kind=readings")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "unknown_run"
        assert client.get("/api/runs/99").status_code == 404


def test_without_a_history_file_the_history_endpoints_say_so_and_nothing_is_written(tmp_path):
    settings = Settings(_env_file=None, live_autostart=False, heartbeat_interval_seconds=0.05)
    with TestClient(create_app(settings)) as client:
        step(client, 5_000)
        for url in ("/api/runs", "/api/runs/1", "/api/runs/1/export"):
            response = client.get(url)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "history_disabled"
    assert not list(tmp_path.iterdir())


def test_the_history_limits_come_from_the_settings(tmp_path):
    with make_client(tmp_path, history_max_runs=2, history_max_events=30) as client:
        step(client, 30_000)
        limits = client.get("/api/runs").json()["limits"]
        assert limits["max_runs"] == 2 and limits["max_events"] == 30
        run = client.get("/api/runs/1").json()
        assert run["events"] == 30 and run["events_evicted"] > 0 and run["complete"] is False


def test_recording_does_not_change_what_the_dashboard_is_sent(tmp_path):
    def stream(client):
        step(client, 30_000)
        return client.get("/api/events?after_id=0&limit=1000").json()["events"]

    plain = Settings(_env_file=None, live_autostart=False, heartbeat_interval_seconds=0.05)
    with TestClient(create_app(plain)) as client:
        without = stream(client)
    with make_client(tmp_path) as client:
        with_history = stream(client)
    assert without == with_history


def test_hardware_mode_records_its_port_and_controller_settings(tmp_path):
    live = LiveHardware(
        HardwareConfig(controller_scenario=SCENARIOS / "live-demo.json", port="COM7", baud=115200)
    )
    assert live.run_config["port"] == "COM7" and live.run_config["baud"] == 115200
    assert live.run_config["controller"]["heartbeat_interval_ms"] == 1000
    assert live.mode == "hardware"


# ---- the command line ----


def seeded_db(tmp_path):
    path = tmp_path / "cli.sqlite3"
    store = RunStore(path)
    run = store.start_run("simulation", "live-demo", 1, {"seed": 1})
    store.add_reading(run, 1, reading(1))
    store.add_event(run, 2, 10, "node_seen", {"sim_ms": 10})
    store.end_run(run)
    store.close()
    return path


def test_cli_lists_runs(tmp_path, capsys):
    path = seeded_db(tmp_path)
    assert history_main(["--db", str(path), "list"]) == 0
    out = capsys.readouterr().out
    assert "run   1" in out and "live-demo" in out and "1 events, 1 readings" in out


def test_cli_exports_both_csv_files(tmp_path, capsys):
    path = seeded_db(tmp_path)
    out_dir = tmp_path / "exports"
    assert history_main(["--db", str(path), "export", "1", "--out-dir", str(out_dir)]) == 0
    assert (out_dir / "run-1-readings.csv").read_text().splitlines()[1].startswith("1,A,7,1,21.51")
    assert "node_seen" in (out_dir / "run-1-events.csv").read_text()
    assert "wrote" in capsys.readouterr().out


def test_cli_reports_a_missing_database_and_a_missing_run(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("IOT_HISTORY_DB", raising=False)
    assert history_main(["list"]) == 2
    assert "IOT_HISTORY_DB" in capsys.readouterr().err
    path = seeded_db(tmp_path)
    assert history_main(["--db", str(path), "export", "42", "--out-dir", str(tmp_path)]) == 1
    assert "no run 42" in capsys.readouterr().err
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"not a database" * 50)
    assert history_main(["--db", str(junk), "list"]) == 2
