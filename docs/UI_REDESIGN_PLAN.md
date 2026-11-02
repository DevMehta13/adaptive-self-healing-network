# Dashboard redesign plan (UI only)

Status: **complete** (U1 to U6 done on `harshal-dev`, 2026-11-01), ready for Dev's review. Approved by Harshal on 2026-11-01; phases were done one at a time, each ending with a review.

## Goal

Turn the single long scrolling page into five focused pages behind a slim top navigation bar, with a short heading on every page that says what the page shows and how to read it, an interactive time-series chart for the readings (uPlot), and a light/dark theme toggle. The information shown stays the same; it is reorganised and presented better.

## Rules for every phase

- **Frontend only.** Change files under `dashboard/` (and this plan, `docs/STATUS.md`). Do not change `backend/`, `firmware/`, the API, the WebSocket messages or any contract document. If a phase seems to need a backend or contract change, stop and report it instead.
- **Git:** work on `harshal-dev` only. Commit at the end of the phase with a clear message. **Never push, never merge, never switch to or commit on `main` or `dev-work`.**
- **Ownership:** `dashboard/` is Dev's area. This pass is agreed with him; each phase adds one dated line to `docs/STATUS.md` saying what changed in the UI.
- **Dependencies:** the only new runtime dependency allowed is `uplot` (Phase U3), pinned to an exact version like the existing ones. No router, UI kit, CSS framework or chart library besides uPlot.
- **Keep the honesty rules of the current UI:** the mode (simulation / hardware) is always visible; planned route and observed path stay separate; "lost contact", never "powered off"; replayed readings are labelled as replayed; simulation is never presented as hardware evidence. The texts in `docs/DASHBOARD_API.md` ("Labels the UI must show") still apply.
- **Checks before every commit:** `npm run check`, `npm test`, `npm run build` in `dashboard/` all pass. Add unit tests (Vitest) for new pure logic (route parsing, readings buffer, grouping of counters); no snapshot tests of markup.
- **Look at the result:** run it against the fake C in hardware mode and against the simulator (commands below), take screenshots of every page touched in that phase in light and dark theme (headless Chrome or Playwright, saved outside the repo), and look at them before reporting.

## How to test without boards

From `backend/` and `dashboard/`, in separate windows:

```powershell
# Hardware-mode screen, fed by the fake C (not real hardware)
uv run python -m iot_backend.fake_c
$env:IOT_MODE = "hardware"; $env:IOT_SERIAL_PORT = "socket://127.0.0.1:7777"; uv run python -m iot_backend
npm run dev

# Simulation-mode screen (presenter controls work)
Remove-Item Env:IOT_MODE -ErrorAction SilentlyContinue; uv run python -m iot_backend
npm run dev
```

Type `stop B` / `start B`, `link A->B down`, `unplug` / `replug` in the fake C window to cause changes; stop the backend for ~30 s and restart it to cause a replay.

## Shared design decisions

**Navigation bar** (sticky, slim, one row): brand on the left; page links `Overview · Network · Readings · Delivery · Saved runs` in the middle; on the right a mode pill (Simulation / Hardware / Mode unconfirmed), the connection state (Connected / Stale / Disconnected / Connecting, with a dot) and the theme toggle. The active page is highlighted and marked `aria-current="page"`. On narrow screens the links scroll horizontally or collapse into a menu; no horizontal page scroll.

**Routing:** hash routes, no library: `#/` (Overview), `#/network`, `#/readings`, `#/delivery`, `#/runs`. Unknown hash → Overview. Refresh keeps the page; browser back/forward work. Page `document.title` follows the page.

**One live connection for the whole app:** the existing `DashboardConnection` stays at the top of the app; pages receive its state. Switching pages never reconnects or loses data.

**Page header** (same component on every page): a short title, one line *what this page shows*, one line *how to read it*. Example: **Readings** — "Temperature from node A, as received by the controller." / "Each point is one reading. Hollow points were held at C while the controller was unreachable and replayed later."

**Theme:** colours become CSS custom properties on `:root`, with a dark set. Default follows the system (`prefers-color-scheme`); the toggle overrides it and is remembered in `localStorage` (wrapped in try/catch; the page works without it). Keep the current calm green look for light; dark must be equally readable (check contrast of text, pills, graph lines and chart).

**Removed or fixed:** the big hero headline ("See the network change course."), the separate "Connection status" card (its facts move to a small details popover or the Overview page), the "D2 · 03 / 12" learning row, "Coming later: Physical boards", the "Live simulation" breadcrumb, and the "ESP8266" footer (the boards are ESP32). Labels follow the actual mode.

## Pages

| Page | Content |
| --- | --- |
| **Overview** `#/` | The 30-second view. Current temperature tile (latest reading, its age, via which relay), current route (planned) and last observed path, a one-line status for each of A, B, C, D, three key numbers (created at A, delivered at C, lost in total), the last 3–5 network changes, and backend facts (version, run, snapshot time, last message). Each block links to its full page. |
| **Network** `#/network` | The diamond graph (larger), node cards, planned route vs observed path side by side, routing-policy selector, presenter controls (simulation) or the hardware note (hardware), and the "Recent network changes" timeline with readable times and event titles. |
| **Readings** `#/readings` | uPlot chart (below) and a cleaner table of recent readings: time, value, path, replayed / delay. |
| **Delivery** `#/delivery` | The counters as a flow: created at A → delivered at C → acknowledged at A, then the loss counters (lost at A, lost at C, lost in total, no route at A, delivered late), each with a one-line plain explanation and its age/source as today. |
| **Saved runs** `#/runs` | The existing recorded story ("Explore saved runs"), presented as a proper page. Built last. |

## The readings chart (uPlot)

- **Data:** a browser-side buffer of readings, kept at app level (shared by all pages). It is fed by `reading_received` events from the existing WebSocket (expose them from `DashboardConnection` through a listener; same messages, no protocol change, the documented ordering and dedup rules still apply) and by merging `readings.recent` from every snapshot/poll by identity, as a backstop. Identity is `(origin, session, seq)`: no duplicates. Capped at about 2,000 points. In simulation mode it is cleared when `run_id` changes or a `reset` arrives. In hardware mode it is kept across backend restarts (the controller-loss demo restarts the backend), because its time axis is the wall clock.
- **Network-change buffer:** likewise an app-level buffer of significant state-changing events (everything except `reading_received` and `telemetry`), seeded from the snapshot's `recent_events`, extended from the WebSocket, capped at about 500. The backend's own window of 100 events is mostly telemetry, so Overview's "last changes", the chart markers and the Network timeline all use this buffer instead.
- **X-axis:** in **hardware mode**, the laptop's wall-clock time (HH:MM:SS), computed from the snapshot's `server_time` and `clock.sim_ms` (an event at `t` happened at `server_time − (clock.sim_ms − t)`); this stays continuous across backend restarts. In **simulation mode**, the simulated clock (`sim_ms`, shown as mm:ss). A reading is placed at `received_ms`, the (estimated, for replayed readings) time C received it; the controller learned of it at `received_ms + delay_ms`. So replayed readings fill the outage gap. (Corrected after U3: the first version of this plan subtracted `delay_ms`, which misread the API.) The axis caption says which clock is used, and the tooltip shows both times for replayed readings. A `local_failover` with `replayed: true` is placed at `received_ms`, as the API requires.
- **Y-axis:** °C in hardware mode, "units" in simulation mode (as today). Auto range with padding and a minimum span of 2, so whole-degree DHT11 steps do not look like spikes.
- **Interaction:** hover crosshair and tooltip (value, time, path, live or replayed with delay); window buttons 1 min / 5 min / 15 min / All; Live (follows new data) and Paused (frozen for explaining); drag-to-zoom with a "Reset zoom" control.
- **Gaps:** the line is not drawn across a gap with no readings (more than about 3 reading intervals), so an outage that was not replayed is visible as a gap.
- **Encoding:** points via B and via D in two distinct colours (with a legend, and not by colour alone: the tooltip and table name the path); replayed readings as hollow points; thin vertical event markers with short labels for local failover, route change (`route_active` with a new path), node lost / regained contact, and controller link lost / restored (event names as in `docs/DASHBOARD_API.md`).
- **Housekeeping:** follows the theme (re-create or re-style on toggle), resizes with its container, destroyed on unmount, no growth of memory over a long run.

## Phases

### U1 — Shell, navigation, routing, theme

- Top bar with navigation, mode pill, connection state and theme toggle; hash routing; page header component; theme tokens with light and dark sets.
- Move the existing sections onto their pages **without redesigning them yet** (graph, controls, route panel, events → Network; counters → Delivery; readings list and sparkline → Readings; saved runs → Saved runs; Overview can start as a simple placeholder summary).
- Remove/fix the stale items listed above.
- Done when: every page is reachable from the bar and by URL, nothing that was shown before is lost, both themes are readable, checks pass, screenshots taken.

### U2 — Overview and Delivery pages

- Build Overview as described (tiles, route, node status line, key numbers, last changes, backend facts, links to pages).
- Build Delivery as a flow with grouped loss counters and plain explanations.
- Done when: an examiner can understand the current state from Overview alone in under a minute, in both modes.

### U3 — Readings page with uPlot

- Add `uplot` (exact version), the readings buffer (with unit tests), the chart with all interactions and encodings above, and the recent-readings table.
- Done when: in the fake C, `stop B` shows the colour change from B to D with a failover marker, and stopping/restarting the backend shows hollow replayed points filling the outage gap; works in both themes and modes.

### U4 — Network page

- Larger, clearer graph layout; node cards; planned vs observed side by side; policy selector and controls/hardware note in a tidy panel; readable event timeline (relative time, icon or tag per event kind, presenter actions marked).
- Done when: a `stop B` → failover → `start B` sequence reads clearly on this page alone.

### U5 — Polish across pages

- Narrow-screen layout (about 375 px wide) for every page, keyboard focus states, empty / connecting / stale / disconnected states on every page, consistent spacing and typography, dark-theme contrast audit.
- Done when: no page has horizontal scroll at phone width, and every page makes sense while the backend is down.

### U6 — Saved runs page and wrap-up

- Present the saved-runs story as a proper page in the new design (works offline, as today).
- Final screenshots of all five pages in both themes and both modes; update `dashboard` notes in `README.md` if they describe the old layout; final `docs/STATUS.md` entry; mark this plan complete.
- Done when: all phases are checked off below and the branch is ready for Dev's review.

## Progress

- [x] U1 — Shell, navigation, routing, theme
- [x] U2 — Overview and Delivery
- [x] U3 — Readings with uPlot
- [x] U4 — Network page
- [x] U5 — Polish across pages
- [x] U6 — Saved runs and wrap-up

## Report expected at the end of each phase

Commit hash (not pushed); files changed; output of `npm run check`, `npm test`, `npm run build`; where the screenshots are; what was checked against the fake C and the simulator; anything left undone or any decision made that this plan did not cover.

## Addition (2026-11-02): Packets page

Approved by Harshal on 2026-11-02. Two phases, backend first. The hardware check with the real boards happens once, after everything is done.

**Goal.** A sixth page, **Packets** (`#/packets`, nav order: Overview · Network · Readings · Packets · Delivery · Saved runs), that shows the 14-byte radio header we designed, using the real frames node C hands up to the laptop, data and non-data alike.

**Honest scope (shown on the page).** Only frames **handed up by C** are shown: DATA that C accepts (new or repeated), and the TOPOLOGY_REPORT, ROUTE_INSTALL_ACK, LOCAL_FAILOVER_REPORT and TELEMETRY addressed to C. HELLO, PROBE, PROBE_REPLY, DELIVERY_ACK, controller-originated frames and any hop between other boards are never seen by the laptop. Version, type and payload length are *decoded from the bytes*, not measured. Simulation and hardware show the same shape; in simulation the bytes come from the simulator's codec and are labelled so.

### P1 — Backend: frame records in the API (Harshal's area)

- For every frame handed up by C (both modes, also during replay), build a frame record: C's uplink number `n`; time on the controller clock (for replayed frames the estimated time C got it, as for readings) plus `delay_ms` / `replayed`; `via`; `dup` (DATA only); `hex` (the exact bytes); `size`; the decoded header (`version`, `type`, `type_name`, `src`, `dst` as node names, `session`, `seq`, `ttl`, `payload_len`); and a short decoded payload summary (DATA value in hundredths and units, telemetry counters, topology neighbours, route version, failover from/to/reason). Lines rejected as `bad_frame` are kept as records with the reason and the hex, if available.
- Expose them **without disturbing the state-changing event history** (the 100-event `recent_events` window is already crowded): a non-state-changing WebSocket message type (like `heartbeat`, no `event_id`) and a bounded `frames` section in the snapshot (recent ~200 records, counts by type, duplicates, rejected). Additive only: existing fields, events and dashboard fixtures stay valid.
- Document it in `docs/DASHBOARD_API.md` (new section, marked additive) and `docs/STATUS.md`; tests for building records from the reference vectors in `tests/protocol-examples/` and `tests/serial-examples/`, for bounds, for replay and duplicates, and for both modes.

### P2 — Frontend: the Packets page

- **Anatomy strip:** the 14 header bytes as labelled, coloured cells (version · type · src · dst · session ×4 · seq ×4 · ttl · payload_len) followed by the payload; selecting a frame fills the cells with its values and shows the raw hex below, field bytes highlighted on hover (little-endian noted for session and seq).
- **Frame log:** newest first, columns time · type · src → dst · session · seq · TTL · size · via · flags (dup, replayed, rejected); filter by type; pause/follow live; app-level buffer like the readings (cap ~1,000, same clearing rules per mode).
- **Facts panel derived from real frames:** TTL on arrival and hops (data TTL 8; one relay → TTL 7 at C, two transmissions), a session change marks a reboot, duplicates caught by the identity `(src, session, seq)`, replayed frames with their delay.
- **Summary strip:** counts by type, duplicates, rejected, TTL distribution.
- **Delivery acknowledgements (not observed):** DELIVERY_ACK frames are created by C and sent back to A, so the laptop never sees them. Show (a) the DELIVERY_ACK layout from `docs/PROTOCOL.md` (14-byte header + 9-byte payload naming the reading it confirms), labelled "format, not observed by the laptop", and (b) the indirect evidence: A's acknowledgement counter from its TELEMETRY frames, linking to the frames that carry it.
- Page header states the honest scope above. Both themes, 375 px, keyboard access, empty/stale/disconnected states like the other pages.

### Progress (Packets)

- [x] P1 — Backend frame records
- [ ] P2 — Packets page
