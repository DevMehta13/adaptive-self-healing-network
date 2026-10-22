"""Watch the live backend from a terminal, without a dashboard.

    uv run python -m iot_backend.watch
    uv run python -m iot_backend.watch --url ws://127.0.0.1:8000/ws --no-heartbeats

It connects to /ws, prints each event on one line, and (like a dashboard must) fetches the
snapshot after connecting. Press Ctrl+C to stop. Events from the presenter's environment
channel are marked `ENV`, so "what we did" reads next to "what the network noticed".
"""

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


def _clock(ms: int) -> str:
    seconds, milli = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{milli:03d}"


def describe(envelope: dict[str, Any]) -> str:
    """One readable line for an envelope. Unknown event types still print, as raw data."""
    kind, data = envelope["type"], envelope["data"]
    when = _clock(data["sim_ms"]) if "sim_ms" in data else "        "
    tag = f"#{envelope['event_id']:<4}" if "event_id" in envelope else "     "
    if kind == "status":
        body = f"connected: mode={data['mode']} run={data.get('run_id')} (fetch the snapshot)"
    elif kind == "heartbeat":
        state = "PAUSED" if data.get("paused") else f"{data.get('speed')}x"
        body = f"heartbeat sim={_clock(data.get('sim_ms', 0))} {state}"
    elif kind == "environment":
        body = f"ENV   {data['label']}"
    elif kind == "reading_received":
        via = "->".join("?" if n is None else n for n in data["observed_path"])
        body = f"reading A#{data['seq']} value={data['value']:.2f} via {data['via']} path {via}"
        if data.get("replayed"):
            body = (
                f"REPLAYED reading A#{data['seq']} value={data['value']:.2f} via {data['via']} "
                f"path {via}  (reached C at {_clock(data['received_ms'])}, "
                f"{data['delay_ms'] / 1000:.1f} s late)"
            )
    elif kind == "telemetry":
        c = data["counters"]
        body = (
            f"telemetry {data['node']}: created={c['created']} acked={c['acknowledged']} "
            f"lost={c['lost']} no_route={c['no_route']} fwd={c['forwarded']} drop={c['dropped']}"
        )
    elif kind == "local_failover":
        to = data["to"] or "NO ROUTE"
        body = f"LOCAL FAILOVER {data['node']}: {data['from']} -> {to} ({data['reason']})"
        if data.get("replayed"):
            body += f"  (REPLAYED: reached C at {_clock(data['received_ms'])})"
    elif kind == "route_computed":
        cost = (
            f"p={data['delivery_probability']}"
            if data["cost_ms"] is None
            else f"cost={data['cost_ms']}ms"
        )
        body = (
            f"route computed v{data['version']} {'>'.join(data['path'])} "
            f"{cost} backups={data['backups']} trigger={data['trigger']} policy={data['policy']}"
        )
    elif kind == "policy_changed":
        body = f"POLICY {data['old']} -> {data['new']}"
    elif kind == "route_candidate":
        body = (
            f"better route {'>'.join(data['path'])} seen {data['evaluations']} of "
            f"{data['needed']} times ({data['policy']}; in use: {'>'.join(data['current_path'])})"
        )
    elif kind == "route_candidate_dropped":
        body = (
            f"better route {'>'.join(data['path'])} dropped after {data['evaluations']} "
            f"evaluation(s): {data['reason']}"
        )
    elif kind == "route_active":
        body = (
            f"ROUTE ACTIVE v{data['version']} {'>'.join(data['path'])} "
            f"confirmed by {','.join(data['confirmed_by'])} trigger={data['trigger']}"
        )
    elif kind == "route_installing":
        body = f"route installing v{data['version']} {data['phase']}: {','.join(data['nodes'])}"
    elif kind == "no_available_route":
        body = f"NO AVAILABLE ROUTE v{data['version']} ({data['reason']})"
    elif kind == "node_lost_contact":
        body = f"LOST CONTACT with {data['node']} (observed by {','.join(data['observed_by'])})"
    elif kind == "link_state_changed":
        body = f"link {data['link']}: {data['old']} -> {data['new']}"
    elif kind == "link_held_down":
        body = f"link {data['link']} held down {data['hold_down_ms']} ms"
    elif kind in ("node_seen", "node_regained_contact", "node_restarted", "node_stale"):
        body = f"{kind} {data['node']}"
    elif kind == "gateway_link_lost":
        body = (
            f"GATEWAY LINK LOST: nothing from C since {_clock(data['since_ms'])} "
            f"(the network view is stale from here)"
        )
    elif kind == "gateway_link_restored":
        body = f"GATEWAY LINK RESTORED after {data['lost_for_ms'] / 1000:.1f} s"
    elif kind == "replay_started":
        body = (
            f"REPLAY START: {data['entries']} entries ({data['held']} held during the outage), "
            f"{data['dropped_overflow']} dropped by overflow "
            f"({data['dropped_readings']} readings, {data['evicted_readings']} more handed up "
            f"and never acknowledged), {data['coalesced']} coalesced, "
            f"outage {data['outage_ms'] / 1000:.1f} s"
        )
    elif kind == "replay_finished":
        body = (
            f"REPLAY END: {data['replayed']} replayed, {data['dropped_overflow']} dropped by "
            f"overflow ({data['dropped_readings']} readings, {data['evicted_readings']} more "
            f"handed up and never acknowledged), {data['coalesced']} coalesced"
        )
    elif kind == "uplink_loss":
        if data["reason"] == "c_restarted" and data["count"] == 0:
            body = "C RESTARTED: whatever it held is lost (RAM only); no missing number was known"
        elif data["reason"] == "c_restarted":
            body = (
                f"C RESTARTED: whatever it held is lost (RAM only); at least {data['count']} "
                f"message(s) never arrived (numbers {data['first']}..{data['last']})"
            )
        else:
            body = (
                f"UPLINK LOSS: {data['count']} message(s) gone at C "
                f"({data['readings']} reading(s), {data['unknown']} of unknown kind), "
                f"numbers {data['first']}..{data['last']}"
            )
    elif kind == "serial_port":
        body = f"SERIAL PORT {data['port']}: {data['state']} {data['detail']}".rstrip()
    elif kind == "runtime":
        body = f"runtime {data['change']} paused={data['paused']} speed={data['speed']}"
    elif kind == "reset":
        body = f"RESET to scenario {data['scenario']} (run {data['run_id']}); refetch snapshot"
    else:
        extra = {k: v for k, v in data.items() if k != "sim_ms"}
        body = f"{kind} {json.dumps(extra, separators=(',', ':'))[:160]}"
    return f"{when}  {tag}  {body}"


def fetch_snapshot(http_url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{http_url}/api/snapshot", timeout=5) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def summarize_snapshot(snapshot: dict[str, Any]) -> str:
    nodes = " ".join(f"{n['id']}={n['status']}" for n in snapshot.get("nodes", []))
    route = snapshot.get("route", {})
    path = ">".join(route.get("path", [])) or "-"
    clock = snapshot.get("clock", {})
    gateway = snapshot.get("gateway", {})
    link = f"{gateway.get('state', '?')}"
    return (
        f"snapshot: run {snapshot.get('run_id')} scenario {snapshot.get('scenario')} "
        f"sim={_clock(clock.get('sim_ms', 0))} last_event_id={snapshot.get('last_event_id')}\n"
        f"          nodes: {nodes}\n"
        f"          route: {route.get('state')} {path} v{route.get('version')}\n"
        f"          gateway link: {link}"
    )


async def watch(url: str, http_url: str, heartbeats: bool) -> int:
    try:
        async with connect(url) as socket:
            print(f"connected to {url}")
            snapshot = await asyncio.get_running_loop().run_in_executor(
                None, fetch_snapshot, http_url
            )
            if snapshot is not None:
                print(summarize_snapshot(snapshot))
            else:
                print("(could not fetch the snapshot over HTTP)")
            async for raw in socket:
                envelope = json.loads(raw)
                if envelope["type"] == "heartbeat" and not heartbeats:
                    continue
                print(describe(envelope), flush=True)
    except (ConnectionClosed, OSError) as error:
        print(f"disconnected: {error}")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m iot_backend.watch", description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws", help="WebSocket URL")
    parser.add_argument("--http", default=None, help="HTTP base URL (default: derived from --url)")
    parser.add_argument("--no-heartbeats", action="store_true", help="hide heartbeat lines")
    args = parser.parse_args(argv)
    http_url = args.http or args.url.replace("ws://", "http://").replace("wss://", "https://")
    http_url = http_url.removesuffix("/ws")
    try:
        return asyncio.run(watch(args.url, http_url, not args.no_heartbeats))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
