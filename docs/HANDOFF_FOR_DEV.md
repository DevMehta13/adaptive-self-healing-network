# Handoff for Dev: the backend, in one sitting

Written by Harshal's backend work for Dev, who owns `dashboard/` and `firmware/`. The backend is **complete in simulation**. Everything about the real world (radio, USB, boards, sensor) is untested, and this document says where. Read it top to bottom once; the review list in section 4 is the thing to answer. Nothing in a contract is frozen until you approve it.

**How long it takes.** About an hour to read; about half a day to run every demo in section 5 if you want to see each one. Do the first one (the live demo) before you read the rest.

**What is simulated and what is not, in one line.** All results in this repository come from a deterministic simulator with configured delays and loss. The laptop side of the USB link has run only against a fake node C. No board has been bought, programmed or connected.

## Contents

1. [What the backend does](#1-what-the-backend-does)
2. [Set up in five minutes](#2-set-up-in-five-minutes)
3. [The contracts and what each requires of you](#3-the-contracts-and-what-each-requires-of-you)
4. [The consolidated review list, with recommended answers](#4-the-consolidated-review-list-with-recommended-answers)
5. [How to run every demo](#5-how-to-run-every-demo)
6. [Known limitations](#6-known-limitations)
7. [Hardware test plan for Phases 6 to 8](#7-hardware-test-plan-for-phases-6-to-8)

---

## 1. What the backend does

```text
   boards (radio, ESP-NOW)                         laptop
 ┌─────────────────────────┐                ┌──────────────────────────────────────────┐
 │  A ── B ── C ──USB──────┼── serial ──────┤ transports/   (serial or simulator)      │
 │   \       /   (gateway) │   lines        │      │ typed messages                    │
 │    ── D ──              │                │ controller/   topology, Dijkstra,        │
 │                         │                │               policies, route install    │
 └─────────────────────────┘                │      │ events                            │
   simulated in `simulator/`                │ live/ + api/  snapshot, WebSocket,       │
   (same protocol bytes)                    │               controls, policy, history  │
                                            └───────────────────┬──────────────────────┘
                                                                │ HTTP + WebSocket
                                                          dashboard (yours)
```

The four ESP8266 boards form a diamond: A (source) to B and D, both to C (destination and USB gateway). The laptop computes routes; the boards only forward. In simulation the whole left half is Python (`backend/src/iot_backend/simulator/`) speaking the **same binary frames** your firmware must speak. In hardware mode (`IOT_MODE=hardware`) the left half is real boards and only node C is connected, over USB serial.

What exists, by phase (all backend side, all simulation; "6-prep" was tested against a fake C):

| Phase | What the backend does |
| --- | --- |
| 1, 2 | Binary packet format (14-byte header, 250-byte limit). Four virtual nodes: forwarding with TTL, end-to-end retries (3 attempts), duplicate detection at C, bounded queues, failure injection. |
| 3 | HELLO discovery, neighbor tables (one-way / two-way / lost), bounded flooding of management traffic, topology reports; the controller declares *lost contact* (never "powered off"). |
| 4a | Link probes (round trips on the prober's own clock), Dijkstra, versioned and acknowledged route installation (relays first, source last), automatic rerouting, explicit "No available route". |
| 4b | Backup next hops chosen by the controller; a node whose next hop fails switches to its backup by itself (local failover), by two triggers: next hop no longer two-way (T1) or 3 failed link-layer sends (T2). |
| 5a | The simulation runs live, paced against the wall clock, and is served to the dashboard: snapshot, ordered WebSocket events, presenter controls. The dashboard shows what the **controller** knows. |
| 5b | A controller heartbeat to C; when it stops C keeps the radio network working, holds what it would have sent up in a bounded buffer (50 entries) and replays it when the controller returns. Each reading counted once. |
| 6-prep | The serial line format between C and the laptop (JSON lines with a CRC), a numbered and acknowledged uplink, `SerialGateway`, hardware mode, a fake C, a serial probe tool, and a C test-vector header. |
| 9 | Two routing policies, **lowest delay** (smoothed RTT) and **highest reliability** (probe delivery probability as `-log p` costs), selectable by configuration and live API, with hysteresis (3 evaluations in a row); a fast-but-lossy versus slow-reliable scenario; a many-seed comparison. |
| 10 | A bounded SQLite run history and CSV export of a run's readings and events, by command line and API. |

What does **not** exist: `dashboard/`, `firmware/`, any hardware test, a real sensor, and per-viewer sessions (one live run is shared by everyone connected).

Three rules run through everything and are worth internalising before you read any contract:

1. **The dashboard shows what the controller knows, not the simulator's truth.** After "Stop B" B still looks reachable until the network notices. That gap is the detection time, and it is part of what is demonstrated.
2. **Planned route and observed path are different objects.** The route is the controller's plan; the observed path is built only from what node C can see (the neighbor that handed it a frame, the TTL left). Animations are not delivery evidence.
3. **Radio success is not delivery.** The link-layer callback proves one hop; only C's DELIVERY_ACK proves a reading arrived.

---

## 2. Set up in five minutes

Requires [uv](https://docs.astral.sh/uv/). Same commands on Windows (PowerShell) and macOS.

```sh
cd backend
uv sync
uv run pytest            # all tests
uv run ruff check . && uv run ruff format --check .
uv run python -m iot_backend            # the live demo on http://127.0.0.1:8000
```

In a second terminal: `uv run python -m iot_backend.watch --no-heartbeats`. Full instructions, Windows and macOS port names, and curl / PowerShell versions of every command are in [backend/README.md](../backend/README.md). The backend has been run on **Windows only**; macOS is unverified until CI runs after the branch is pushed.

---

## 3. The contracts and what each requires of you

There are five artifacts that connect your work to the backend. For each: where it is, what it fixes, what you must implement or consume, and how you check it.

| Artifact | Where | Governs | You implement or consume | Check it with |
| --- | --- | --- | --- | --- |
| **Radio protocol** | [PROTOCOL.md](PROTOCOL.md) | The bytes between boards | **Implement** (firmware, all four nodes) | `tests/protocol-examples/` (79 files); `tests/generated/test_vectors.h` |
| **Serial protocol** | [SERIAL_PROTOCOL.md](SERIAL_PROTOCOL.md) | The USB line between C and the laptop | **Implement** (firmware, node C) | `tests/serial-examples/` (48 lines); the same header; `fake_c`, `serial_probe` |
| **Dashboard API** | [DASHBOARD_API.md](DASHBOARD_API.md) | HTTP and WebSocket to the browser | **Consume** (dashboard) | `tests/dashboard-fixtures/`; a running backend |
| **Test vectors** | `tests/generated/test_vectors.h` (from `python -m iot_backend.vectors`) | Both firmware formats, as C arrays | **Compile into the firmware tests** | `gcc -std=c99 -Wall -Wextra -Werror -pedantic` is clean |
| **Fake C and probe** | `python -m iot_backend.fake_c`, `python -m iot_backend.serial_probe` | Bring-up without boards, and with them | **Use** | See section 5 |

### 3.1 Radio protocol: what the firmware must do

Read [PROTOCOL.md](PROTOCOL.md) in this order: *Frame layout*, *Message types*, *Decoding rules*, then the behaviour sections. The firmware must:

1. **Encode and decode every frame type bit-exactly.** Ten types: DATA, DELIVERY_ACK, HELLO, TOPOLOGY_REPORT (with two optional trailers: RTT, and since Phase 9 probe statistics), PROBE, PROBE_REPLY, ROUTE_UPDATE, ROUTE_INSTALL_ACK, LOCAL_FAILOVER_REPORT, TELEMETRY. Little-endian, 14-byte header, at most 250 bytes. **Pass:** encoding each `valid-*.json` gives its `hex` exactly, decoding gives its fields, and every `invalid-*.json` is rejected with the stated reason in the stated order.
2. **Reject in the documented order** (the *Decoding rules* table) and **count rejections by reason**. The order matters because the examples depend on it.
3. **Forward** by the installed table (one entry per destination), decrementing TTL; a relay drops a frame that arrives with TTL 1; obey the **permitted-neighbor list** (never send to, and reject frames from, a non-neighbor, even though all four radios can hear each other).
4. **Retry end to end at A only:** the same frame (same source, session, sequence), at most 3 attempts; relays never acknowledge or retry. **C** keeps a per-(source, session) 64-number sliding-window bitmap for at most 8 sessions, counts a reading once, and **re-acknowledges** a duplicate.
5. **Choose a random non-zero session at every boot**, and count sequence numbers per session (probes and HELLOs consume numbers too).
6. **HELLO** once a second per permitted neighbor (unicast, ±100 ms jitter), neighbor table with `one_way` / `two_way` / `lost` (3 s timeout), two-way only when the neighbor's HELLO lists your current session.
7. **Flood management traffic** (reports, ACKs, route updates, telemetry): handle each frame once through a 64-entry identity cache, never send it back to the sender, drop your own frames, TTL-bounded.
8. **Probe** each neighbor once a second (one outstanding probe each), time the round trip **on your own clock**, report the **median of the last 5** (needs 3). **New in Phase 9:** keep the outcome of the last 20 completed probes per neighbor (answered or not), report `answered / completed` once 5 have completed, throw that history away while the neighbor is in the lost state.
9. **Install routes** from ROUTE_UPDATE only when `src` is the gateway's id; accept only a newer `(epoch, version)`; acknowledge installed, rejected or stale; keep a backup next hop per entry.
10. **Local failover:** switch to the installed backup on T1 (next hop no longer two-way) or T2 (K = 3 consecutive link-layer send failures); report it (bounded repeats); never switch back by yourself.
11. **At C only:** the 50-entry bounded buffer with its eviction order and coalescing ([PROTOCOL.md](PROTOCOL.md) *Laptop and C*), the heartbeat watch (3 s), numbered uplink (section 3.2).

Sizing estimates (not measured): about 0.7 KB discovery, 0.1 to 0.4 KB for probes and routes, 0.05 KB for the probe window, 0.05 KB for failover, about 4 KB for a 16-frame queue and the duplicate window; C's buffer 2.3 KB typical, 5.7 KB worst case. **Measure free heap in Phase 6** (section 7).

### 3.2 Serial protocol: what C's firmware must do

[SERIAL_PROTOCOL.md](SERIAL_PROTOCOL.md) is short (about 150 lines) and is the contract you are most likely to need to push back on. Summary of what C must do:

1. **115200 8N1.** One message per `\n`-terminated line. A data line is compact JSON, `*`, and a **CRC-16/CCITT-FALSE** as four hex digits (the routine is in the document; check value `0x29B1` for `123456789`). Everything C prints for humans starts with `#`.
2. **Receive** at most 128 bytes per line from the laptop; drop (and count) anything longer or with a bad checksum; accept only the four downlink types `hb`, `ack`, `rs`, `tx`, with exactly their fields.
3. **Send up** every TOPOLOGY_REPORT, ROUTE_INSTALL_ACK, LOCAL_FAILOVER_REPORT and TELEMETRY addressed to C and every DATA frame it accepts, as `fr` lines with the frame as hex, the neighbor that handed it over (`via`), its age, and its duplicate verdict.
4. **Number** what it sends up, keep it until the laptop's cumulative acknowledgement, window 16, answer `rs` by resending, say `gone` for numbers it no longer holds. After a reboot numbering starts at 1 under a new boot id `b`.
5. **`tx`:** build the 14-byte header itself (its own `src`, its session, its next sequence number, the default TTL) around the payload the laptop supplies, for ROUTE_UPDATE only (11-byte payload), and flood it.
6. **Never write a log line in the middle of a data line**, and never write from the ESP-NOW receive callback.

**Pass:** the vectors in `tests/generated/test_vectors.h`, and then `serial_probe` against the real board shows `DATA` lines for everything and `BAD` for none. The review questions in section 4 (R1 to R12) are the points where the contract may not survive contact with the ESP8266.

### 3.3 Dashboard API: what the dashboard must do

[DASHBOARD_API.md](DASHBOARD_API.md). The dashboard must:

1. **Load the snapshot, then stream.** `GET /api/snapshot`, then `/ws` with `event_id` ordering; on reconnect fetch a fresh snapshot and discard events with `event_id <= last_event_id`; replace all state when `run_id` changes or `last_event_id` goes backwards.
2. **Show the labels the contract lists** (simulation versus hardware, "delayed" for replayed readings, "stale" while the gateway link is lost, the age of telemetry counts, "No available route", local failover versus controller reroute).
3. **Keep planned route and observed path visibly different**, and draw `one_way` and `stale` links differently from `up`.
4. **Show the policy** (`policy.current`, `route.policy`) and, if you wish, a selector that POSTs `/api/policy`, plus "better route seen 2 of 3 times" from `route.pending_switch`.
5. **Offer run history and CSV download** from `/api/runs` (Phase 10; the backend works, the UI is yours).
6. **Presenter controls** are `POST /api/control/*` (simulation mode only; hardware mode answers 409).

**Pass:** build against `tests/dashboard-fixtures/` first (no backend needed; a test keeps them valid), then against a running backend.

### 3.4 Test vectors, fake C and probe

- `uv run python -m iot_backend.vectors` writes `tests/generated/test_vectors.h`: plain C99 arrays of the radio examples and serial lines, with expected results, the line limits and the CRC check value. `--check` fails if it is out of date.
- `uv run python -m iot_backend.fake_c` serves the simulated network behind the real line format on a TCP port; the backend reaches it with `IOT_SERIAL_PORT=socket://127.0.0.1:7777`. It proves the laptop's side, not your firmware.
- `uv run python -m iot_backend.serial_probe --port COM3` (or `/dev/cu.usbserial-0001`) is the bring-up console for a real board: every line C sends, classified `DATA`, `LOG`, `NOISE` or `BAD`; it sends heartbeats and acknowledgements like the backend and can send test lines (see [backend/README.md](../backend/README.md)).

---

## 4. The consolidated review list, with recommended answers

These come from the earlier STATUS lists plus the Phase 9 and 10 additions. "Recommended" is Harshal's suggestion, not a decision. Mark each **agree**, **change to ...**, or **defer to Phase 6 measurement**.

### 4.1 Serial link and node C (review [SERIAL_PROTOCOL.md](SERIAL_PROTOCOL.md) first)

| # | Question | Recommended answer |
| --- | --- | --- |
| R1 | Line format: `{json}*CRC`, `#` for logs, 115200 8N1, limits 128 bytes down and 640 up? | **Agree.** The limits come from the ESP8266 core's 256-byte receive buffer behind a 128-byte FIFO. Raise `Serial.setRxBufferSize()` if Phase 6 shows loss. |
| R2 | Is CRC-16/CCITT-FALSE (check value 0x29B1) fine, and do the vectors in `test_vectors.h` pass in your C++? | **Agree**, and make passing the vectors the first firmware test you write. |
| R3 | The uplink messages `fr`, `st`, `rps`, `rpe`, `gone`, `hello` and downlink `hb`, `ack`, `rs`, `tx`, with every field. | **Agree** unless a field is awkward to produce; tell Harshal which and it changes on the laptop side. |
| R4 | The reliable uplink in section 4 of the serial document (numbering, window 16, keep until acknowledged, resend on `rs`, `gone`). | **Agree**; the window of 16 is a constant. If RAM is tight shrink it (it is carried in `st`). |
| R5 | `tx`: C builds the header and accepts only ROUTE_UPDATE with an 11-byte payload. | **Agree.** It is the only place C's sequence counter is correct. |
| R6 | Can C keep about 2.3 KB (typical) to 5.7 KB (worst case) of frames next to everything else? | **Defer to Phase 6.** Measure free heap. If tight, reduce capacity from 50 (a constant, reported in `hello` and `st`) before changing any format. |
| R7 | Can the ESP-NOW receive callback queue frames while `loop()` writes lines (about 26 ms for a 300-byte line)? | **Yes, by design:** the callback only enqueues; `loop()` writes. Use a small fixed ring. If the ring overflows, count it in `drop`. |
| R8 | Does opening the port from the laptop reset the NodeMCU with your board's CH340 or CP2102 chip even though DTR and RTS are cleared before opening? | **Defer to Phase 6 (test first, on both OSes).** If it resets, a capacitor on the reset line or opening the port in a different mode is the fix; no protocol change. |
| R9 | Is 115200 the right baud rate? | **Keep 115200** until throughput is measured (a few frames a second is far below 11.5 KB/s). Raise it only if replay of 50 entries is too slow; it is configurable on the laptop. |
| R10 | The 20 ms per replayed entry (a simulated setting) and 128-byte rule. | **Defer to Phase 6.** Measure real replay time of 50 entries. |
| R11 | Decisions to confirm: window 16; numbered entries evicted first, named in `gone` with kinds (`R`, `M`, `S`, `?`); a C restart is reported as a loss with a lower bound; a retried reading keeps the first copy at C. | **Agree** all four; the alternatives are less honest or more RAM. |
| R12 | The buffer rules from Phase 5b: 50 entries, RAM only, eviction order (already-sent, then reports/acks/telemetry, then readings, then failover reports; oldest first within a class), coalescing to the latest report/ack/telemetry per node, first copy of a repeated failover report kept, replay oldest first between `rps` and `rpe`. Is a fixed slot array practical; can C use `millis()` ages (unsigned subtraction survives the 49-day wrap)? | **Agree; yes.** Fixed slots are the natural implementation. |

### 4.2 Dashboard API ([DASHBOARD_API.md](DASHBOARD_API.md))

| # | Question | Recommended answer |
| --- | --- | --- |
| R13 | The whole API as rewritten in Phase 5a: snapshot shape and `recent_events`; every event type and its fields; `event_id` never restarting within a run; `run_id` meaning "replace everything"; the bounded history (2000) and the `complete` flag; the 1013 close for a client more than 2000 messages behind. | **Agree after you have built the first screen against the fixtures.** Anything that is awkward is cheap to change now and expensive after. |
| R14 | The honesty rules: controller's view only; `observed_path` from C's evidence with `null` for relays C cannot name; plan and observation as separate objects; the separate `environment` channel; telemetry counts with `known_by` and `age_ms`. | **Agree**; they are the point of the demonstration. |
| R15 | The nine `POST /api/control/*` endpoints, the error shape and codes, the Origin / `Sec-Fetch-Site` / JSON-only checks, `step` as a test aid, and Origin-less requests being accepted (curl) because the default bind is localhost. | **Agree.** Do not offer `step` in the UI. |
| R16 | Hardware mode in the API: `mode: "hardware"`, `scenario: null`, `presenter.conditions: null`, controls refused with 409; `gateway.uplink`, `gateway.serial` (with `available_ports`), events `uplink_loss` and `serial_port`. | **Agree.** |
| R17 | In hardware mode `sim_ms` is the laptop's clock since the backend started. Rename to a neutral name such as `t_ms`, or keep? | **Keep `sim_ms` in this version.** The label already says SIMULATION or HARDWARE, a rename touches every event and fixture, and the field means "time since the run began" in both. Decide before you write the time axis; it is one search-and-replace on the backend if you prefer `t_ms`. |
| R18 | UI: how to show a closed or missing serial port, lost uplink messages, "C restarted", "stale" during a controller outage, the "delayed" label, and readings lost to overflow. | **Suggested:** a persistent banner for port closed or gateway link lost ("network view is stale as of ..."); a counter chip for lost uplink messages with a tooltip of `lower_bound`; a "C restarted" marker on the timeline; delayed readings drawn hollow with their `delay_ms`; overflow shown as a red "N readings never arrived". |
| R19 | **New, Phase 9:** the `policy` snapshot section, `route.policy` / `plan_policy` / `delivery_probability` / `pending_switch`, `smoothed_rtt_ms` and `delivery_probability` on links, events `policy_changed`, `route_candidate`, `route_candidate_dropped`, `policy` on `route_computed` / `route_active`, and `cost_ms` being `null` under highest reliability. | **Agree.** Display delay in ms for the delay policies and probability as a percentage for reliability; never mix the units. |
| R20 | **New, Phase 9:** `POST /api/policy` is a setting of the controller, not an environment control, so it is outside `/api/control/` and works in hardware mode. | **Agree.** |
| R21 | **New, Phase 10:** `GET /api/runs`, `/api/runs/{id}`, `/api/runs/{id}/export?kind=readings|events`; history off unless `IOT_HISTORY_DB` is set. | **Agree.** The dashboard needs a "download CSV" button per run and a note when `complete` is false. |
| R22 | The default `live-demo` scenario keeps the Phase 4 policy (`immediate_rtt`). Should the demo default to `lowest_delay`? | **Keep as is** until you have a policy selector in the UI; otherwise the default demo takes 15 s to move back to B after B returns. |

### 4.3 Radio protocol and node behaviour ([PROTOCOL.md](PROTOCOL.md))

| # | Question | Recommended answer |
| --- | --- | --- |
| R23 | Phase 1: 14-byte header with 32-bit session and sequence acceptable on the ESP8266, or should `seq` shrink? `value` as int32 hundredths fine for the sensor? Rejection-reason names and order practical to count? | **Agree all three.** The header cost is 14 of 250 bytes. Phase 8 may refine `value` without touching the header. |
| R24 | Two examples changed meaning (HELLO and ROUTE_UPDATE header-only frames are now `bad_payload_length`, files renamed `invalid-hello-empty-payload.json`, `invalid-route-update-empty-payload.json`). A firmware written against the Phase 1 examples must accept this. | **Agree.** The bytes are unchanged. |
| R25 | Phase 2 behaviour: the TTL rule (a relay drops a frame arriving with TTL 1), the permitted-neighbor rule, end-to-end retries with the same identity, the 64-number / 8-session duplicate window with re-ACK, the 16-frame queue (about 4 KB total). | **Agree; defer RAM to Phase 6.** |
| R26 | Phase 3: HELLO as unicast per neighbor, session-qualified neighbor lists for two-way, 1 s interval / 3 s timeout, flooding rules, about 0.7 KB. | **Agree.** |
| R27 | Phase 4a: is the RTT trailer easier than a wider entry for your packer; do `max_routes` 8 and the 5-sample RTT window fit; are the sequence numbers consumed by probes (about 6 to 7 per second at A) acceptable? Link cost is the **larger** of the two ends' RTTs and both must exist (the mean is the easy alternative). | **Agree** the trailer (it kept every Phase 3 byte). If probe numbers are a problem, probes can use a separate counter; say so. Keep "larger of two ends". |
| R28 | Phase 4b: the node-protecting backup rule and three-round install order; T1 and T2 (K = 3); `to_next_hop` 0 and reason 2 in LOCAL_FAILOVER_REPORT; report repeats (5, one per report interval). | **Agree.** K = 3 produces about 20 false failovers per simulated hour at 5 % loss on a link (table in the viva notes); tune K on hardware. |
| R29 | Phase 4b **hardware assumption**: the ESP8266 ESP-NOW send callback reports, in send order and after a short delay, whether the peer's radio acknowledged a unicast frame, and may say "failed" for a frame that arrived; `send_fail_ms` 30 ms is a placeholder; firmware must count consecutive failures per neighbor. | **Verify first thing in Phase 6.** If the callback is unreliable, T2 is dropped and only T1 remains (slower but safe). |
| R30 | Phase 4b decisions: nodes never switch back by themselves; the controller treats a failover report as "this link is gone until routable for the hold-down" (a false T2 costs one hold-down of the primary); expired information (silence) keeps the last route rather than announcing "no route". | **Agree.** |
| R31 | Phase 5a: TELEMETRY (0x17, 32 bytes, nine counters, flooded every 5 s): the right counters? Can C report the ESP-NOW sender (mapped to a node id) and the frame's TTL for each reading? | **Agree the counters; the second is a hard requirement** for the observed path, so verify it early. |
| R32 | **New, Phase 9:** the probe-statistics trailer on TOPOLOGY_REPORT (after the RTT trailer; the RTT trailer is a lone `0` byte when only statistics exist), a 20-outcome window per neighbor discarded while the neighbor is lost, the completed-probe rule (answered on reply, unanswered when the next probe replaces it). | **Agree.** About 0.05 KB of RAM: a 20-bit mask and a count per neighbor. |

### 4.4 Decisions that shape the hardware tests

| # | Question | Recommended answer |
| --- | --- | --- |
| R33 | Hardware controller-loss test is "stop the backend", never "unplug C's USB cable" (the cable powers C and the buffer is RAM-only). | **Agree.** Also a design limit: C is a single point of failure. |
| R34 | The laptop's epoch is its start time in seconds (so a restarted controller always has a larger epoch than the boards hold). | **Agree**; if the laptop clock is badly wrong the nodes answer `stale` and the controller advances its epoch by itself. |
| R35 | No authentication or encryption anywhere (laboratory prototype); the backend binds to localhost. | **Agree**; state it in the demo. |

---

## 5. How to run every demo

All commands from `backend/`. `../scenarios/` is the repository's scenario folder. Same commands on Windows and macOS; curl lines have PowerShell equivalents in [backend/README.md](../backend/README.md).

| What | Command | What to look for |
| --- | --- | --- |
| Tests and lint | `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` | All pass. |
| The live demo | `uv run python -m iot_backend` and, in another terminal, `uv run python -m iot_backend.watch --no-heartbeats` | Route A>B>C active, readings arriving. |
| Stop B, then start B | `POST /api/control/node` `{"node":"B","action":"stop"}` then `"start"` | `LOCAL FAILOVER A: B -> D` about a second after the stop; `ROUTE ACTIVE ... A>D>C`; later `LOST CONTACT with B`; after the start, a hold-down and back to B. |
| Controller outage and replay | `POST /api/control/gateway` `{"attached":false}`, stop B, wait 20 s, `{"attached":true}` | `GATEWAY LINK LOST`, nothing from the network, `GATEWAY LINK RESTORED`, `REPLAY START`, `REPLAYED reading ...`, the failover learned late, `REPLAY END`. |
| Choose a routing policy | `POST /api/policy` `{"policy":"highest_reliability"}` or `IOT_ROUTING_POLICY=lowest_delay` | `POLICY ... -> ...`; `GET /api/snapshot` shows `policy.current`. |
| Hardware mode with no board | `uv run python -m iot_backend.fake_c`; then `IOT_MODE=hardware IOT_SERIAL_PORT=socket://127.0.0.1:7777 uv run python -m iot_backend`; `uv run python -m iot_backend.watch` | `"mode": "hardware"`; type `stop B`, `unplug`, `replug` in the fake C's terminal. |
| Serial bring-up with a board | `uv run python -m iot_backend.serial_probe --list`, then `--port COM3` | `DATA` / `LOG` / `NOISE` / `BAD` lines. |
| C test vectors | `uv run python -m iot_backend.vectors` (`--check` to verify) | `tests/generated/test_vectors.h` is current. |
| Any simulator scenario | `uv run python -m iot_backend.simulator ../scenarios/b-failure-auto-reroute.json` | The scenario table in [backend/README.md](../backend/README.md) says what each of the 37 files shows. |
| Detection and recovery timing | `... ../scenarios/b-failure-auto-reroute.json --measure-seeds 50` | Min / median / max over 50 seeds. |
| Local failover versus controller-only | `... ../scenarios/local-failover-b-stops.json --compare-modes 50` | Three modes side by side. |
| False local failovers under loss | `... ../scenarios/no-false-failover.json --false-triggers 30 --duration 120` | Failovers per simulated hour for K = 2, 3, 4. |
| Controller outage study | `... ../scenarios/controller-outage-replay.json --outage-study 10` | Late versus lost readings by outage length. |
| **Policy comparison (Phase 9)** | `... ../scenarios/fast-lossy-vs-slow-reliable.json --compare-policies 30` | The table in [VIVA_NOTES.md](VIVA_NOTES.md). About a minute. |
| **Run history (Phase 10)** | `IOT_HISTORY_DB=history.sqlite3 uv run python -m iot_backend` (PowerShell: `$env:IOT_HISTORY_DB="history.sqlite3"`), run for a while, Ctrl+C, then `uv run python -m iot_backend.history list` and `... export 1 --out-dir exports` | `exports/run-1-readings.csv` and `run-1-events.csv`; also `GET /api/runs`. |
| Regenerate dashboard fixtures | `uv run python -m iot_backend.live.record` | Only after a deliberate shape change; a test fails if they are stale. |

---

## 6. Known limitations

Plain list, nothing hidden. Details are in each document's own limits section.

**Everything is simulation.** Delays, loss, service time (5 ms), timeouts and queue sizes are configured values. The simulator does not model radio collisions, range, RSSI, power quality or USB drivers. A simulation shows protocol logic, not radio behaviour.

**Never exercised on hardware:** the ESP-NOW send callback model (R29); whether opening the port resets C; the serial line under load; the RAM estimates; peer RSSI (no reliable ESP8266 peer-RSSI method is verified; the dashboard must show "unavailable", never an access-point RSSI); the real recovery times.

**Structural:** one flow (A to C); C is the only gateway and A the only source (single points of failure; C's loss is reported, not healed); a node cut off from every neighbor cannot report; a second failure before the controller replaces a route may leave no route; a backup protects only the failure the last route version anticipated.

**Backend behaviour worth knowing:**

- Delivery may pause and readings may be lost during recovery; there is no zero-loss promise. In simulation a B failure loses 0 to 1 readings (2 s interval) and shows a delivery gap of 3 to 6 s.
- C's buffer holds 50 entries: about 100 s of 2 s readings or 25 s of 500 ms readings; beyond that the oldest are dropped and **counted**. It is RAM only.
- Phase 9: the probe estimate is a round trip; the policies take 15 s (three 5 s evaluations) to move to a merely better route; with no loss `highest_reliability` can stay on the slower path (equal is not better); retries hide loss, so delivery rate differs by about one reading in a hundred while delivery time differs a lot.
- Phase 10: history is off by default; it records the controller's view; the last few rows of a killed run can be lost; `event_id` and `run_id` of the live run restart with each backend start (the history's own run ids do not).
- One live run is shared by everyone connected; presenter controls act on it for all viewers.
- Tested on Windows only; macOS is unverified until CI runs after push.

---

## 7. Hardware test plan for Phases 6 to 8

Authorization: Phase 6 onwards needs Harshal's explicit instruction and your firmware; nothing here has been started. Record **macOS and Windows results separately** and never write "tested" for what was not. The plan follows [plan.md](../plan.md) Phases 6 to 8; the recommended order inside each phase is the order below.

### 7.1 Before any radio: toolchain and contracts (Phase 6, bench, one board)

| # | Check | Pass |
| --- | --- | --- |
| 6.1 | PlatformIO builds the NodeMCU target on macOS and Windows; versions pinned in the repository. | Both build and flash. |
| 6.2 | The firmware's codec compiled with `test_vectors.h` passes every radio and serial vector. | 79 radio examples (29 valid, 50 invalid) and 48 serial lines all as stated. |
| 6.3 | Opening the port from the laptop does **not** reset C (R8), on both OSes, with DTR and RTS cleared as the backend does. | The board's uptime / boot id does not change across 20 open-close cycles. |
| 6.4 | `serial_probe` against C: `hello`, heartbeats answered by `st`, a wrong-checksum line refused, a 129-byte line dropped and counted, noise not parsed. | No `BAD` for good lines; every bad line counted by reason. |
| 6.5 | **Measure free heap** (`ESP.getFreeHeap()` and the minimum) on a node with the full tables and on C with its buffer full. | Fits with at least the margin you choose (suggest 10 KB spare); else reduce C's capacity first. |

### 7.2 Two boards (Phase 6 completion)

| # | Check | Pass |
| --- | --- | --- |
| 6.6 | Node identity: each board knows its id and permitted neighbors from per-node configuration; one firmware codebase. | Wrong-neighbor frames rejected and counted. |
| 6.7 | **ESP-NOW unicast between two boards**: DATA to C, DELIVERY_ACK back; encoded packets equal the reference examples byte for byte. | 100 readings delivered and acknowledged. |
| 6.8 | **Send callback characterisation (R29):** to a present peer, to an absent peer, with the peer's ACK suppressed if possible. Record how long failure takes to be reported and whether "failed" occurs for a delivered frame. | A written table; decide T2 and the real `send_fail_ms`. |
| 6.9 | C reports `via` (the ESP-NOW sender as a node id) and the TTL for every reading (R31). | Observed path correct for 100 readings. |
| 6.10 | Backend in hardware mode with two boards: route install through C, readings appear in the snapshot and events; presenter controls answer 409. | As in the fake-C run. |
| 6.11 | Serial throughput and loss: lines per second, `gateway.serial.lines` rejected counts, a deliberate burst; replay of 50 held entries (time it; replaces the simulated 20 ms). | No rejected line at normal load; replay time recorded. |
| 6.12 | Peer RSSI feasibility: can ESP-NOW peer RSSI be read reliably on the ESP8266? | Yes with a method, or "unavailable" recorded; never fabricated. |

### 7.3 Four boards (Phase 7)

| # | Check | Pass criterion / what to record |
| --- | --- | --- |
| 7.1 | Normal operation 10 minutes: A>B>C active, readings via B. | Observed path A>B>C; no direct A-C frame ever accepted. |
| 7.2 | **Unplug B, 10 times** (the main test; section 7.4 for the sheet). | Delivery restored through D each time; packet evidence shows both paths; record the gap, loss and times every time. |
| 7.3 | Unplug D while on B: the route is unchanged; D was A's backup, so A now has none and the controller shows the loss of contact. | No readings lost; no false failover. |
| 7.4 | Unplug both relays: "No available route"; bounded memory and retries; A drops with reason. | State reported honestly; return of a relay restores delivery. |
| 7.5 | B returns after a failover: no local switch-back; the controller restores B only after the hold-down. | Observed path stays D until the controller's route. |
| 7.6 | **Stop the backend (Ctrl+C), then unplug B** (never unplug C): A fails over locally to D; C buffers; restart the backend; replay without double counting. | Reading counts equal the sum of before, buffered and after; each reading once; `local_failover` shown late with its `received_ms`. Record local failover time separately from controller-driven recovery. |
| 7.7 | A restarts mid-run: new session, no confusion with old packets. | Old ACKs ignored; sequence reuse harmless. |
| 7.8 | C restarts (power-cycle): reported as a loss with a lower bound; nodes keep forwarding. | `uplink_loss` with `c_restarted`. |
| 7.9 | One direction of a link down (if you can do it in firmware with a deny list): shown `one_way`, not healthy. | Link state correct. |
| 7.10 | Quiet 30-minute soak: free heap trend, rejected counts, **false local failovers per hour** (compare with the 20 per hour at 5 % loss in simulation), probe loss. | Heap flat; false failovers recorded; K tuned if needed. |
| 7.11 | Policy check (optional on hardware): add a software-injected loss to the B path if the firmware supports it and compare `lowest_delay` against `highest_reliability`; **label any injected loss or delay as injected**. | Honest, labelled; do not falsify measured latency to force B. |

### 7.4 What to measure, and how

Definitions are those in [ARCHITECTURE.md](ARCHITECTURE.md) *Measurement rules*. Do not subtract timestamps from different boards to claim one-way latency.

| Measure | Definition | Reference and source |
| --- | --- | --- |
| Delivered readings | Unique readings at C by (source, session, sequence). | `readings.delivered_unique`, the readings CSV. |
| Loss | Readings generated but not received within the evaluation window; separate pending, retried and duplicates. | A's telemetry counters (with their age) and C's count. |
| Failure detection time | Failure reference to `node_lost_contact`. | **Reference** is the cut: use a laptop-controlled switch or relay if you can; otherwise press a key at the moment of the cut and record the reference error (about ±0.5 s by hand). State which. |
| Local failover time | Declared loss of the primary to the first confirmed delivery through the backup; separate from controller recovery. | `local_failover` event (use `received_ms`), then the first reading `via D`. |
| Controller reroute | Failure reference to `route_active` with trigger `topology_changed`. | The route events. |
| Service recovery and delivery gap | Failure reference to first restored delivery; last receipt before to first receipt after. | Consecutive `received_ms` in the readings CSV. |
| Link RTT and probe delivery | Per-link round trip on the prober's own clock; probe answered share. | `links[].rtt_ms`, `delivery_probability`. |
| Memory | Free heap per node, minimum over the run; C's buffer occupancy. | A debug `#` log line every 10 s. |
| Serial health | Lines rejected by reason, uplink losses, C restarts. | `gateway.serial`, `uplink_loss` events. |

**Recording the 10-trial unplug test.** Turn the run history on (`IOT_HISTORY_DB`), do the ten unplug-and-replug cycles in one run, export it (`uv run python -m iot_backend.history export N`), and fill one row per trial:

| Trial | Cut time (reference) | `local_failover` at | First reading via D | Delivery gap (s) | Readings lost | `route_active` v+1 at | B back, route restored at | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

Report min / median / max over the ten, in the same layout `--measure-seeds` prints for simulation, so the two can be compared; say plainly where the numbers differ and why. Exact recovery targets follow these measurements; none is promised in advance.

### 7.5 Phase 8: the real sensor

| # | Check | Pass |
| --- | --- | --- |
| 8.1 | One sensor on A replaces generated readings; generated readings remain available as an explicitly labelled test mode. | The label shows which is in use. |
| 8.2 | Real measurements reach C through both relay paths and continue after a relay failure. | Readings continue through the 10-trial test with real values. |
| 8.3 | A sensor failure is reported without stopping forwarding or heartbeats. | Unplug the sensor: HELLOs and probes continue, the failure is visible. |

### 7.6 What would make us stop and rethink

- The send callback cannot tell a delivered frame from an undelivered one: T2 goes, T1 stays, failover is slower (still correct).
- C's free heap cannot hold even 20 buffer entries: say so in the demonstration and shorten the outage test.
- Opening the port always resets C and cannot be fixed on the board: the controller must not be started while the demonstration is running, or use a different USB chip setting.
- Free heap shrinks over the soak: find the leak before anything else.
