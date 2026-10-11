# Current status

Last updated: 2026-10-11

## Project state

**Phase 0 is half done (the backend half exists; the dashboard half is Dev's and has not started). Phase 1 and Phase 2 are done on the backend side.** The agreed project uses four ESP8266 NodeMCU boards. Hardware has not been purchased; no radio, USB, sensor, dashboard, or controller exists, and no hardware test has been performed. The running code is the backend under `backend/`: a status-only API, the protocol codec, and a four-node simulator with manual routing, forwarding, retries and duplicate detection (no route calculation or failure detection).

### Ownership split (agreed between the owners)

| Owner | Area |
| --- | --- |
| Harshal | `backend/` (protocol, simulator, controller, transports, api, storage) and backend tests |
| Dev | `dashboard/` (React, TypeScript, Vite, Cytoscape) and `firmware/` (ESP8266) |

The two sides meet only through written contracts. The first is [docs/DASHBOARD_API.md](DASHBOARD_API.md), currently **Draft — pending Dev's review**.

## Completed

- Created the public repository `DevMehta13/adaptive-self-healing-network`.
- Agreed on node roles, the relay-failure demonstration, and laptop-based control.
- Selected ESP8266 NodeMCU after considering ESP32-C3.
- Recorded the budget preference and macOS/Windows collaboration requirement.
- Added scope, architecture, contribution guidance, and common coding-agent instructions.
- Reviewed and approved the software-first phased plan in `plan.md`: working simulation through Phase 5, hardware integration next, then sensor, adaptive routing, and final polish.
- 2026-10-11: Both owners approved two resilience additions after reviewing single points of failure: controller-computed backup next hops with local fast-failover, and fail-static forwarding with bounded buffering at C while the controller is unreachable. Updated `plan.md` (amendment note, Phases 4–7 and 9, gateway interface, test scenarios, assumptions), `docs/ARCHITECTURE.md` (new *Local failover and controller loss* section, workflow, measurements, boundaries), `README.md`, and `AGENTS.md`.
- 2026-10-11: **Phase 0, backend half** (authorized by Harshal, branch `harshal-dev`). Added `backend/` (FastAPI, uv with a committed `uv.lock`, env-var configuration with `.env.example`), `GET /health`, `GET /api/snapshot`, WebSocket `/ws` with a versioned envelope (status event on connect, then heartbeats), the draft contract `docs/DASHBOARD_API.md`, pytest and ruff checks, a GitHub Actions workflow for Windows and macOS, and `backend/README.md`.

- 2026-10-11: **Phase 0 follow-ups** (authorized by Harshal): `/ws` rejects disallowed browser origins; heartbeats are paced by time only; `docs/DASHBOARD_API.md` gained `event_id` / `last_event_id` ordering (and `last_event_id` is in `/api/snapshot`); stale phase-specific wording removed from `plan.md`.
- 2026-10-11: **Phase 1, backend side** (authorized by Harshal):
  - `docs/PROTOCOL.md` (draft): compact binary frame, 14-byte little-endian header, 250-byte limit, DATA and DELIVERY_ACK, reserved management codes, ordered decoding rules.
  - `tests/protocol-examples/`: 20 shared reference cases (hex plus fields) for Python now and the firmware later.
  - `backend/src/iot_backend/protocol/`: message classes, `encode()`/`decode()`, rejection reasons.
  - `backend/src/iot_backend/simulator/`: virtual clock, seeded directional links (delay, random loss, scripted drops), virtual A and B exchanging encoded bytes, event trace, CLI demo.
  - 64 new tests covering the required scenarios (see Verification record).

- 2026-10-11: **Phase 1 review fixes and Phase 2, backend side** (authorized by Harshal):
  - Fixes: nodes receive the link-layer sender (`receive(data, from_node)`); the summary comes from node/link counters, separating readings created from frames sent; `stop_node` / `start_node` with RAM loss replace instant restart.
  - Four virtual nodes A-D. All boards can physically hear each other; the diamond A-B, B-C, A-D, D-C is enforced by each node's permitted-neighbor list (not by leaving links out).
  - Manual routing tables with a runtime `set_route`; `no_route` drops.
  - Forwarding with a TTL rule, only C acknowledges, observed paths rebuilt from received-frame evidence.
  - End-to-end retries from A (same identity, at most 3 attempts), bounded wrap-safe duplicate detection at C with re-ACK, bounded send queues with `queue_full` drops.
  - Failure controls: node stop/start, directional link down, runtime delay/loss changes.
  - Six strict JSON scenarios in `scenarios/`, and a CLI that runs them and prints trace, observed paths and a drops-by-reason summary.
  - Behavioural rules added to `docs/PROTOCOL.md`; the frame header and byte layout, `protocol/` code and `tests/protocol-examples/` are unchanged.

## Current authorization and next step

Harshal authorized **Phase 0 (backend half), its follow-ups, Phase 1 (backend side), and Phase 2 (backend side)**. Nothing beyond that is authorized. Phase 3 (HELLO/discovery, neighbor-loss timeouts, management flooding), the controller, Dijkstra, backup next hops and dashboard events have **not** been started; plan approval is not implementation authorization.

**Needs Dev's review (nothing here is frozen until he approves):**

- [docs/DASHBOARD_API.md](DASHBOARD_API.md): new `event_id` / `last_event_id` rule, Origin check, 3-interval stale rule.
- [docs/PROTOCOL.md](PROTOCOL.md) and `tests/protocol-examples/`: header size, 32-bit session/sequence, `value` encoding, rejection-reason names and order. The firmware encoder must reproduce every `valid-*` example and reject every `invalid-*` one.
- The new *Forwarding and delivery behaviour* section of `docs/PROTOCOL.md` (no format change): the TTL rule (a relay drops a frame that arrives with TTL 1), the permitted-neighbor rule, end-to-end retries with the same identity, the 64-number / 8-session duplicate window and re-ACK, and the default queue of 16 frames. Can the ESP8266 hold these (about 8 sessions x 8 bytes of bitmap plus a 16-frame queue of up to 250 bytes = 4 KB)? Confirm against measured free heap in Phase 6.

**Phase 0 remaining:**

- Dev builds the dashboard shell (Simulation mode label, backend connection status) against the reviewed API contract.
- Phase 0 is complete only when Dev's shell connects to the backend from a fresh checkout on both operating systems.
- macOS verification of the backend (including the simulator and protocol tests) is pending until CI runs after push.

**Next step:** Phase 3 — HELLO messages, neighbor observations with freshness timers, topology reports and bounded management flooding, so the controller can discover that B is gone from received reports and timeouts. It needs Harshal's explicit authorization first.

**Layout note:** `plan.md` lists a top-level `tests/` folder. Backend tests live in `backend/tests/` so the backend can be tested on its own; the top-level `tests/protocol-examples/` holds the shared protocol examples that both Python and firmware check.

**Design decisions recorded in Phase 1:** radio encoding is binary (not JSON); a session id is random per boot and `0` is reserved; packet identity is (`src`, `session`, `seq`); simulator time is integer milliseconds with a per-link seeded generator, so scripting a drop does not disturb other random choices; the simulator keeps its internals private, so the later controller must use a gateway interface fed by events.

## Open items

| Item | Why it remains open |
| --- | --- |
| Complete shopping cost | Need exact seller, delivery/tax, available cables/power, and whether accessories are included in the stated budget |
| Sensor and wiring | Purchased later; model and required connections not selected |
| Physical hardware sharing | Need to arrange access to all four boards for integration; simulation is planned for independent software work |
| Firmware/toolchain versions | Must validate ESP8266 ESP-NOW and serial behavior on actual boards |
| Peer RSSI | Reliable measurement method on the chosen ESP8266 framework is unverified |
| Wire protocol and control transport | Frame header, DATA and DELIVERY_ACK are specified (draft, pending Dev's review). Management messages (HELLO, reports, route updates, probes, failover reports) and bounded flooding are reserved codes only; specify them in Phases 3–4 |
| Phase 2 simulator limits | Routes are manual and nothing detects a failed node, so a stopped relay means lost readings (shown by `b-stopped-no-route-change.json`). ACKs return through the routing tables; plan.md moves them to bounded management flooding in Phase 3. A reading reported *lost* may have been delivered when only its ACKs failed. Late ACKs are ignored. The duplicate window (64 numbers, 8 sessions) is bounded: a very late retry is refused, and a long-forgotten session could be counted again |
| Simulated timing | Link delay (20 ms), service time (5 ms), queue size (16), ACK timeout (1 s) and attempts (3) are configured defaults, not measurements; hardware must set them |
| Route installation | Versioned updates with relay installation before activation at A are planned; failure handling still requires specification and tests |
| Demonstration route policy | Need repeatable selection of B without misrepresenting measured costs |
| Timers and metrics | Initial HELLO, timeout, traffic, TTL, retry, and adaptive-policy settings are recorded in the plan; hardware validation and remaining evaluation details are pending |
| Local failover trigger | Initial trigger is the primary neighbor's HELLO timeout; whether missed delivery acknowledgements should also trigger it needs simulation results |
| C buffer and controller heartbeat | The 50-entry buffer and 3-second controller timeout are proposals; buffer size must fit measured ESP8266 free heap |
| Remaining single points of failure | C (only gateway) and A (only source) remain. Dual gateways, a Wi-Fi backup link, a standby controller, and distributed routing are future work |
| Application libraries | Backend dependencies are locked in `backend/uv.lock`; dashboard and firmware libraries are not yet chosen or locked (Dev) |
| Dashboard API contract | Draft pending Dev's review. `event_id` is not persisted across backend restarts (see the draft's known limits) |
| Test-client warning | Starlette's test client warns that `httpx` is deprecated in favor of `httpx2`; harmless, revisit when dependencies are updated |

## Verification record

- 2026-10-11, **Windows 11, Python 3.11.0, uv 0.13.0** (Phase 1 review fixes and Phase 2; macOS **not** run; CI not run, nothing pushed): `uv run ruff check .` and `uv run ruff format --check .` passed; `uv run pytest` passed **117 tests**. They cover: the receive-with-sender, counter-based summary and stop/start fixes; normal A>B>C delivery with observed paths for DATA and ACK (relays never ACK); manual switch to D; `no_route`; direct A-to-C bypass rejected by C although the radio reached it, and a never-sent frame when A's own list forbids C; frames from a non-permitted neighbor rejected by a relay; nothing reaching C except through B or D under random loss (3 seeds); lost ACK then retry with the same identity, counted once and re-ACKed; three lost attempts then *lost* with no further sends; late ACK ignored; routing loop ended by TTL and a precise TTL-1 rule test; B stopped with routes unchanged then restarted; restarted A with a new session and an in-flight old ACK ignored; one-direction link failure in each direction; runtime delay change; queue overflow bounded and counted; same seed gives identical traces and different seeds differ; duplicate detection across the 2^32 wrap (unit and simulation); malformed frames; scenario files load strictly, run and are repeatable; the CLI. As a check on the tests themselves, three deliberate breakages (duplicate filter disabled, receive-side neighbor check removed, retries given new sequence numbers) were each caught and then reverted. These are simulation results only; they say nothing about radio behaviour, timing on ESP8266 boards, or the firmware, which does not exist.
- 2026-10-11, **Windows 11, Python 3.11.0, uv 0.13.0** (Phase 0 follow-ups and Phase 1; macOS **not** run; CI not run, nothing pushed): `uv run ruff check .` and `uv run ruff format --check .` passed; `uv run pytest` passed **74 tests**. These cover the Origin check and time-only heartbeats; every file in `tests/protocol-examples/` (encode equals bytes, decode equals fields, invalid files rejected with the stated reason); round-trip, boundary and reserved-type tests; and the simulator scenarios: normal delivery, dropped DATA, dropped ACK (delivered but unacknowledged), same-seed identical trace and different-seed different losses, malformed frames rejected while the run continues, and an A restart with a new session and restarted sequence numbers. The CLI demo (`uv run python -m iot_backend.simulator --seed 42 --duration 12 --drop A-B:2 --drop B-A:4 --malformed-at 5 --restart-a 9`) was also run and read by hand. These are simulation and codec results only; they say nothing about radio behavior or the firmware, which does not exist. The reference examples were generated with an independent hand-written packer and their hex checked against the codec, but no second implementation (C++) has yet confirmed the format.

- 2026-10-11, **Windows 11, Python 3.11.0, uv 0.13.0** (backend only; no macOS run): `uv sync`; `uv run ruff check .` and `ruff format --check .` passed; `uv run pytest` passed 7 tests (health, snapshot, CORS, WebSocket status event, envelope shape, increasing `seq`, heartbeat, `seq` restart on reconnect). Started the real server with `uv run python -m iot_backend` and a separate Python `websockets` client received `/health`, `/api/snapshot`, the `status` event and two heartbeats about 2 s apart. The CI workflow has **not** run yet (not pushed); macOS is unverified. Simulation-mode status plumbing only; nothing network-related is measured.

- 2026-10-11, Linux shell against the Windows checkout: after the resilience amendment, checked all Markdown files for valid local links, balanced code fences, consistent table columns, trailing whitespace, and LF line endings; `git diff --check` passed. Documentation checks only; no behavior was implemented or tested.
- Approved-plan handoff, macOS: verified all 11 phases (0–10), local Markdown links, code fences, and diff whitespace. Confirmed that no application scaffolding or separate roadmap was created.
- 2026-09-07, macOS: checked all seven Markdown files for valid local links, balanced code fences, and trailing whitespace; checks passed. Confirmed that no deferred roadmap file exists.
- Reviewed the documents for consistent ESP8266 hardware selection, budget wording, implementation status, and honest measurement/recovery limits.
- No runtime or hardware results exist. Documentation checks do not verify project behavior. Future entries must state the tested platform/hardware, results, and remaining gaps.

## Keeping this file useful

After a meaningful contribution, update completed work, current state, relevant verification, and next steps. Link to supporting code or decisions rather than pasting chat transcripts. Keep unresolved issues visible. Git history preserves older handoffs; this page should describe the current state.
