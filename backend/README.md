# Backend

Laptop-side backend. It contains:

- a FastAPI app serving a small HTTP API and a live WebSocket stream to the dashboard (reports its own status only);
- the node **protocol** module: binary frame encoding and validated decoding;
- a deterministic **simulator** with four virtual nodes (A, B, C, D): manual or controller-installed routing, forwarding, retries, duplicate detection, HELLO discovery, link probes, bounded flooding and failures;
- a **gateway interface** (`transports/`) and a **controller** (`controller/`): a topology view and, since Phase 4a, Dijkstra route calculation with versioned route installation and automatic rerouting. The controller sees the network only through events that reached node C and acts on it only through commands that node C turns into frames.

Primary routes are calculated and installed automatically (Phase 4a). Backup next hops, local failover, the controller heartbeat and C's buffering, adaptive policies, a serial gateway and a database do not exist yet.

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

## Run the server

```sh
uv run python -m iot_backend
```

Then open <http://127.0.0.1:8000/health> or <http://127.0.0.1:8000/docs>. Stop with Ctrl+C.

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
| `--measure-seeds N` | Run a controller scenario with seeds 1..N and print min / median / max of failure detection, route active, distribution, service recovery, delivery gap and readings lost, plus route computation time (wall-clock CPU, reported separately). Prints no trace. |

Scenario files are strict JSON (an unknown key is an error, so a typo cannot silently change an experiment). They can set `seed`, `duration_s`, `settings` (interval, timeout, `max_attempts`, `ttl`, `queue_capacity`, `service_ms`, `discovery`, `ack_delivery`, `hello_interval_ms`, `hello_jitter_ms`, `neighbor_timeout_ms`, `report_interval_ms`, `report_coalesce_ms`, `flood_cache_size`, `probe_interval_ms`, `probe_jitter_ms`, `rtt_window`, `rtt_min_samples`, `rtt_max_age_ms`, `max_routes`), `routing` (`"manual"` or `"controller"`), `controller` (`epoch`, `hold_down_ms`, `install_timeout_ms`, `install_max_attempts`, `replan_delay_ms`, `max_failed_plans`, `no_route_grace_ms`, `initial_settle_ms`), `permitted`, `routes` (manual routing only), `links` (`delay_ms`, `loss`, `drop_frames`, `drop_msg`, `down`), `initial_seq`, and timed `actions`: `stop_node`, `start_node`, `restart_node`, `set_route`, `set_permitted`, `link_down`, `set_link`, `inject_frame`, `restart_controller`.

Event names in the trace: `frame_sent` (a node created and transmitted a frame), `frame_forwarded` (a relay passed one on), `frame_received`, `frame_rejected`, `frame_dropped`, `frame_dropped_by_link`, `reading_delivered`, `duplicate_received`, `ack_received`, `ack_ignored`, `ack_timeout` (one attempt), `reading_lost`, `neighbor_changed`, `neighbor_restarted`, `node_booted`, `node_stopped`, `route_update_sent` (C originated an instruction), `route_installed` (a node applied it), `route_update_ignored` (duplicate, stale or refused). The `delivery_time_ms` on `ack_received` is the time from the *first attempt* to the ACK (time to confirmed delivery), together with `attempts`; it is not an RTT. A frame appears as `A:3905a56d#1`: creator, session id in hex, sequence number.

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
    api/               dashboard-facing endpoints (routes.py, ws.py)
    protocol/          frame constants, message classes, encode()/decode()
    simulator/         engine.py (virtual clock), link.py, nodes.py (forwarding, retries),
                       dedup.py (duplicate filter), trace.py, scenario.py (Simulation),
                       scenario_file.py (JSON loader), gateway.py (SimulatedGateway),
                       measurements.py (detection and recovery times), __main__.py (CLI)
    transports/        the Gateway interface the controller is written against
    controller/        topology.py: nodes and links from reports, with freshness and RTT;
                       routing.py: Dijkstra, versioned installation, hold-down, no-route
  tests/               pytest checks (test_protocol.py loads ../tests/protocol-examples/,
                       test_scenarios.py loads ../scenarios/)
```

`src/iot_backend/` is the Python package from plan.md's `backend/` tree. `controller/`, `transports/` and `storage/` will be added inside it by the phases that need them. Backend tests live in `backend/tests/` so the backend can be tested on its own; the shared protocol examples that the firmware will also use live at the repository top level, in `tests/protocol-examples/`.

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

## Limitations

- Tested on Windows only so far; macOS is covered by CI after push.
- No authentication; keep the default `127.0.0.1` binding.
- Failures are detected and routed around automatically (Phase 4a), but the node-side reaction is still the controller's: until a replacement route is confirmed, readings sent toward the failed relay are lost (bounded: 3 attempts each). Backup next hops and local failover are Phase 4b.
- The route is calculated for one flow, A to C, and only primary next hops are used; the backup field is carried but always none.
- If both relays are dead, A cannot be told there is no route (nothing can leave C). A's readings are then lost after their 3 attempts rather than dropped `no_route`.
- Recovery timings are quantised by the 2 s reading interval and the 1 s retry timer: the first reading to arrive over the new path is usually a retry, so *service recovery* is mostly a statement about those timers, not about how fast the route was installed (which is *route active*).
- The controller waits 1 s after the first link is measured before computing the first route, and 5 s of silence at start before saying "no route". Both are configurable and were chosen for the simulation.
- Detection times are simulated: the reference is the scripted stop time and delays are configured values. Real boards add radio timing the simulator does not model, including collisions.
- Reports are sent by flooding without their own retry; a lost report is replaced by the next periodic one (5 s), and the controller expires stale information after 15 s.
- If node C (the only gateway) stops, the controller learns nothing and can only show information as *stale*; it cannot tell that C itself is gone.
- Only the source's readings are retried; the destination's ACKs are not. A reading counted *lost* may still have been delivered (only its ACKs failed); the summary shows this as unique delivered > acknowledged.
- Duplicate detection is bounded (64 numbers, 8 sessions): a retry older than the window is refused and a very old session may be forgotten.
- A simulation shows protocol logic, not radio behavior. Delays, loss and the 5 ms service time are configured values, not measurements.
- `httpx` emits a deprecation warning under pytest (Starlette's test client suggests `httpx2`); harmless for now.
