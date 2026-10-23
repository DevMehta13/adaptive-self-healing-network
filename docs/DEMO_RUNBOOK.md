# Demo runbook (Windows, four boards)

One page for the live demo. It describes procedures and what to say; the results it refers to are recorded in [STATUS.md](STATUS.md) and [results/](results/) (one session each, not guarantees). This runbook itself has not been rehearsed on hardware. Provisioning and recovery detail: [FOUR_BOARD_LAB_PACKAGE.md](../firmware/FOUR_BOARD_LAB_PACKAGE.md).

## Equipment and power

- Four ESP32-S boards flashed `f3c_a_dht11` (A), `f3c_b`, `f3c_c`, `f3c_d`, each already holding its two peers (`peers_restored=2`). A has the DHT11 on GPIO4 (`S` to P4, `+` to 3V3, `-` to GND).
- **A, B, C: each on its own direct laptop USB port. D: a 5 V wall adapter.** No USB hubs, no thin or charge-only cables. Why: boards entered brownout reset loops ("Brownout detector was triggered") on shared or weak ports when the radio transmitted.
- Only C needs a USB data link to the laptop for the run. B must be on USB so you can pull its cable.

## Power-up order

1. Laptop on, nothing else running that opens COM ports (no serial monitors, no old backend).
2. Plug in D (adapter), then A, then B, then C. Wait about 10 s.
3. `pio device list` (or Device Manager) and note C's COM port. Below it is `COM10`; use yours.

## Commands (three PowerShell windows)

```powershell
# 1 Backend, hardware mode, on C's port (from backend/)
$env:IOT_MODE = "hardware"; $env:IOT_SERIAL_PORT = "COM10"; uv run python -m iot_backend

# 2 Dashboard (from dashboard/; first time only: npm ci)
npm run dev          # open the printed http://localhost:5173 URL

# 3 Watcher, the controller's own event record (from backend/)
uv run python -m iot_backend.watch --no-heartbeats
```

Wait until the dashboard shows mode **hardware**, all four nodes reachable and a confirmed route.

## Demo script

1. **Normal operation.** Show the four-board diamond and the active route. Say: the laptop computes the route; the boards only forward. Warm the DHT11 with a finger or breath: the temperature and sparkline rise. Say: these are real readings from A, acknowledged end to end at A, not a radio callback. Check which relay carries the route (the controller picks the lowest measured delay, so it may be D, not B); use whichever it is below.
2. **Failure and local failover.** Say what you will do, then unplug the USB cable of the relay in use (B needs its cable; for D, pull the wall adapter). Show: A switches to its installed backup within about a second (watcher `local_failover`, outcome `backup_in_use`), then the controller confirms the new route, and readings keep arriving with at most a short gap. Say: A did not calculate a route; it used a backup the controller had already installed. Previous trials: median about 0.5 s, worst case 12 s when the backup link had just recovered; do not promise a recovery time.
3. **Replug.** Plug the relay back. Say: the route returns to it only if the policy prefers it; under `immediate_rtt` it may stay on the other relay, and that is correct behaviour.
4. **Controller loss and replay.** Stop the backend (Ctrl+C in window 1), wait about 30 s, say: C keeps the network running and holds what it would have sent up, in a bounded buffer. Restart the backend with the same command. Show the replay: readings labelled replayed with their delay, and the lost-at-C counters at zero. Say: bounded, not a guarantee; we measured no uncounted readings on 8 to 90 s outages.
5. **Be honest.** Detection times here come from Windows' port removal and are approximate; removal of A, C or D was not tested.

## Troubleshooting

- **Board not listed:** install the Silicon Labs CP210x driver, try another cable (data-capable) and another port.
- **Reset loop or repeated "Brownout detector" in a monitor:** power. Move to a direct laptop port, a better cable, or the wall adapter.
- **Dashboard "disconnected" or no hardware:** the backend must be running and holding C's port exclusively; close any serial monitor.
- **DHT shows NaN or no readings:** check wiring (3V3, GPIO4, GND; never VIN/5V), then the module (one dead DHT11 was found in the lab).
- **A board lost its peers or a board was replaced:** `python tools/provision.py --only X` (backend stopped), or the manual steps in the lab package.

## Fallback: simulation demo (no hardware)

```powershell
# window 1, from backend/ (simulation is the default mode)
Remove-Item Env:IOT_MODE -ErrorAction SilentlyContinue; uv run python -m iot_backend
# window 2, from dashboard/
npm run dev
```

The dashboard shows mode **simulation** with presenter controls: stop B (failover to D), start B, detach and attach C (controller loss and replay), pause, reset. Say clearly that this is a simulation and not radio evidence. To show the hardware-mode screen without boards: `uv run python -m iot_backend.fake_c` in one window, then `$env:IOT_MODE = "hardware"; $env:IOT_SERIAL_PORT = "socket://127.0.0.1:7777"; uv run python -m iot_backend`, labelled "fake C".
