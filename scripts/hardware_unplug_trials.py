"""Unplug-the-active-relay trials on the real boards (Windows).

Needs the backend running in hardware mode and both relays (B and D) on laptop USB ports,
so that a relay's COM port disappearing marks the moment of failure.  Before each trial the
harness reads the active route from the backend and asks for whichever relay (B or D)
carries it to be unplugged; the relay pulled is recorded per trial.  Spoken cues tell the
person when to pull and replug that cable.  Everything is measured from the backend's own
event record (sim_ms = laptop clock since the backend started); the harness only adds the
wall-clock moment Windows removed the port, converted to sim_ms.  After the replug it does
not wait for the route to return to the relay that was pulled: it waits for the port, lets
the network settle, and the next trial starts from whatever route is then active.

    python scripts/hardware_unplug_trials.py --b-port COM6 --d-port COM7 --trials 5 --out r.json

Windows' detach latency is not measured, so detection times may be slightly understated.
"""
import argparse
import json
import statistics
import subprocess
import time
import urllib.request

from serial.tools import list_ports

API = "http://localhost:8000"
RELAYS = ("B", "D")


def get(path):
    return json.load(urllib.request.urlopen(API + path, timeout=5))


def ports():
    return {p.device for p in list_ports.comports() if p.vid == 0x10C4}


def say(text):
    print(f"[cue] {text}", flush=True)
    subprocess.Popen(["powershell", "-NoProfile", "-Command",
                      "Add-Type -AssemblyName System.Speech;"
                      f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')"])


class Clock:
    """Maps wall time to the backend's sim_ms using snapshot samples."""

    def __init__(self):
        self.offsets = []

    def sample(self):
        t0 = time.time()
        snap = get("/api/snapshot")
        t1 = time.time()
        self.offsets.append((t0 + t1) / 2 - snap["clock"]["sim_ms"] / 1000)
        self.offsets = self.offsets[-15:]
        return snap

    def sim_ms(self, wall):
        return (wall - statistics.median(self.offsets)) * 1000


def route_via(snap, node):
    r = snap["route"]
    return r["kind"] == "route" and r["state"] == "active" and node in r["path"][1:-1]


def active_relay(snap):
    """The relay (B or D) on the active route, or None if the route uses neither or both."""
    on_route = [n for n in RELAYS if route_via(snap, n)]
    return on_route[0] if len(on_route) == 1 else None


def other_relay(relay):
    return "D" if relay == "B" else "B"


def counters(snap):
    c = snap["counts"]
    return {k: c[k]["value"] for k in ("created", "acknowledged", "lost", "no_route", "delivered", "duplicates")
            if k in c}


def wait_steady(clock, timeout=120):
    """Route active through one relay, its last 3 readings arrived through it, all nodes reachable.

    Returns (snapshot, relay), or (None, None) on timeout.
    """
    end = time.time() + timeout
    while time.time() < end:
        snap = clock.sample()
        relay = active_relay(snap)
        recent = snap["readings"]["recent"][-3:]
        if (relay and len(recent) == 3 and all(relay in (r["observed_path"] or []) for r in recent)
                and all(n["status"] == "reachable" for n in snap["nodes"])):
            return snap, relay
        time.sleep(1)
    return None, None


def fresh_counts(clock, max_age=2500, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        snap = clock.sample()
        age = snap["counts"]["created"].get("age_ms")
        if age is not None and age < max_age:
            return counters(snap), snap
        time.sleep(0.4)
    snap = clock.sample()
    return counters(snap), snap


def run_trial(n, clock, relay_ports):
    print(f"\n=== trial {n} ===", flush=True)
    snap, pulled = wait_steady(clock)
    if snap is None:
        return {"trial": n, "error": "network was not steady through B or D before the trial"}
    other = other_relay(pulled)
    relay_port = relay_ports[pulled]
    route_before = snap["route"]["path"]
    ports_before = len(ports())
    print(f"  active route {route_before}: pulling relay {pulled} ({relay_port})", flush=True)
    before, _ = fresh_counts(clock)
    last_id = get("/api/events?after_id=0&limit=1")["last_event_id"]
    events = []

    def pump():
        nonlocal last_id
        data = get(f"/api/events?after_id={last_id}&limit=1000")
        for e in data["events"]:
            events.append(e)
            last_id = e["event_id"]

    say(f"Unplug relay {pulled} now")
    t_removed = None
    t_cue = time.time()
    while time.time() - t_cue < 40:
        if relay_port not in ports():
            t_removed = time.time()
            break
        time.sleep(0.02)
    if t_removed is None:
        return {"trial": n, "relay_removed": pulled,
                "error": f"{pulled}'s port never disappeared within 40 s"}
    print(f"  {pulled} port gone ({t_removed - t_cue:.1f} s after cue)", flush=True)
    t_rm_sim = clock.sim_ms(t_removed)

    deadline = t_removed + 25
    while time.time() < deadline:
        pump()
        clock.sample()
        time.sleep(0.25)
    pump()

    say(f"Plug relay {pulled} back in")
    t_cue2 = time.time()
    t_back = None
    while time.time() - t_cue2 < 40:
        if relay_port in ports():
            t_back = time.time()
            break
        time.sleep(0.05)
    if t_back is None:
        # The relay may re-enumerate under another COM name; accept the port count being restored.
        t_back = time.time() if len(ports()) >= ports_before else None
    # Do not wait for the route to return to the pulled relay: only let the network settle.
    end = time.time() + 10
    while time.time() < end:
        pump()
        clock.sample()
        time.sleep(0.3)
    time.sleep(6)
    pump()
    after, snap = fresh_counts(clock)

    # ---- metrics (all sim_ms, relative to the moment the pulled relay's port vanished) ----
    def rel(e):
        return round(e["data"]["sim_ms"] - t_rm_sim)

    post = [e for e in events if e["data"].get("sim_ms", 0) >= t_rm_sim - 200]
    lf = [e for e in post if e["type"] == "local_failover" and e["data"].get("node") == "A"]
    lf_used = [e for e in lf if e["data"].get("outcome") == "backup_in_use"]
    ra = [e for e in post if e["type"] == "route_active"]
    via_other = [e for e in ra if other in e["data"]["path"][1:-1]]
    first_other_ms = via_other[0]["data"]["sim_ms"] if via_other else 0
    back_pulled = [e for e in ra if pulled in e["data"]["path"][1:-1]
                   and e["data"]["sim_ms"] > first_other_ms]
    readings = [e["data"] for e in events if e["type"] == "reading_received"]
    first_other = next((r for r in readings if r["received_ms"] >= t_rm_sim
                        and other in (r["observed_path"] or [])), None)
    window = sorted(r["received_ms"] for r in readings
                    if t_rm_sim - 2600 <= r["received_ms"]
                    <= (first_other["received_ms"] + 2600 if first_other else t_rm_sim + 15000))
    gap = max((b - a for a, b in zip(window, window[1:])), default=None)
    last_before = max((r["received_ms"] for r in readings
                       if r["received_ms"] < t_rm_sim and pulled in (r["observed_path"] or [])),
                      default=None)
    deltas = {k: after.get(k, 0) - before.get(k, 0) for k in before}
    return {
        "trial": n,
        "relay_removed": pulled,
        "backup_relay": other,
        "route_before": route_before,
        "port_replugged": t_back is not None,
        "cue_to_port_removed_s": round(t_removed - t_cue, 1),
        "last_reading_via_pulled_relay_before_removal_ms":
            None if last_before is None else round(last_before - t_rm_sim),
        "local_failover_at_A_ms": rel(lf_used[0]) if lf_used else None,
        "local_failover_trigger": lf_used[0]["data"].get("reason") if lf_used else None,
        "local_failover_outcomes": [(rel(e), e["data"].get("outcome")) for e in lf],
        "controller_route_via_backup_active_ms": rel(via_other[0]) if via_other else None,
        "controller_route_trigger": via_other[0]["data"].get("trigger") if via_other else None,
        "first_delivery_via_backup_ms":
            round(first_other["received_ms"] - t_rm_sim) if first_other else None,
        "largest_gap_between_readings_at_C_ms": gap,
        # Passive observation only; the harness does not wait for the route to return.
        "route_via_pulled_relay_again_event_ms_after_removal":
            rel(back_pulled[0]) if back_pulled else None,
        "counter_deltas_over_trial": deltas,
        "created_minus_delivered": deltas.get("created", 0) - deltas.get("delivered", 0),
        "events_seen": len(events),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--b-port", required=True, help="COM port of relay B")
    ap.add_argument("--d-port", required=True, help="COM port of relay D (must be on USB data)")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    relay_ports = {"B": args.b_port, "D": args.d_port}
    clock = Clock()
    for _ in range(5):
        clock.sample()
        time.sleep(0.2)
    results = []
    for n in range(1, args.trials + 1):
        r = run_trial(n, clock, relay_ports)
        print(json.dumps(r), flush=True)
        results.append(r)
        json.dump(results, open(args.out, "w"), indent=1)
        if "error" in r:
            break
    say("Trials finished")


if __name__ == "__main__":
    main()
