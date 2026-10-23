"""Controller-loss test on the real boards (Windows): stop the backend for N seconds, restart it,
and compare what A created with what the new backend received (live + replayed).

    python scripts/hardware_controller_loss.py --port COM10 --outage 30 --out results.json
"""
import argparse, json, os, subprocess, time, urllib.request

API = "http://localhost:8000"
get = lambda p: json.load(urllib.request.urlopen(API + p, timeout=5))


def fresh(max_age=2500, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        s = get("/api/snapshot")
        a = s["counts"]["created"].get("age_ms")
        if a is not None and a < max_age:
            return s
        time.sleep(0.4)
    return get("/api/snapshot")


def stop_backend():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|uv' -and $_.CommandLine -match '-m iot_backend' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True); ap.add_argument("--outage", type=int, default=30); ap.add_argument("--out", required=True)
    args = ap.parse_args()
    before = fresh()
    c0 = before["counts"]["created"]["value"]; d0 = before["readings"]["delivered_unique"]
    print("before: created", c0, "delivered_unique", d0, flush=True)
    t_kill = time.time(); stop_backend(); print("backend stopped", flush=True)
    time.sleep(args.outage)
    env = dict(os.environ, IOT_MODE="hardware", IOT_SERIAL_PORT=args.port)
    subprocess.Popen(["uv", "run", "python", "-m", "iot_backend"], cwd="backend", env=env,
                     stdout=open(os.environ.get("TEMP", ".") + "/loss_backend.log", "w"), stderr=subprocess.STDOUT)
    t_restart = time.time(); print("backend restarted after", round(t_restart - t_kill), "s", flush=True)
    time.sleep(25)
    while True:
        s = get("/api/snapshot")
        lr = s["gateway"].get("last_replay")
        if lr and lr.get("state") == "finished" or time.time() - t_restart > 120:
            break
        time.sleep(2)
    time.sleep(12)
    after = fresh()
    ev = get("/api/events?after_id=0&limit=1000")["events"]
    rep = [e["data"] for e in ev if e["type"] == "reading_received" and e["data"].get("replayed")]
    c1 = after["counts"]["created"]["value"]
    res = {
        "outage_s_requested": args.outage, "outage_s_actual": round(t_restart - t_kill),
        "a_created_before": c0, "a_created_after": c1, "a_created_during_and_after": c1 - c0,
        "delivered_unique_before_stop": d0, "new_backend_delivered_unique_after": after["readings"]["delivered_unique"],
        "replayed_readings": len(rep), "replayed_values_c": sorted({r["value"] for r in rep}),
        "replay_delay_ms_min_max": [min((r["delay_ms"] for r in rep), default=None), max((r["delay_ms"] for r in rep), default=None)],
        "last_replay": after["gateway"].get("last_replay"), "c_buffer": after["gateway"]["c_buffer"],
        "uplink": after["gateway"]["uplink"], "counts": {k: v.get("value") for k, v in after["counts"].items()},
        "nodes": [(n["id"], n["status"]) for n in after["nodes"]],
    }
    json.dump(res, open(args.out, "w"), indent=1, default=str)
    print(json.dumps(res, default=str)[:2500])


main()
