"""CSV export of a stored run: its readings and its events.

Two files, because they have different columns. Both start with a comment-free header row and use
plain `,` separators and `\\n` line ends, so Excel, Numbers and pandas open them directly.
`value` is the reading in the sensor's unit (hundredths converted); `observed_path` shows what
node C could see (`?` for a relay it cannot name); `data` is the event's JSON exactly as the
dashboard received it.
"""

import csv
import io
import json
from collections.abc import Iterator
from pathlib import Path

from iot_backend.storage.store import RunStore

READINGS_HEADER = (
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
)
EVENTS_HEADER = ("event_id", "sim_ms", "type", "data")

KINDS = ("readings", "events")


def _lines(header: tuple[str, ...], rows: Iterator[tuple]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    yield buffer.getvalue()
    for row in rows:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(row)
        yield buffer.getvalue()


def _reading_rows(store: RunStore, run_id: int) -> Iterator[tuple]:
    for row in store.readings(run_id):
        (
            event_id,
            origin,
            session,
            seq,
            hundredths,
            via,
            hops,
            ttl,
            received,
            replayed,
            delay,
            path,
        ) = row
        yield (
            event_id,
            origin,
            session,
            seq,
            f"{hundredths / 100:.2f}",
            via,
            hops,
            ttl,
            received,
            "true" if replayed else "false",
            delay,
            path,
        )


def _event_rows(store: RunStore, run_id: int) -> Iterator[tuple]:
    for event_id, sim_ms, type_, data_json in store.events(run_id):
        yield event_id, sim_ms, type_, json.dumps(json.loads(data_json), sort_keys=True)


def csv_lines(store: RunStore, run_id: int, kind: str) -> Iterator[str]:
    """The CSV text of one kind of data from one run, a row at a time."""
    if kind == "readings":
        return _lines(READINGS_HEADER, _reading_rows(store, run_id))
    if kind == "events":
        return _lines(EVENTS_HEADER, _event_rows(store, run_id))
    raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")


def export_run(store: RunStore, run_id: int, directory: str | Path) -> list[Path]:
    """Write `run-<id>-readings.csv` and `run-<id>-events.csv`; returns the paths."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for kind in KINDS:
        target = folder / f"run-{run_id}-{kind}.csv"
        with target.open("w", encoding="utf-8", newline="") as handle:
            for line in csv_lines(store, run_id, kind):
                handle.write(line)
        written.append(target)
    return written
