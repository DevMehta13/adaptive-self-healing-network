"""Unplug-the-active-relay trials on the real boards (Windows).

Needs the backend running in hardware mode and relay B on a laptop USB port, so that
its COM port disappearing marks the moment of failure.  Spoken cues tell the person
when to pull and replug B's USB cable.  Everything is measured from the backend's own
event record (sim_ms = laptop clock since the backend started); the harness only
adds the wall-clock moment Windows removed B's port, converted to sim_ms.

    python scripts/hardware_unplug_trials.py --b-port COM6 --trials 5 --out results.json

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


def counters(snap):
    c = snap["counts"]
    return {k: c[k]["value"] for k in ("created", "acknowledged", "lost", "no_route", "delivered", "duplicates")
            if k in c}


def wait_steady(clock, relay, timeout=120):
    """Route active through `relay` and its last 3 readings arrived through it."""
    end = time.time() + timeout
    while time.time() < end:
        snap = clock.sample()
        recent = snap["readings"]["recent"][-3:]
        if (route_via(snap, relay) and len(recent) == 3 and all(relay in (r["observed_path"] or []) for r in recent)
                and all(n["status"] == "reachable" for n in snap["nodes"])):
            return snap
        time.sleep(1)
    return None


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


def run_trial(n, clock, relay_port):
    print(f"\n=== trial {n} ===", flush=True)
    snap = wait_steady(clock, "B")
    if snap is None:
        return {"trial": n, "error": "network was not steady through B before the trial"}
    before, _ = fresh_counts(clock)
    last_id = get("/api/events?after_id=0&limit=1")["last_event_id"]
    events = []

    def pump():
        nonlocal last_id
        data = get(f"/api/events?after_id={last_id}&limit=1000")
        for e in data["events"]:
            events.append(e)
            last_id = e["event_id"]

    say("Unplug relay B now")
    t_removed = None
    t_cue = time.time()
    while time.time() - t_cue < 40:
        if relay_port not in ports():
            t_removed = time.time()
            break
        time.sleep(0.02)
    if t_removed is None:
        return {"trial": n, "error": "B's port never disappeared within 40 s"}
    print(f"  B port gone ({t_removed - t_cue:.1f} s after cue)", flush=True)
    t_rm_sim = clock.sim_ms(t_removed)

    deadline = t_removed + 25
    while time.time() < deadline:
        pump()
        clock.sample()
        time.sleep(0.25)
    pump()

    say("Plug relay B back in")
    t_cue2 = time.time()
    t_back = None
    while time.time() - t_cue2 < 40:
        if relay_port in ports():
            t_back = time.time()
            break
        time.sleep(0.05)
    if t_back is None:
        # B may re-enumerate under another COM name; accept any CP210x port that was absent.
        t_back = time.time() if len(ports()) >= 3 else None
    returned_ms = None
    end = time.time() + 120
    while time.time() < end:
        pump()
        snap = clock.sample()
        if route_via(snap, "B") and t_back and snap["route"]["version"] > 0:
            returned_ms = clock.sim_ms(time.time()) - clock.sim_ms(t_back)
            # confirm readings flow through B again
            if any("B" in (r["observed_path"] or []) for r in snap["readings"]["recent"][-2:]):
                break
        time.sleep(0.3)
    time.sleep(6)
    pump()
    after, snap = fresh_counts(clock)

    # ---- metrics (all sim_ms, relative to the moment B's port vanished) ----
    def rel(e):
        return round(e["data"]["sim_ms"] - t_rm_sim)

    post = [e for e in events if e["data"].get("sim_ms", 0) >= t_rm_sim - 200]
    lf = [e for e in post if e["type"] == "local_failover" and e["data"].get("node") == "A"]
    lf_used = [e for e in lf if e["data"].get("outcome") == "backup_in_use"]
    ra = [e for e in post if e["type"] == "route_active"]
    via_d = [e for e in ra if "D" in e["data"]["path"][1:-1]]
    back_b = [e for e in ra if "B" in e["data"]["path"][1:-1] and e["data"]["sim_ms"] > (via_d[0]["data"]["sim_ms"] if via_d else 0)]
    readings = [e["data"] for e in events if e["type"] == "reading_received"]
    first_d = next((r for r in readings if r["received_ms"] >= t_rm_sim and "D" in (r["observed_path"] or [])), None)
    window = sorted(r["received_ms"] for r in readings if t_rm_sim - 2600 <= r["received_ms"] <= (first_d["received_ms"] + 2600 if first_d else t_rm_sim + 15000))
    gap = max((b - a for a, b in zip(window, window[1:])), default=None)
    last_before = max((r["received_ms"] for r in readings if r["received_ms"] < t_rm_sim and "B" in (r["observed_path"] or [])), default=None)
    lost_pairs = {k: after.get(k, 0) - before.get(k, 0) for k in before}
    return {
        "trial": n,
        "relay_removed": "B",
        "cue_to_port_removed_s": round(t_removed - t_cue, 1),
        "last_reading_via_B_before_removal_ms": None if last_before is None else round(last_before - t_rm_sim),
        "local_failover_at_A_ms": rel(lf_used[0]) if lf_used else None,
        "local_failover_trigger": lf_used[0]["data"].get("reason") if lf_used else None,
        "local_failover_outcomes": [(rel(e), e["data"].get("outcome")) for e in lf],
        "controller_route_via_D_active_ms": rel(via_d[0]) if via_d else None,
        "controller_route_trigger": via_d[0]["data"].get("trigger") if via_d else None,
        "first_delivery_via_D_ms": round(first_d["received_ms"] - t_rm_sim) if first_d else None,
        "largest_gap_between_readings_at_C_ms": gap,
        "route_back_via_B_after_replug_ms": None if returned_ms is None else round(returned_ms),
        "route_back_via_B_event_ms_after_removal": rel(back_b[0]) if back_b else None,
        "counter_deltas_over_trial": lost_pairs,
        "created_minus_delivered": lost_pairs.get("created", 0) - lost_pairs.get("delivered", 0),
        "events_seen": len(events),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--b-port", required=True)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    clock = Clock()
    for _ in range(5):
        clock.sample()
        time.sleep(0.2)
    results = []
    for n in range(1, args.trials + 1):
        r = run_trial(n, clock, args.b_port)
        print(json.dumps(r), flush=True)
        results.append(r)
        json.dump(results, open(args.out, "w"), indent=1)
        if "error" in r:
            break
    say("Trials finished")


if __name__ == "__main__":
    main()
