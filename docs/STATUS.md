# Current status

Last updated: 2026-10-11

## Project state

**Phase 0 is half done: the backend half exists; the dashboard half is Dev's and has not started.** The agreed project uses four ESP8266 NodeMCU boards. Hardware has not been purchased; no radio, USB, sensor, controller, dashboard, simulator, or routing test has been performed. The only running code is the status-only backend under `backend/`.

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

## Current authorization and next step

Harshal authorized **Phase 0, backend half only**. Nothing beyond Phase 0 is authorized; plan approval is not implementation authorization.

**Phase 0 remaining:**

- Dev reviews [docs/DASHBOARD_API.md](DASHBOARD_API.md) and builds the dashboard shell (Simulation mode label, backend connection status) against it.
- Phase 0 is complete only when Dev's shell connects to the backend from a fresh checkout on both operating systems.
- macOS verification of the backend is pending until CI runs after push.

**Next step after Phase 0:** Phase 1 — the shared protocol contract and a two-node simulator (virtual A and B, numbered readings, acknowledgement, a deliberately dropped packet).

**Layout note:** `plan.md` lists a top-level `tests/` folder. Backend tests live in `backend/tests/` so the backend can be tested on its own; top-level `tests/` is left for shared protocol examples when Phase 1 introduces them.

## Open items

| Item | Why it remains open |
| --- | --- |
| Complete shopping cost | Need exact seller, delivery/tax, available cables/power, and whether accessories are included in the stated budget |
| Sensor and wiring | Purchased later; model and required connections not selected |
| Physical hardware sharing | Need to arrange access to all four boards for integration; simulation is planned for independent software work |
| Firmware/toolchain versions | Must validate ESP8266 ESP-NOW and serial behavior on actual boards |
| Peer RSSI | Reliable measurement method on the chosen ESP8266 framework is unverified |
| Wire protocol and control transport | Bounded management flooding is planned; encoding and bounds must be specified and tested in the relevant phases |
| Route installation | Versioned updates with relay installation before activation at A are planned; failure handling still requires specification and tests |
| Demonstration route policy | Need repeatable selection of B without misrepresenting measured costs |
| Timers and metrics | Initial HELLO, timeout, traffic, TTL, retry, and adaptive-policy settings are recorded in the plan; hardware validation and remaining evaluation details are pending |
| Local failover trigger | Initial trigger is the primary neighbor's HELLO timeout; whether missed delivery acknowledgements should also trigger it needs simulation results |
| C buffer and controller heartbeat | The 50-entry buffer and 3-second controller timeout are proposals; buffer size must fit measured ESP8266 free heap |
| Remaining single points of failure | C (only gateway) and A (only source) remain. Dual gateways, a Wi-Fi backup link, a standby controller, and distributed routing are future work |
| Application libraries | Backend dependencies are locked in `backend/uv.lock`; dashboard and firmware libraries are not yet chosen or locked (Dev) |
| Dashboard API contract | Draft pending Dev's review; snapshot/event ordering marker and WebSocket origin check are open (see the draft's known limits) |
| Test-client warning | Starlette's test client warns that `httpx` is deprecated in favor of `httpx2`; harmless, revisit when dependencies are updated |

## Verification record

- 2026-10-11, **Windows 11, Python 3.11.0, uv 0.13.0** (backend only; no macOS run): `uv sync`; `uv run ruff check .` and `ruff format --check .` passed; `uv run pytest` passed 7 tests (health, snapshot, CORS, WebSocket status event, envelope shape, increasing `seq`, heartbeat, `seq` restart on reconnect). Started the real server with `uv run python -m iot_backend` and a separate Python `websockets` client received `/health`, `/api/snapshot`, the `status` event and two heartbeats about 2 s apart. The CI workflow has **not** run yet (not pushed); macOS is unverified. Simulation-mode status plumbing only; nothing network-related is measured.

- 2026-10-11, Linux shell against the Windows checkout: after the resilience amendment, checked all Markdown files for valid local links, balanced code fences, consistent table columns, trailing whitespace, and LF line endings; `git diff --check` passed. Documentation checks only; no behavior was implemented or tested.
- Approved-plan handoff, macOS: verified all 11 phases (0–10), local Markdown links, code fences, and diff whitespace. Confirmed that no application scaffolding or separate roadmap was created.
- 2026-09-07, macOS: checked all seven Markdown files for valid local links, balanced code fences, and trailing whitespace; checks passed. Confirmed that no deferred roadmap file exists.
- Reviewed the documents for consistent ESP8266 hardware selection, budget wording, implementation status, and honest measurement/recovery limits.
- No runtime or hardware results exist. Documentation checks do not verify project behavior. Future entries must state the tested platform/hardware, results, and remaining gaps.

## Keeping this file useful

After a meaningful contribution, update completed work, current state, relevant verification, and next steps. Link to supporting code or decisions rather than pasting chat transcripts. Keep unresolved issues visible. Git history preserves older handoffs; this page should describe the current state.
