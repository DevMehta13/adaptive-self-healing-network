"""Record a demo run as fixture files for the dashboard.

    uv run python -m iot_backend.live.record            # rewrite tests/dashboard-fixtures/
    uv run python -m iot_backend.live.record --check    # exit 1 if the files are out of date

The run: normal operation, B is stopped, A fails over to D by itself, the controller confirms
the route through D, B is started again, its links are held down, and the controller moves the
route back to B. Everything is simulated time with a fixed seed, so the files are reproducible
byte for byte and a test keeps them valid against the current shapes.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from iot_backend.envelope import make_envelope
from iot_backend.live import LiveConfig, LiveEvent, LiveSimulation

REPO = Path(__file__).resolve().parents[4]
FIXTURE_DIR = REPO / "tests" / "dashboard-fixtures"
SCENARIOS_DIR = REPO / "scenarios"
BASE_TIME = "2026-01-01T00:00:{s:02d}.{ms:03d}Z"
HEARTBEAT_S = 2.0

# (simulated time of the action, description, what to do)
SCRIPT: list[tuple[int, str, str]] = [
    (12_000, "presenter stops node B", "stop B"),
    (32_000, "presenter starts node B", "start B"),
]
DURATION_MS = 60_000
MID_SNAPSHOT_MS = 20_000


def _timestamp(sim_ms: int) -> str:
    total_s, ms = divmod(sim_ms, 1000)
    minutes, seconds = divmod(total_s, 60)
    return f"2026-01-01T00:{minutes:02d}:{seconds:02d}.{ms:03d}Z"


def _snapshot(live: LiveSimulation) -> dict[str, Any]:
    return {
        "v": 1,
        "mode": "simulation",
        "backend": {"status": "ok", "version": "fixture"},
        "server_time": _timestamp(live.sim_ms),
        **live.snapshot(),
    }


def build() -> dict[str, dict[str, Any]]:
    """The fixture files, as {file name: JSON document}."""
    live = LiveSimulation(
        LiveConfig(scenarios_dir=SCENARIOS_DIR, scenario="live-demo", history=10_000)
    )
    envelopes: list[dict[str, Any]] = []
    seq = 0

    def add(type_: str, data: dict[str, Any], event_id: int | None, at_ms: int) -> None:
        nonlocal seq
        seq += 1
        envelopes.append(make_envelope(seq, type_, data, event_id, ts=_timestamp(at_ms)))

    def on_event(event: LiveEvent) -> None:
        add(event.type, event.data, event.event_id, event.data["sim_ms"])

    add(
        "status",
        {
            "backend": "ok",
            "mode": "simulation",
            "version": "fixture",
            "heartbeat_interval_s": HEARTBEAT_S,
            "snapshot_required": True,
            "run_id": live.run_id,
        },
        None,
        0,
    )
    initial = _snapshot(live)
    live.subscribe(on_event)
    mid: dict[str, Any] | None = None
    pending = sorted(SCRIPT)
    step_ms = int(HEARTBEAT_S * 1000)
    annotations = []
    for t in range(step_ms, DURATION_MS + 1, step_ms):
        live.advance(step_ms)
        add(
            "heartbeat",
            {
                "interval_s": HEARTBEAT_S,
                "sim_ms": live.sim_ms,
                "speed": live.speed,
                "paused": live.paused,
            },
            None,
            live.sim_ms,
        )
        while pending and pending[0][0] <= t:
            at, description, command = pending.pop(0)
            verb, node = command.split()
            getattr(live, "stop_node" if verb == "stop" else "start_node")(node)
            annotations.append({"sim_ms": live.sim_ms, "what": description})
        if mid is None and live.sim_ms >= MID_SNAPSHOT_MS:
            mid = _snapshot(live)  # a snapshot taken mid-run, to test the reconnect rule
    assert mid is not None
    return {
        "demo-run.snapshot-initial.json": initial,
        "demo-run.snapshot-mid.json": mid,
        "demo-run.events.json": {
            "description": (
                "A recorded simulated run of the live demo. Events are the WebSocket messages "
                "in order, as sent. Apply demo-run.snapshot-initial.json first, then the events; "
                "or start from demo-run.snapshot-mid.json and discard events with "
                "event_id <= its last_event_id."
            ),
            "scenario": "live-demo",
            "presenter_script": annotations,
            "events": envelopes,
        },
    }


def _dump(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m iot_backend.live.record")
    parser.add_argument("--check", action="store_true", help="only report whether files are stale")
    args = parser.parse_args(argv)
    stale = []
    for name, document in build().items():
        path = FIXTURE_DIR / name
        text = _dump(document)
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None
        if current != text:
            stale.append(name)
            if not args.check:
                FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
    if args.check:
        print("fixtures are up to date" if not stale else f"stale fixtures: {stale}")
        return 1 if stale else 0
    print(f"wrote {len(stale)} file(s) to {FIXTURE_DIR}" if stale else "fixtures already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
