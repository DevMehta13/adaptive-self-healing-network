"""SQLite run history: one row per run, and its events and readings, bounded in size.

What is stored, per run: the run's id, its mode (simulation or hardware), its configuration
(the scenario file as it was, the controller settings, or the serial port), every
state-changing event the dashboard was shown (except readings, which have their own table), and
every reading the controller counted. It is a record of what the *controller knew*, in the form
the dashboard saw it; it is not the simulator's ground truth.

Bounds (all of them, so the file cannot grow without limit):

* at most `max_runs` runs; starting another deletes the oldest whole run;
* at most `max_events` events and `max_readings` readings per run; past that the oldest rows are
  deleted and counted in the run (`events_evicted`, `readings_evicted`), so a truncated history
  says so instead of looking complete.

Rows are buffered in memory and written in batches (`flush_every` rows, and whenever the history
is read or closed). A crash can therefore lose the last few rows of the running run; it cannot
corrupt the database (WAL journal).
"""

import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iot_backend.envelope import utc_now

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    scenario TEXT,
    live_run_id INTEGER NOT NULL,
    started_utc TEXT NOT NULL,
    ended_utc TEXT,
    config_json TEXT NOT NULL,
    events_evicted INTEGER NOT NULL DEFAULT 0,
    readings_evicted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE events (
    run_id INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL,
    sim_ms INTEGER NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
CREATE TABLE readings (
    run_id INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL,
    origin TEXT NOT NULL,
    session INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    value_hundredths INTEGER NOT NULL,
    via TEXT NOT NULL,
    hops INTEGER NOT NULL,
    ttl INTEGER NOT NULL,
    received_ms INTEGER NOT NULL,
    replayed INTEGER NOT NULL,
    delay_ms INTEGER NOT NULL,
    observed_path TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
"""

READING_COLUMNS = (
    "event_id",
    "origin",
    "session",
    "seq",
    "value_hundredths",
    "via",
    "hops",
    "ttl",
    "received_ms",
    "replayed",
    "delay_ms",
    "observed_path",
)


class HistoryError(Exception):
    """The history cannot do what was asked (no such run, unreadable file)."""


@dataclass(frozen=True)
class StoreLimits:
    max_runs: int = 10
    max_events: int = 20_000
    max_readings: int = 10_000
    flush_every: int = 100

    def __post_init__(self) -> None:
        if min(self.max_runs, self.max_events, self.max_readings, self.flush_every) < 1:
            raise ValueError("history limits must be at least 1")


class RunStore:
    def __init__(self, path: str | Path, limits: StoreLimits | None = None) -> None:
        self.path = str(path)
        self.limits = limits or StoreLimits()
        self._lock = threading.RLock()
        try:
            self._db = sqlite3.connect(self.path, check_same_thread=False)
        except sqlite3.Error as error:
            raise HistoryError(f"cannot open the history database {self.path}: {error}") from error
        self._db.execute("PRAGMA foreign_keys = ON")
        try:
            self._init_schema()
        except sqlite3.Error as error:
            raise HistoryError(f"{self.path} is not a usable history database: {error}") from error
        self._events: list[tuple[Any, ...]] = []
        self._readings: list[tuple[Any, ...]] = []

    # ---- setup ----

    def _init_schema(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._db.execute("PRAGMA journal_mode = WAL")
                self._db.execute("PRAGMA synchronous = NORMAL")
                with self._db:
                    self._db.executescript(_SCHEMA)
                    self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise HistoryError(
                    f"{self.path} has history format {version}; this backend writes "
                    f"{SCHEMA_VERSION}. Move the file aside or delete it."
                )

    def close(self) -> None:
        with self._lock:
            self.flush()
            self._db.close()

    # ---- writing ----

    def start_run(
        self, mode: str, scenario: str | None, live_run_id: int, config: dict[str, Any]
    ) -> int:
        """Begin a run and return its id. The oldest runs beyond `max_runs` are deleted."""
        with self._lock:
            self.flush()
            with self._db:
                cursor = self._db.execute(
                    "INSERT INTO runs (mode, scenario, live_run_id, started_utc, config_json)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (mode, scenario, live_run_id, utc_now(), json.dumps(config, sort_keys=True)),
                )
                run_id = int(cursor.lastrowid)  # type: ignore[arg-type]
                stale = self._db.execute(
                    "SELECT run_id FROM runs ORDER BY run_id DESC LIMIT -1 OFFSET ?",
                    (self.limits.max_runs,),
                ).fetchall()
                for (old,) in stale:
                    self._db.execute("DELETE FROM runs WHERE run_id = ?", (old,))
            return run_id

    def end_run(self, run_id: int) -> None:
        with self._lock:
            self.flush()
            with self._db:
                self._db.execute(
                    "UPDATE runs SET ended_utc = ? WHERE run_id = ? AND ended_utc IS NULL",
                    (utc_now(), run_id),
                )

    def add_event(self, run_id: int, event_id: int, sim_ms: int, type_: str, data: dict) -> None:
        with self._lock:
            self._events.append((run_id, event_id, sim_ms, type_, json.dumps(data, sort_keys=True)))
            if len(self._events) + len(self._readings) >= self.limits.flush_every:
                self.flush()

    def add_reading(self, run_id: int, event_id: int, data: dict[str, Any]) -> None:
        path = ">".join("?" if hop is None else hop for hop in data["observed_path"])
        with self._lock:
            self._readings.append(
                (
                    run_id,
                    event_id,
                    data["origin"],
                    data["session"],
                    data["seq"],
                    data["value_hundredths"],
                    data["via"],
                    data["hops"],
                    data["ttl"],
                    data["received_ms"],
                    int(bool(data["replayed"])),
                    data["delay_ms"],
                    path,
                )
            )
            if len(self._events) + len(self._readings) >= self.limits.flush_every:
                self.flush()

    def flush(self) -> None:
        """Write what is buffered, then enforce the per-run bounds."""
        with self._lock:
            if not self._events and not self._readings:
                return
            events, readings = self._events, self._readings
            self._events, self._readings = [], []
            with self._db:
                self._db.executemany("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?)", events)
                self._db.executemany(
                    "INSERT OR REPLACE INTO readings VALUES (" + ", ".join("?" * 13) + ")",
                    readings,
                )
                for run_id in {row[0] for row in events} | {row[0] for row in readings}:
                    self._enforce_bounds(run_id)

    def _enforce_bounds(self, run_id: int) -> None:
        for table, column, limit in (
            ("events", "events_evicted", self.limits.max_events),
            ("readings", "readings_evicted", self.limits.max_readings),
        ):
            held = self._db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            extra = held - limit
            if extra > 0:
                self._db.execute(
                    f"DELETE FROM {table} WHERE run_id = ? AND event_id IN ("
                    f"SELECT event_id FROM {table} WHERE run_id = ? ORDER BY event_id LIMIT ?)",
                    (run_id, run_id, extra),
                )
                self._db.execute(
                    f"UPDATE runs SET {column} = {column} + ? WHERE run_id = ?", (extra, run_id)
                )

    # ---- reading ----

    def runs(self) -> list[dict[str, Any]]:
        with self._lock:
            self.flush()
            rows = self._db.execute(
                "SELECT r.run_id, r.mode, r.scenario, r.live_run_id, r.started_utc, r.ended_utc,"
                " r.events_evicted, r.readings_evicted,"
                " (SELECT COUNT(*) FROM events e WHERE e.run_id = r.run_id),"
                " (SELECT COUNT(*) FROM readings g WHERE g.run_id = r.run_id)"
                " FROM runs r ORDER BY r.run_id DESC"
            ).fetchall()
        return [
            {
                "run_id": run_id,
                "mode": mode,
                "scenario": scenario,
                "live_run_id": live_run_id,
                "started_utc": started,
                "ended_utc": ended,
                "events": events,
                "readings": readings,
                "events_evicted": events_evicted,
                "readings_evicted": readings_evicted,
                "complete": events_evicted == 0 and readings_evicted == 0,
            }
            for (
                run_id,
                mode,
                scenario,
                live_run_id,
                started,
                ended,
                events_evicted,
                readings_evicted,
                events,
                readings,
            ) in rows
        ]

    def run(self, run_id: int) -> dict[str, Any]:
        with self._lock:
            for entry in self.runs():
                if entry["run_id"] == run_id:
                    row = self._db.execute(
                        "SELECT config_json FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    return {**entry, "config": json.loads(row[0])}
        raise HistoryError(f"no run {run_id} in the history")

    def readings(self, run_id: int) -> Iterator[tuple[Any, ...]]:
        self.run(run_id)  # raises if there is no such run; also flushes
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(READING_COLUMNS)} FROM readings WHERE run_id = ?"
                " ORDER BY event_id",
                (run_id,),
            ).fetchall()
        yield from rows

    def events(self, run_id: int) -> Iterator[tuple[Any, ...]]:
        self.run(run_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, sim_ms, type, data_json FROM events WHERE run_id = ?"
                " ORDER BY event_id",
                (run_id,),
            ).fetchall()
        yield from rows
