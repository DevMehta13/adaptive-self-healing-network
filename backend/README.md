# Backend

Laptop-side backend. It contains:

- a FastAPI app that runs the simulation **live** and serves it to the dashboard: a snapshot, a WebSocket event stream, and presenter controls that change the simulated environment (Phase 5a);
- the node **protocol** module: binary frame encoding and validated decoding;
- a deterministic **simulator** with four virtual nodes (A, B, C, D): manual or controller-installed routing, forwarding, retries, duplicate detection, HELLO discovery, link probes, bounded flooding and failures;
- a **gateway interface** (`transports/`) and a **controller** (`controller/`): a topology view and, since Phase 4a, Dijkstra route calculation with versioned route installation and automatic rerouting. The controller sees the network only through events that reached node C and acts on it only through commands that node C turns into frames.

Primary routes are calculated and installed automatically (Phase 4a), together with backup next hops that nodes switch to on their own when the primary fails (Phase 4b, local failover). The controller sends node C a heartbeat; if it stops, C keeps the network running and holds what it would have sent up in a bounded buffer, replayed when the controller returns (Phase 5b). Adaptive policies, a serial gateway and a database do not exist yet.

Contracts (both drafts, pending Dev's review): [docs/DASHBOARD_API.md](../docs/DASHBOARD_API.md) and [docs/PROTOCOL.md](../docs/PROTOCOL.md).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs the right Python and the locked dependencies for you). Python 3.11 or newer.

| | Windows (PowerShell) | macOS |
| --- | --- | --- |
| Install uv | `winget install --id=astral-sh.uv -e` or `pip install uv` | `brew install uv` or `pip install uv` |
| Check | `uv --version` | `uv --version` |

## Install

From the repository root:

```sh
cd backend
uv sync
```

This creates `backend/.venv` and installs the exact versions pinned in `uv.lock`. Commands are the same on Windows and macOS.

## Run the live demo

The server starts the simulated network and paces it against the wall clock. The same commands work in PowerShell (Windows) and in a macOS terminal; run them inside `backend/`.

```sh
uv run python -m iot_backend
```

In a second terminal, watch it without a dashboard:

```sh
uv run python -m iot_backend.watch --no-heartbeats
```

Then do things to the simulated network (a presenter's controls). With PowerShell use `curl.exe` (plain `curl` is an alias for something else) and double the inner quotes, or use `Invoke-RestMethod`; with a macOS terminal use `curl` as written:

```sh
# macOS / Git Bash
curl -X POST http://127.0.0.1:8000/api/control/node -H "Content-Type: application/json" -d '{"node":"B","action":"stop"}'
curl -X POST http://127.0.0.1:8000/api/control/node -H "Content-Type: application/json" -d '{"node":"B","action":"start"}'
```

```powershell
# Windows PowerShell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/control/node -ContentType application/json -Body '{"node":"B","action":"stop"}'
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/control/node -ContentType application/json -Body '{"node":"B","action":"start"}'
```

What you will see in the watcher, in order: `ENV Stop B (injected by presenter, simulation)`; `LOCAL FAILOVER A: B -> D` about one second later; `route computed ... trigger=local_failover_report` and `ROUTE ACTIVE ... A>D>C`; a couple of seconds after that `LOST CONTACT with B` (the controller's own detection, later than the node's); readings now `via D`. After `Start B`: the links come back, are held down for 5 s, and then the route moves back to B. All times are simulated time; add `IOT_LIVE_SPEED=10` to run ten times faster.

Other controls (full list in [docs/DASHBOARD_API.md](../docs/DASHBOARD_API.md)):

| Request | Body |
| --- | --- |
| `POST /api/control/link` | `{"link":"A->B","down":true}` (one direction) |
| `POST /api/control/link-config` | `{"link":"B->C","loss":0.2,"delay_ms":80}` |
| `POST /api/control/gateway` | `{"attached":false}` (the controller outage: C holds, then replays on `true`) |
| `POST /api/control/pause`, `/resume` | `{}` |
| `POST /api/control/speed` | `{"factor":5}` |
| `POST /api/control/reset` | `{"scenario":"b-failure-auto-reroute"}` (any file in `scenarios/`) |

### The controller outage demo (Phase 5b)

With the server and the watcher running as above, take the controller away from node C, break something while it is away, and bring it back:

```sh
# macOS / Git Bash
curl -X POST http://127.0.0.1:8000/api/control/gateway -H "Content-Type: application/json" -d '{"attached":false}'
curl -X POST http://127.0.0.1:8000/api/control/node -H "Content-Type: application/json" -d '{"node":"B","action":"stop"}'
# wait 20 seconds or so
curl -X POST http://127.0.0.1:8000/api/control/gateway -H "Content-Type: application/json" -d '{"attached":true}'
```

```powershell
# Windows PowerShell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/control/gateway -ContentType application/json -Body '{"attached":false}'
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/control/node -ContentType application/json -Body '{"node":"B","action":"stop"}'
# wait 20 seconds or so
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/control/gateway -ContentType application/json -Body '{"attached":true}'
```

What the watcher prints, in order: `ENV Detach the gateway`; about 3 s later `GATEWAY LINK LOST: nothing from C since ...` (the controller's own timer; the network view is stale from here); `ENV Stop B` and then **nothing** from the network, because nothing reaches the laptop; after reattaching, `GATEWAY LINK RESTORED after ... s` and `REPLAY START: N entries ...`; a burst of `REPLAYED reading ... (reached C at ..., X s late)`, first via B and then via D; `LOCAL FAILOVER A: B -> D ... (REPLAYED: reached C at ...)`, which happened about half a second after the stop although the controller learns of it only now; `REPLAY END: N replayed, 0 dropped by overflow, ...`; and `ROUTE ACTIVE v2 A>D>C ... trigger=local_failover_report`, the controller confirming what A did alone. `GET /api/snapshot` shows the link state and C's buffer figures under `gateway`. Detach for more than about 100 s (2 s readings, 50-entry buffer) and the replay starts reporting readings dropped by overflow: those are lost, and counted.

**On hardware, test controller loss by stopping the backend (Ctrl+C), never by unplugging C's USB cable.** The cable also powers C, and C's buffer is in RAM: unplugging it removes C and everything it held.

`GET /api/snapshot` is the full current state; `GET /api/events?after_id=N` the recent history; `GET /health` a liveness check. Open <http://127.0.0.1:8000/docs> for the generated reference. Stop with Ctrl+C.

A recorded run of this demo, as JSON, is in `tests/dashboard-fixtures/` so the dashboard can be built without the backend. Regenerate it after changing a shape with `uv run python -m iot_backend.live.record`.

Optional configuration: copy `.env.example` to `.env` (`Copy-Item .env.example .env` on Windows, `cp .env.example .env` on macOS) and edit it, or set `IOT_*` environment variables. Defaults work without any file.

## Run the tests and linter

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Run the simulator

The same commands work in PowerShell and in a macOS terminal (run them inside `backend/`).

Run a ready-made scenario from the top-level `scenarios/` folder (note the `../`):

```sh
uv run python -m iot_backend.simulator ../scenarios/normal-via-b.json
```

or with no file for the default (A sends to C through B for 20 simulated seconds):

```sh
uv run python -m iot_backend.simulator --seed 42 --duration 20 --drop A-B:3:DATA
```

The output has up to six parts: a time-ordered **trace** of events, the **observed path** of every reading (rebuilt only from "frame received" evidence, never from the routing tables), a **summary** with a drops-by-reason table, and, when discovery is on, the **controller view** with topology events and simulated detection times. In `routing: "controller"` scenarios it also prints the **routes held by the nodes**, the controller's **route events**, and a **recovery measurement**. HELLO and PROBE frames are hidden from the trace unless you add `--verbose`. Simulated time passes instantly; nothing waits for real seconds.

| Scenario file | What it shows |
| --- | --- |
| `normal-via-b.json` | Readings A > B > C, ACKs C > B > A. |
| `manual-switch-to-d.json` | At 9 s an operator edits A's and C's routes to use D; later readings go A > D > C. Nothing is detected automatically. |
| `b-stopped-no-route-change.json` | B loses power at 7 s and nothing reroutes: readings are retried 3 times and reported *lost*; D is never used (there is no healing yet). |
| `ack-loss-with-retry.json` | An ACK is lost; A re-sends the same identity; C counts the reading once and acknowledges again. |
| `routing-loop-misconfiguration.json` | B's route to C wrongly points back to A; TTL ends the loop. |
| `direct-bypass-attempt.json` | A is misconfigured to send straight to C; C rejects it because A is not a permitted neighbor. |
| `b-loss-detected.json` | B stops at 8 s. A and C time it out after 3 s, flood reports to C (A's through D), and the controller declares **lost contact** (about 2.5 s after the stop). Readings via B are still lost. |
| `b-return-detected.json` | B returns at 14 s with a new session; the controller regains contact, flags the restart, and the links return to two-way. |
| `one-way-link.json` | Only A→B fails. The A–B link is shown *one-way* and unusable; no node is declared lost. |
| `report-survives-relay-loss.json` | After B stops, A's reports still reach C through D. |
| `flooding-is-bounded.json` | An 8-entry flood cache and faster reports: each node still forwards a frame at most once and the run stays small. |
| `stale-report-expiry.json` | C stops at 10 s; with no reports arriving the view turns *stale* after 15 s instead of staying fresh. |

The six Phase 2 files set `discovery: false` and `ack_delivery: "routed"` so they keep demonstrating the data plane alone; the Phase 3 files use the defaults (discovery on, ACKs flooded) and still use manual routes.

**Phase 4a scenarios** use `"routing": "controller"`: nodes start with **no** data routes, probe their links every second, and the controller installs and changes routes by itself. Their link delays are *simulated settings* (A-B and B-C 10 ms one way, A-D and D-C 25 ms one way) so that B is cheaper than D; every description says so. They are not measurements.

| Scenario file | What it shows |
| --- | --- |
| `auto-initial-route.json` | Readings created before the first route are dropped `no_route` and counted. After discovery and probing, the controller installs A > B > C (B first, then A) with nobody editing a table. |
| `b-failure-auto-reroute.json` | B stops at 12 s. A and C time it out, reports reach C through D, and the controller installs A > D > C: D first, then A. |
| `b-returns.json` | B stops (10 s) and returns (20 s). Its links come back but are held down for 5 s; then the controller picks B again because the simulated B links are cheaper. |
| `one-way-a-b.json` | Only A to B fails while B stays alive. The link is *one-way*, so it is not usable, and the controller routes via D. |
| `both-relays-fail.json` | B then D stop. The controller reports **No available route**. A cannot be told (nothing can leave C), so the announcement is retried a bounded number of times and ends *unconfirmed*; it stops by itself. |
| `no-route-told-explicitly.json` | Every link loses one direction, so no link is two-way, while management messages still go around the ring. The controller says No available route, tells A, and A drops readings with reason `no_route`. The route returns after the links return and the hold-down. |
| `route-update-lost.json` | The first ROUTE_UPDATE is dropped on both of C's links. The controller retries; the route is not active until the retry is acknowledged. |
| `route-ack-lost.json` | B installs the route but its acknowledgement is lost. The controller resends, B recognises the repeat and acknowledges again without installing twice. |
| `stale-update-ignored.json` | After the reroute two late, stale updates (an older version, and a bigger version of an older epoch) reach A. A ignores both and keeps the newer route. |
| `controller-restart.json` | The controller restarts with a new epoch; its version numbers start again at 1, yet nodes accept them and delivery never stops. |

**Phase 4b scenarios** add `"local_failover": true` at the nodes and `"backup_next_hops": true` at the controller:

| Scenario file | What it shows |
| --- | --- |
| `local-failover-b-stops.json` | The controller installs A > B > C with D as A's backup (D first). B stops; A switches to D by itself, *before* the controller's new route reaches it; the controller confirms A > D > C. |
| `local-failover-one-way-a-b.json` | Only A to B fails. A still hears B, so a timeout never fires; A fails over because B is no longer two-way (T1) or sends to B fail (T2). |
| `failover-without-controller.json` | The controller is detached (sees and sends nothing), then B stops. A still moves to D and C keeps accepting and acknowledging readings. On reattach the controller learns of the failover and catches up. |
| `backup-also-dead.json` | B and D stop. A enters no_route locally, drops readings with a reason, and its report is bounded (it cannot leave A: A is cut off). |
| `backup-unusable-but-reportable.json` | B stops and A hears nothing from D. A has no usable backup, enters no_route, and its report still reaches C through D. |
| `no-false-failover.json` | Two consecutive link-layer failures happen three times with successes between them: K - 1 failures must not trigger. |
| `b-returns-after-failover.json` | B returns. Nothing switches back by itself; the controller restores B only after the hold-down, by policy. |

**Phase 5b scenarios** turn on the controller heartbeat (`controller.heartbeat_interval_ms` 1000, `link_timeout_ms` 3000) and C's watching of it (`settings.controller_timeout_ms` 3000, `buffer_capacity`, `replay_spacing_ms`). The routing output then also shows the gateway link and C's buffer counters:

| Scenario file | What it shows |
| --- | --- |
| `controller-outage-replay.json` | The controller is away from 15 s to 35 s. C keeps accepting and acknowledging readings, holds them, and replays them in order on return; the controller counts each once and labels them delayed. |
| `outage-with-failover.json` | Away from 15 s; B stops at 20 s and A fails over to D alone; back at 40 s. The replay carries the failover report and the controller confirms A > D > C. |
| `buffer-overflow.json` | A 20-entry buffer, readings every 200 ms and a 60 s outage: the oldest readings are dropped and counted; the replay says how many. |
| `coalescing.json` | Reports and telemetry every 500 ms during a 20 s outage: each node's reports occupy one slot, so readings keep the room. |
| `stale-replay.json` | Reports only every 30 s and a 45 s outage: replayed reports are older than the 15 s expiry, so they arrive already stale and cannot make a link look fresh. |
| `heartbeat-timeout.json` | Only the timing: C declares the controller unreachable 3 s after the last heartbeat, the laptop declares the link lost 3 s after hearing nothing, and both recover on return. |

Measure readings delivered late versus lost for several outage lengths (simulation only):

```sh
uv run python -m iot_backend.simulator ../scenarios/controller-outage-replay.json --outage-study 10
uv run python -m iot_backend.simulator ../scenarios/controller-outage-replay.json --outage-study 10 --interval-ms 500 --buffer 50
```

Columns: *made* (readings accepted at C during the outage), *late* (reached the controller by replay, once), *lost* (never reached it), *unexpl.* (lost readings C's overflow counter does not account for; should be 0, and is not for outages too short for C to notice), *dup* (counted twice; must be 0), and the median and maximum delay.

Compare the three ways of handling a failure, and measure false triggers (simulation only):

```sh
uv run python -m iot_backend.simulator ../scenarios/local-failover-b-stops.json --compare-modes 50
uv run python -m iot_backend.simulator ../scenarios/local-failover-one-way-a-b.json --compare-modes 50
uv run python -m iot_backend.simulator ../scenarios/no-false-failover.json --false-triggers 30 --duration 120
```

Measure recovery over many seeds (simulation only; the reference is the scripted failure time):

```sh
uv run python -m iot_backend.simulator ../scenarios/b-failure-auto-reroute.json --measure-seeds 50
```

Command-line options (also usable on top of a scenario file):

| Option | Meaning |
| --- | --- |
| `--seed N` | Random seed. The same seed always gives the same output. |
| `--duration S` | Simulated seconds to run. |
| `--drop A-B:3` | Drop the 3rd frame offered to the A→B link (HELLOs count). `B-C:2,4` drops several; `A-B:3:DATA` counts only DATA frames (also `DELIVERY_ACK`, `TOPOLOGY_REPORT`, `HELLO`). Repeatable. Any pair of A–D works. |
| `--loss A-B:0.2` | Randomly lose 20 % of frames on a link (seeded, so repeatable). |
| `--delay-ms N` | One-way delay on every link (default 20). |
| `--interval-ms N` | Time between readings (default 2000). |
| `--ack-timeout-ms N` | How long A waits for an ACK per attempt (default 1000). |
| `--restart-a S` | Power-cycle A at second S: new session, sequence numbers start again. |
| `--malformed-at S` | Deliver a corrupted 3-byte frame from A to B at second S. |
| `--no-trace` | Print only the observed paths, summary and controller view. |
| `--verbose` | Also show HELLO and PROBE frames in the trace. |
| `--compare-modes N` | Replay a scenario's scripted failure with seeds 1..N under three modes (controller-only, local failover T1, T1 + T2) and print when A's table switched, when it first sent that way, the delivery gap, readings lost and when the controller's route was confirmed. |
| `--false-triggers N` | No failure at all: count local failovers under random A-B loss (0-20 %) for K = 2, 3, 4 over seeds 1..N (use `--duration`). |
| `--measure-seeds N` | Run a controller scenario with seeds 1..N and print min / median / max of failure detection, route active, distribution, service recovery, delivery gap and readings lost, plus route computation time (wall-clock CPU, reported separately). Prints no trace. |
| `--outage-study N` | Detach the controller for outages of 5 to 160 s over seeds 1..N and print readings made, delivered late, lost, unexplained and doubled, with delays. Use with `--interval-ms` and `--buffer`. |
| `--buffer N` | C's buffer capacity for `--outage-study`. |

Scenario files are strict JSON (an unknown key is an error, so a typo cannot silently change an experiment). They can set `seed`, `duration_s`, `settings` (interval, timeout, `max_attempts`, `ttl`, `queue_capacity`, `service_ms`, `discovery`, `ack_delivery`, `hello_interval_ms`, `hello_jitter_ms`, `neighbor_timeout_ms`, `report_interval_ms`, `report_coalesce_ms`, `flood_cache_size`, `probe_interval_ms`, `probe_jitter_ms`, `rtt_window`, `rtt_min_samples`, `rtt_max_age_ms`, `max_routes`, `local_failover`, `failover_t1`, `failover_t2_k`, `send_fail_ms`, `failover_report_repeats`, `controller_timeout_ms`, `buffer_capacity`, `replay_spacing_ms`), `routing` (`"manual"` or `"controller"`), `controller` (`epoch`, `hold_down_ms`, `install_timeout_ms`, `install_max_attempts`, `replan_delay_ms`, `max_failed_plans`, `no_route_grace_ms`, `initial_settle_ms`, `backup_next_hops`, `heartbeat_interval_ms`, `link_timeout_ms`), `permitted`, `routes` (manual routing only), `links` (`delay_ms`, `loss`, `drop_frames`, `drop_msg`, `down`), `initial_seq`, and timed `actions`: `stop_node`, `start_node`, `restart_node`, `set_route`, `set_permitted`, `link_down`, `set_link`, `inject_frame`, `restart_controller`, `detach_gateway`, `attach_gateway`.

Event names in the trace: `frame_sent` (a node created and transmitted a frame), `frame_forwarded` (a relay passed one on), `frame_received`, `frame_rejected`, `frame_dropped`, `frame_dropped_by_link`, `reading_delivered`, `duplicate_received`, `ack_received`, `ack_ignored`, `ack_timeout` (one attempt), `reading_lost`, `neighbor_changed`, `neighbor_restarted`, `node_booted`, `node_stopped`, `route_update_sent` (C originated an instruction), `route_installed` (a node applied it), `route_update_ignored` (duplicate, stale or refused), `send_failed` (the link layer said the next hop's radio did not acknowledge; shown with `--verbose`), `local_failover`, `local_no_route`, `frame_rerouted`, `gateway_detached`, `gateway_attached`. The `delivery_time_ms` on `ack_received` is the time from the *first attempt* to the ACK (time to confirmed delivery), together with `attempts`; it is not an RTT. A frame appears as `A:3905a56d#1`: creator, session id in hex, sequence number.

## Layout

```text
backend/
  pyproject.toml       project metadata, dependencies, ruff and pytest settings
  uv.lock              exact dependency versions (commit changes to this file)
  .env.example         documented configuration defaults
  src/iot_backend/
    config.py          settings from IOT_* environment variables
    envelope.py        the versioned WebSocket message envelope
    main.py            app factory and server entry point
    api/               dashboard-facing endpoints (routes.py, control.py, ws.py)
    live/              runtime.py (the paced live simulation and what the dashboard is
                       shown), schema.py (event and snapshot shapes),
                       record.py (writes tests/dashboard-fixtures/)
    watch.py           terminal watcher: python -m iot_backend.watch
    protocol/          frame constants, message classes, encode()/decode()
    simulator/         engine.py (virtual clock), link.py, nodes.py (forwarding, retries),
                       dedup.py (duplicate filter), trace.py, scenario.py (Simulation),
                       scenario_file.py (JSON loader), gateway.py (SimulatedGateway),
                       bridge.py (node C's heartbeat watching, buffer and replay),
                       measurements.py (detection and recovery times), studies.py
                       (mode comparison, false triggers, outage study), __main__.py (CLI)
    transports/        the Gateway interface and the typed laptop <-> C messages
    controller/        topology.py: nodes and links from reports, with freshness and RTT;
                       routing.py: Dijkstra, versioned installation, hold-down, no-route;
                       gateway_session.py: heartbeat and link monitor;
                       readings.py: counts each reading once
  tests/               pytest checks (test_protocol.py loads ../tests/protocol-examples/,
                       test_scenarios.py loads ../scenarios/)
```

`src/iot_backend/` is the Python package from plan.md's `backend/` tree. `controller/`, `transports/` and `storage/` will be added inside it by the phases that need them. Backend tests live in `backend/tests/` so the backend can be tested on its own; the shared protocol examples that the firmware will also use live at the repository top level, in `tests/protocol-examples/`.

## The live simulation in plain language (Phase 5a)

**How a discrete-event simulation becomes a live one.** The simulator never looks at a clock: it has a to-do list of future events and a virtual clock that jumps to the next one. To make it live, a small task wakes every 50 ms, measures how much real time passed, multiplies that by the speed factor (1x is real time) and says "run until your virtual clock has moved by that much". The simulation inside is untouched, so it is exactly as repeatable as before: only *when* slices are requested depends on the wall clock. Pause just stops asking. Tests do not sleep: they use `POST /api/control/step` to ask for slices directly.

**Why the dashboard shows the controller's view and not the simulator's truth.** The simulator knows B was switched off at 12.000 s. A real controller would not: it finds out when neighbors stop hearing B, a timeout expires, and reports travel to C. If the dashboard drew the truth, B would turn red instantly, every detection delay would look like zero, and nothing on screen would be evidence of what the system actually knew. So the backend keeps three parts apart: the environment (ground truth), the gateway on C (the only way out of it), and the controller. The dashboard model reads only from the gateway and the controller. After you press "Stop B" the snapshot still says B is reachable, and `presenter.conditions.stopped_nodes` says B is stopped: those are two different statements by two different parties, and the gap between them is the detection time.

**What C can actually observe about a packet's path.** Not much, and it is enough. When a frame reaches C, the radio tells C which neighbor sent it (on ESP-NOW, that neighbor's MAC address) and the frame still carries its remaining TTL. So C knows "this reading came from A, was handed to me by D, and has made two transmissions". It does not know what happened before D. The dashboard's observed path is `A, D, C` for two hops, and `A, ?, D, C` (with an unknown relay) for three. This is the same evidence the real C will have, and it is deliberately different from the controller's *planned* route: after a local failover the plan can still say B while readings arrive via D.

**Why presenter actions go on a separate channel.** "We stopped B" is not something the network knows, and it is not something the controller knows. But a viewer wants to see it: the story is "we did X; A noticed after 1 s; the controller confirmed after 2.5 s". Putting the action in its own `environment` channel keeps that story readable without pretending the system had that knowledge. Every control also goes only to the environment: none of them calls the controller, so the controller has to discover each effect itself, as it must on hardware.

**Why some counts are labelled with an age.** C knows how many readings it received. Only A knows how many it created and how many were never acknowledged. A sends those counters in a small TELEMETRY message every 5 s, flooded to C like other management traffic, so "pending" on the dashboard is as old as the last message. The snapshot says who knows each number and how old it is.

## How the live updates work

A **WebSocket** is a long-lived two-way connection between the browser and the backend. With normal HTTP the browser must ask ("polling") each time it wants news, which wastes requests and shows changes late, up to one polling interval. With a WebSocket the browser connects once and the backend pushes each event the moment it happens. That is what the dashboard needs for packets, failures and recovery.

The **heartbeat** is a small event sent every 2 seconds even when nothing else happens. Silence on a WebSocket is ambiguous: "nothing changed" looks the same as "the backend froze or the connection died". Because heartbeats are regular, the dashboard can tell the difference: arriving heartbeats mean *connected*, a missed few mean *stale*, and a closed socket means *disconnected*.

The two channels split the job: `GET /api/snapshot` gives the full current state (used on first load and on every reconnect), and `/ws` streams the changes afterwards.

## Packets, sessions, ACKs and the simulator, in plain language

**Packet header.** Every frame starts with a small fixed header, like the address and label on a parcel, so any node can understand it without guessing:

| Field | Why it exists |
| --- | --- |
| `version` | Lets a node refuse a format it does not understand instead of misreading it. |
| `type` | Says what the payload is (a reading, an acknowledgement, later a HELLO...). |
| `src` | Which node *created* the frame, so the reply knows where to go. |
| `dst` | Which node the frame is finally for, even when relays carry it. |
| `session` | A random number chosen at each boot (next paragraph). |
| `seq` | Counts the frames a node has created in this session, so each can be named. |
| `ttl` | A hop budget that relays will decrement, so a frame can never circulate forever. |
| `payload_len` | Lets a receiver check it got the whole frame and nothing extra. |

**Why a session ID.** A board that reboots forgets its counter and starts again at 1. Without more information, a brand-new reading #1 looks identical to the old reading #1, and an acknowledgement from before the reboot could be mistaken for the new one. A random session number, chosen at boot, makes the pair (`src`, `session`, `seq`) unique across restarts. The test `test_restarted_a_has_new_session...` shows an in-flight ACK from the old session being ignored.

**What an ACK proves, and what it doesn't.** A `DELIVERY_ACK` proves that B received that exact reading intact (it names origin, session and sequence). It does **not** prove B has done anything further with it. And **not receiving** an ACK proves nothing about whether the reading arrived: when the ACK itself is lost, B has the reading but A must report it *unacknowledged*. Try `--drop B-A:2` to see exactly this. Sending is not delivery, and delivery is not confirmed delivery.

**Discrete-event simulator with a virtual clock.** Instead of running in real time, the simulator keeps a to-do list of future events ("at 2000 ms, A creates a reading", "at 2020 ms, this frame reaches B") sorted by time. It repeatedly takes the earliest one, sets its clock to that time, and runs it, possibly adding new events. Nothing sleeps, so 20 simulated seconds take milliseconds, and the result does not depend on how busy the laptop is.

**Why seeds make tests repeatable.** "Random" loss is generated by a pseudo-random generator that is fully determined by its starting number, the seed. The same seed gives the same sequence of lucky and unlucky frames, so the same trace, every run and on every computer. A different seed gives a different (but equally repeatable) run. That turns "it sometimes fails" into an experiment we can replay and assert on.

**Link versus route.** A *link* is one radio hop between two neighbors (A to B). A *route* is the whole chain of hops from a source to a destination (A to B to C). A link can work while the route is broken: if B is up but its link to C is down, A can still reach B, yet no reading reaches C. Each node only knows its *next hop* for a destination, never the whole route.

**Hop-by-hop forwarding versus end-to-end delivery.** Forwarding is local: each relay receives a frame, looks at the destination, and passes it to *its* next hop. No relay knows whether the frame finally arrived. Delivery is an end-to-end fact, confirmed only by the destination's ACK travelling back to the origin. That is why retries are done by A (the origin) and not by B: only A is waiting for the end-to-end answer, and a relay cannot tell a lost frame from a lost ACK.

**Physical reachability versus logical topology.** In the classroom all four boards can hear each other. The diamond (A-B, B-C, A-D, D-C) is a rule the nodes follow: each keeps a list of permitted neighbors, refuses to send to anyone else, and rejects frames from anyone else. Simply leaving a link out of the simulator would hide that, and real radios would still deliver the frame. The test `test_a_misconfigured_to_send_directly_to_c_is_rejected_by_c` shows the frame physically arriving at C and being refused.

**Why TTL stops loops.** TTL (time to live) is a hop budget stamped into the frame; each relay subtracts one and refuses to forward when none is left. If two nodes' routes point at each other, the frame bounces between them but its TTL runs out after at most 8 transmissions, so the loop cannot run forever and cannot flood the network. See `routing-loop-misconfiguration.json`.

**Why a retry keeps the same sequence number.** A retry is the *same reading* sent again, so it must carry the same (source, session, sequence) identity. The destination uses exactly that identity to notice it already has the reading. If the retry were given a new sequence number, the destination would count one reading twice and nobody could tell.

**How duplicate detection works, and why C re-ACKs duplicates.** For each (origin, session) C remembers the highest sequence number it has seen plus a 64-bit bitmap of the 64 numbers just below it (a *sliding window*): one bit per number, a few bytes in total, no list that grows. A number already marked in the bitmap is a duplicate. Comparisons use *wrap-safe* arithmetic: sequence numbers wrap from 4294967295 back to 0, so "which is newer" is decided by the difference modulo 2^32, not by `>`. A duplicate means the earlier ACK was probably lost, so C counts the reading once but sends the ACK again; otherwise A would retry until it gave up and report a reading lost that C actually has. I chose a window over a plain set of seen numbers because its memory is fixed no matter how long the network runs.

**Heartbeats and timeouts.** A HELLO is a heartbeat: a tiny frame each node sends every second to say "still here". A neighbor that has not been heard for 3 seconds (three missed heartbeats) is marked *lost*. The timeout trades speed against false alarms: shorter detects sooner but one lost HELLO would look like a failure; three misses make that unlikely. The simulation shows B's loss declared about 2.2-3.1 s after the stop, always within the 3 s timeout plus report delay.

**Why hearing a HELLO proves only one direction.** A HELLO from B reaching A proves B's frames reach A. It says nothing about whether A's frames reach B, which may be a different (worse) radio path. So each HELLO also lists the neighbors its sender hears. Only when B's HELLO lists A does A know both directions work (*two-way*). With A->B down, A hears B but B never hears A: A sees B as *one-way* forever, and the controller will not count the link as usable.

**Flooding with duplicate suppression, and why it cannot storm.** To spread a management frame without knowing the topology, each node passes a new frame to all its permitted neighbors except the one it came from. Without limits that would multiply forever, so there are three: a node remembers the identities it has already handled and drops repeats (each node acts on a frame once); a node drops its own frames when they return; and the TTL decreases at every hop, so even if memory forgot an identity the flood dies within 8 hops. Memory is a fixed 64-entry cache, oldest forgotten first.

**Why management traffic must not depend on the data route.** The data route is exactly what breaks when a relay dies. If reports and ACKs used that route, the news of the failure (and the instructions to fix it, later) would have to cross the broken path. Flooding uses every surviving permitted link, so when B dies A's report still reaches C through D. The test `test_a_reports_reach_c_through_d_after_b_stops` shows it, and `test_ack_still_reaches_a_when_the_relay_on_the_return_path_is_dead` compares it with the old routed behaviour.

**Why "lost contact" is not proof of power loss.** A timeout only says nothing was heard. B could be unplugged, but its radio could also be blocked, its antenna blocked, its channel busy, or the one link to it down while it is fine. The controller therefore says *lost contact*, names who observed it and for how long, and requires every neighbor that still reports to agree before declaring a node lost. The demonstrator knows B was unplugged; the software only knows it cannot hear B.

**Why jitter helps.** If all boards power up together and send HELLOs on the same 1 s tick, their transmissions would land at the same instant every time, and on a shared radio channel they could collide repeatedly. Adding a small random offset (and a random starting point) spreads them out. The simulator does not model collisions, so it shows the spread-out timing rather than the benefit.

## Routing in plain language (Phase 4a)

**Dijkstra on our diamond.** Every usable link has a cost (its measured round-trip time, in milliseconds). Dijkstra finds the cheapest chain of links from A to C by growing outwards from A, always settling the closest unsettled node next. With A-B 30, B-C 35, A-D 65, D-C 65: A is at 0; B at 30 and D at 65 are its neighbors; settle B (30), which offers C at 30 + 35 = 65; settle D (65), which would offer C at 65 + 65 = 130, worse than 65; C is settled at 65 through B. The route is A > B > C. If two routes cost exactly the same, the rule is fixed so the answer never depends on luck: fewer hops, then the lower node ids (B before D).

**Why RTT and not one-way delay.** To measure "how long did this take one way" you must subtract the sender's clock reading from the receiver's, and the boards' clocks are not synchronised: one can be seconds ahead of the other, so the difference is meaningless. A round trip needs only one clock. A notes when it sent a probe and when the reply came back, on its own clock, and the difference is a real duration. The reply carries no time at all; it only repeats the probe's identity. RTT is the sum of both directions, which is fine for a cost.

**Why relays are installed first.** The source is the only node that *starts* sending down a path. If A is told "use B" while B has not yet been told "send to C", A's frames reach B and B has nowhere to send them: a black hole until B is configured. If B is told first and A afterwards, nothing is ever sent to B before B is ready. That is why the controller waits for each relay's acknowledgement before telling A, and calls the route active only when A has acknowledged as well. Acknowledgements make "I think it's installed" into "I know it is".

**Why versions and epochs.** Updates can be duplicated, delayed or reordered (flooding delivers copies by several paths). Without a number, a late old update (A > B) could arrive after a newer one (A > D) and quietly put the route back through the dead relay. Each update carries a version; a node keeps the highest it has seen and ignores anything older. But the version is a counter in the controller's memory: if the controller restarts it begins again at 1, and every update would look old. The epoch fixes that: it names one run of the controller and only ever goes up, and a newer epoch beats any version of an older one. On the real laptop the epoch is its start time.

**Why route updates must travel by flooding.** The update that moves A away from a dead B has to reach A. If it followed the data route (through B) it would be sent into the failure it is meant to repair. Flooding sends it over every surviving link, so it reaches A through D. The acknowledgement comes back the same way.

**Why hold-down prevents flapping.** Imagine B's radio is on the edge of its range: it appears for a second, vanishes for two, appears again. Each reappearance would make the controller switch back to B (it is cheaper), then away again. Every switch costs messages and a short window where packets can be lost, and the network spends its time switching rather than carrying data. Requiring a returning link to stay up for 5 s before it is used again means the controller switches back only when the evidence says B is staying. Leaving B is never delayed: when it fails the route moves at once.

**Why "no route" must be explicit.** A source with no instructions and a source that has been *told* there is nothing available look the same from outside, but they are different facts: the first might simply not have heard yet. Saying "No available route" makes the situation observable, lets the source drop readings immediately with a reason (counted, nothing piling up) instead of sending them into the dark and retrying each three times, and tells the person looking at the dashboard what is actually wrong. It also keeps the controller honest: if the source *could not* be told, the announcement ends `unconfirmed`, and nothing claims otherwise.

## Local failover in plain language (Phase 4b)

**Compute centrally, react locally.** The laptop knows the whole network, so it works out both the best route and a spare next hop for each node. A node knows only its own neighbors, but it can notice quickly when its next hop has failed, and then all it has to do is use the spare it was given. Splitting the work this way keeps the slow part (thinking) off the failure path, so the reaction is a table lookup. Routers do the same in IP fast reroute and in MPLS fast reroute: backup paths are precomputed and installed, and the point of failure switches to them at once; the control plane then catches up and installs the new best route.

**Loop-free and node-protecting, with the diamond.** Route A > B > C. If B fails, A may use D, because D reaches C directly (A > D > C): D's own path does not pass through A (no loop) and does not pass through B (it does not rely on the failed relay). B itself has no backup: its only other neighbor is A, whose way to C is through B, so "falling back" to A would send the frame straight back. Now add a link D-B, and make D's shortest path to C go D > B > C. D is still "loop-free" (it does not come back through A), but it is no protection: when B fails, D's path fails with it. That is why the rule also requires that the backup's path avoids the primary next hop ("node-protecting").

**Why a timeout alone misses one-way failures.** A node declares a neighbor lost when it stops *hearing* it. If only A's transmissions to B are lost, A keeps hearing B's HELLOs, so its timer never expires, yet nothing A sends reaches B. B stops hearing A and drops it from its HELLO list; A then sees that B no longer lists it. The neighbor is *one-way*, not lost. That is why T1 is "no longer two-way" and why T2 (failed sends) exists: it notices from the sender's side.

**What the link-layer callback proves and does not.** After a unicast send, the radio tells the sender whether the *next hop's radio* acknowledged the frame. That proves one hop, a moment ago. It does not prove the relay will forward the frame, that C received it, or that the application accepted it (that is DELIVERY_ACK). It can also say "failed" for a frame that did arrive, when only the acknowledgement was lost. So it is a good, fast hint that a link is bad, and a poor proof of delivery. Real ESP8266 behaviour must still be checked on boards.

**Why nodes never switch back on their own.** When the primary comes back, the node cannot tell a real recovery from a flapping link or a one-off good moment, and two nodes could disagree about which route is in force. Only the controller sees all the links, and it already has hold-down and policy for exactly this decision. A node that went back by itself would also undo the controller's choice and could ping-pong.

**What the measurement comparison shows.** Switching the relay locally is only quicker if the node can notice sooner than the controller can. Detecting by HELLO timeout (T1) waits for the same 3 s as the controller, so it saves only the report and installation time (about 0.2 s here) and nothing in the one-way case. Detecting by failed sends (T2) does not wait for a timer, and moved the switch from about 2.6 s to about 0.1 to 1 s. The readings themselves are then limited by the reading interval and the ACK timeout (a reading created at the failure retries at +1 s), so the delivery gap improves by less than the switch does. T2 also fires falsely under loss (about 20 per simulated hour at 5 % loss with K = 3), which costs a hold-down of the good link each time.

## Controller outage in plain language (Phase 5b)

**Why a heartbeat.** Silence is ambiguous: if the laptop sends C nothing for a while, C cannot tell "the controller has nothing to say" from "the controller is gone". So the laptop says "I am alive" every second, over the USB link only (never on the radio, where it would cost every node airtime to tell C something only C needs). Three missed heartbeats (3 s) and C treats the controller as unreachable; one missed heartbeat is not enough, so a single delayed message is not an outage. C answers each heartbeat with a short status, so the laptop has the same test the other way round: 3 s without anything from C and it declares its link to C lost, and tells the dashboard its picture is stale.

**Fail-static.** When the controller goes, nothing on the radio changes. Nodes keep their installed routes and backups, A still fails over to D if B dies, and C keeps accepting and acknowledging readings, because those acknowledgements come from C, not from the laptop. What stops is only new thinking: no new routes are calculated until the controller is back. Freezing in the last known good state is safer than guessing.

**Why the buffer is bounded, and what is dropped first.** C is a small board with tens of kilobytes of free memory, and an outage can last any length of time. An unbounded buffer would eventually crash C, which would lose everything. So C keeps 50 entries and, when full, gives up the least valuable, oldest one and counts it, so the loss is reported instead of hidden. Readings are what the project exists to deliver, so they are kept in preference to topology reports, acknowledgements and telemetry. Those are also *coalesced*: a newer report from B says everything an older one did, so only the newest is kept, and a long outage costs one slot per node. Failover reports are always kept, because they record a recovery the controller must learn about. When even readings must go, the oldest go first: a recent reading is worth more than a stale one.

**Ages instead of timestamps.** C has no calendar clock, and the laptop's clock means nothing to C. Comparing two clocks that were never synchronised gives nonsense. Instead C notes when each entry arrived *on its own clock*, and on replay says "this is 28.986 s old". The laptop subtracts that from its own clock. Both numbers are durations on one clock, so the result is right to within the time the message spent on the serial line.

**Why replay does not count anything twice.** In the last few seconds before C notices the outage, it is still sending things up that nobody receives. So C keeps a copy of what it sent back to the second-to-last heartbeat and replays it too. Some of those the laptop did receive. That is safe because every reading has an identity, (origin, session, seq), and the controller counts a reading only the first time it sees that identity; a second copy is counted as a repeat. The replay is bracketed by start and end markers with counts, so the dashboard can say "replaying 26 entries" and "done: 0 dropped".

**Why old information must not override new.** A replayed topology report can be 40 s old. If the controller treated it as news, a link that died 30 s ago would look alive again, and could even be routed over. So the controller compares the report's real age with what it already holds: an older report than the one held is ignored, and a report older than the 15 s expiry arrives already stale. Freshness comes from when something happened, not from when it was delivered.

## Limitations of the live demo (Phase 5a)

- One live run is shared by everyone connected; presenter controls act on it for all viewers. There is no per-client session and no persistence: a backend restart starts a new run (`run_id` 1).
- While the gateway is detached the controller and the dashboard see nothing new from the network; since Phase 5b they are told the link is lost and receive what C held when it returns.
- The pacing task runs in the server's event loop. At very high speed the simulation may fall behind (at most 5 s of simulated time per 50 ms wake-up) instead of keeping up.
- The default `Origin` allow-list is `http://localhost:5173` (the Vite dev server). Requests with no `Origin` header are accepted, so keep the default localhost bind.
- All numbers are simulated: delays, loss and timings are configured values, not measurements.

## Limitations of the outage handling (Phase 5b)

- Simulation only. The laptop ↔ C messages are typed messages inside the backend; their serial encoding and a real serial gateway are the next step.
- C's buffer holds 50 entries: about 100 s of 2 s readings, or 25 s of 500 ms readings. Longer outages lose the oldest readings (counted). The RAM estimate (1.5 KB typical, 4 KB worst case) is not measured on an ESP8266.
- **An outage shorter than C's 3 s timeout is not noticed, so nothing is replayed**: readings sent up during it that the laptop missed are absent and not counted as lost (a 1 s detach in the simulation loses every reading in it). A fix is proposed for the serial step: numbered up messages and a replay request.
- Ages ignore the serial transfer time and C's clock drift; replay speed (20 ms per entry) is a simulated setting.
- The buffer is RAM-only: if C loses power, what it held is gone.

## Limitations

- Tested on Windows only so far; macOS is covered by CI after push.
- No authentication; keep the default `127.0.0.1` binding.
- Failures are detected and routed around automatically (Phase 4a), but the node-side reaction is still the controller's: until a replacement route is confirmed, readings sent toward the failed relay are lost (bounded: 3 attempts each). Backup next hops and local failover are Phase 4b.
- The route and its backups are calculated for one flow, A to C. A backup protects only the failures the last route version anticipated; a second failure before the controller replaces it may leave no route.
- If both relays are dead, A cannot be told there is no route (nothing can leave C). With local failover A finds out by itself (backup also dead, `local_no_route`) and drops readings with a reason, but its report cannot leave it either. Without local failover A's readings are lost after their 3 attempts.
- The send callback is a simulated model of ESP-NOW's. It is not verified on ESP8266 boards.
- Recovery timings are quantised by the 2 s reading interval and the 1 s retry timer: the first reading to arrive over the new path is usually a retry, so *service recovery* is mostly a statement about those timers, not about how fast the route was installed (which is *route active*).
- The controller waits 1 s after the first link is measured before computing the first route, and 5 s of silence at start before saying "no route". Both are configurable and were chosen for the simulation.
- Detection times are simulated: the reference is the scripted stop time and delays are configured values. Real boards add radio timing the simulator does not model, including collisions.
- Reports are sent by flooding without their own retry; a lost report is replaced by the next periodic one (5 s), and the controller expires stale information after 15 s.
- If node C (the only gateway) stops, the controller learns nothing and can only show information as *stale*; it cannot tell that C itself is gone.
- Only the source's readings are retried; the destination's ACKs are not. A reading counted *lost* may still have been delivered (only its ACKs failed); the summary shows this as unique delivered > acknowledged.
- Duplicate detection is bounded (64 numbers, 8 sessions): a retry older than the window is refused and a very old session may be forgotten.
- A simulation shows protocol logic, not radio behavior. Delays, loss and the 5 ms service time are configured values, not measurements.
- `httpx` emits a deprecation warning under pytest (Starlette's test client suggests `httpx2`); harmless for now.
