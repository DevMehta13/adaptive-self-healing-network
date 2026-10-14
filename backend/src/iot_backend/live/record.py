"""Record demo runs as fixture files for the dashboard.

    uv run python -m iot_backend.live.record            # rewrite tests/dashboard-fixtures/
    uv run python -m iot_backend.live.record --check    # exit 1 if the files are out of date

Two runs are recorded, both from the `live-demo` scenario:

* **demo-run**: normal operation, B is stopped, A fails over to D by itself, the controller
  confirms the route through D, B is started again, its links are held down, and the controller
  moves the route back to B.
* **outage-run**: the controller is detached, B is stopped while it is away, A fails over to D
  on its own, the controller is reattached, C replays what it held, and the controller catches
  up (it learns of the failover from the replay and confirms the route through D).

Everything is simulated time with a fixed seed, so the files are reproducible byte for byte and a
test keeps them valid against the current shapes.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iot_backend.envelope import make_envelope
from iot_backend.live import LiveConfig, LiveEvent, LiveSimulation

REPO = Path(__file__).resolve().parents[4]
FIXTURE_DIR = REPO / "tests" / "dashboard-fixtures"
SCENARIOS_DIR = REPO / "scenarios"
HEARTBEAT_S = 2.0


@dataclass(frozen=True)
class Run:
    prefix: str
    script: tuple[tuple[int, str, str], ...]  # (simulated ms, description, command)
    duration_ms: int
    mid_ms: int  # when to take the mid-run snapshot
    description: str


RUNS = (
    Run(
        "demo-run",
        (
            (12_000, "presenter stops node B", "stop B"),
            (32_000, "presenter starts node B", "start B"),
        ),
        60_000,
        20_000,
        "A recorded simulated run of the live demo: normal operation, Stop B, local failover, "
        "the controller confirms, Start B, hold-down, back to B.",
    ),
    Run(
        "outage-run",
        (
            (12_000, "presenter detaches the gateway (the controller is away)", "detach"),
            (20_000, "presenter stops node B", "stop B"),
            (40_000, "presenter reattaches the gateway", "attach"),
        ),
        60_000,
        30_000,
        "A recorded simulated controller outage: detach, Stop B, A fails over to D alone, "
        "reattach, C replays what it held, the controller catches up and confirms the route.",
    ),
)


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


def _do(live: LiveSimulation, command: str) -> None:
    if command == "detach":
        live.set_gateway(False)
    elif command == "attach":
        live.set_gateway(True)
    else:
        verb, node = command.split()
        (live.stop_node if verb == "stop" else live.start_node)(node)


def build_run(run: Run) -> dict[str, dict[str, Any]]:
    """The fixture files of one run, as {file name: JSON document}."""
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
    pending = sorted(run.script)
    step_ms = int(HEARTBEAT_S * 1000)
    annotations = []
    for t in range(step_ms, run.duration_ms + 1, step_ms):
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
            _, description, command = pending.pop(0)
            _do(live, command)
            annotations.append({"sim_ms": live.sim_ms, "what": description})
        if mid is None and live.sim_ms >= run.mid_ms:
            mid = _snapshot(live)  # a snapshot taken mid-run, to test the reconnect rule
    assert mid is not None
    return {
        f"{run.prefix}.snapshot-initial.json": initial,
        f"{run.prefix}.snapshot-mid.json": mid,
        f"{run.prefix}.events.json": {
            "description": (
                f"{run.description} Events are the WebSocket messages in order, as sent. Apply "
                f"{run.prefix}.snapshot-initial.json first, then the events; or start from "
                f"{run.prefix}.snapshot-mid.json and discard events with "
                "event_id <= its last_event_id."
            ),
            "scenario": "live-demo",
            "presenter_script": annotations,
            "events": envelopes,
        },
    }


def build() -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for run in RUNS:
        files.update(build_run(run))
    return files


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
