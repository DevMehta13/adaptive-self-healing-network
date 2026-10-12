# Dashboard API contract

**Draft — pending Dev's review.** Written by Harshal (backend owner) for Dev (dashboard owner). Nothing here is frozen until Dev approves it; change requests go in a pull request to this file. Everything below is **implemented and tested** as of Phase 5a, in simulation mode only. Anything not implemented is listed under *Not implemented yet*, not described as if it were.

Recorded examples of every message live in [tests/dashboard-fixtures/](../tests/dashboard-fixtures/); a test keeps them valid against the current shapes, so you can build and test the UI without running the backend.

Base URL in development: `http://127.0.0.1:8000` (HTTP) and `ws://127.0.0.1:8000/ws` (WebSocket). Host and port are configurable (see [backend/README.md](../backend/README.md)).

## The one rule: what is shown is what the controller knows

The backend runs a simulated network in the background. The dashboard is shown **what the controller and node C have learned**, never the simulator's ground truth:

| Shown to the dashboard | Who knows it | How it got to the backend |
| --- | --- | --- |
| Node status (`reachable`, `lost_contact`, `stale`, `unknown`) and link state | the controller | topology reports that reached C, with freshness |
| The calculated route, its state (`installing`, `active`, `unconfirmed`...), its backup next hops | the controller | its own computation and acknowledgements |
| Recovery events (local failover vs controller reroute) | the controller | failover reports from nodes, its own detection |
| Each reading received, with `via` (last-hop relay) and `hops` | node C | link-layer sender and TTL of the frame, observed at C |
| Created / acknowledged / lost / no-route counts | node A (and other nodes) | TELEMETRY messages flooded to C, labelled with their age |
| What the presenter did to the environment | the presenter | the separate **`environment` channel**, below |

The *observed packet path* is built **only** from C's own evidence. C knows which neighbor handed it a frame and how much TTL was left; it does not know the relays further back. Unknown relays are `null` in `observed_path`. It is never taken from the simulator's trace and never from the planned route.

Animations that draw the *planned* route (`route.path`) and the *observed* path (`reading_received.observed_path`) must stay visually distinct, and must say which is which.

## Labels the UI must show

- The snapshot's `label` string ("SIMULATION: ... Link delays are configured settings, not measurements").
- Link delays and costs are **simulated settings**: B's links are configured cheaper than D's so that B is the normal relay.
- Recovery times are simulated time, not hardware.
- Counts that come from telemetry carry `known_by` and `age_ms`; show the age.

## Environment channel (separate on purpose)

Presenter actions ("Stop B", "Take down A→B", "Detach the gateway") are published as `environment` events and listed under `snapshot.presenter`. They are **not** part of the controller's knowledge: the controller finds out later, through the gateway, or never. Showing them next to the controller-view events lets a viewer read *what we did* beside *what the network noticed* (for example `environment stop_node B` at 12.000 s, then `local_failover` at 12.99 s, then `node_lost_contact` at 14.52 s). Mark them visually as injected; each carries `source` (`presenter`, or `scenario` if a scenario file scripted it) and a ready-made `label`.

## HTTP endpoints

### `GET /health`

```json
{ "status": "ok", "mode": "simulation", "version": "0.1.0" }
```

### `GET /api/snapshot`

The complete current state; the source of truth. WebSocket events only describe changes after it.

```json
{
  "v": 1,
  "mode": "simulation",
  "backend": { "status": "ok", "version": "0.1.0" },
  "server_time": "2026-10-13T09:30:00.123Z",
  "run_id": 1,
  "scenario": "live-demo",
  "label": "SIMULATION: nodes, links and delays are simulated. ...",
  "last_event_id": 154,
  "clock": { "sim_ms": 106559, "speed": 1.0, "paused": false },
  "nodes": [ ... ],
  "links": [ ... ],
  "route": { ... },
  "readings": { "delivered_unique": 49, "duplicates": 0, "recent": [ ... ] },
  "counts": { ... },
  "presenter": { ... },
  "recent_events": [ { "event_id": 150, "type": "reading_received", "data": { ... } } ]
}
```

| Field | Meaning |
| --- | --- |
| `run_id` | Increases when the simulation is reset to a scenario. A change means "replace all state". |
| `scenario` | Name of the scenario file in `scenarios/` that is running. |
| `last_event_id` | Id of the newest state-changing event already reflected in this snapshot. |
| `clock.sim_ms` | Simulated milliseconds since the run started. Every event's `data.sim_ms` is on this clock. |
| `clock.speed` / `paused` | Pacing against the wall clock (speed 1.0 = real time). |
| `recent_events` | The last 100 state-changing events, oldest first, for a timeline. Bounded. |

#### `nodes[]`

Always four entries, A to D. A node nobody has reported on has `status: "unknown"`.

```json
{
  "id": "B",
  "status": "lost_contact",
  "since_ms": 14521,
  "session": 3170526542,
  "last_report_age_ms": 8980,
  "source": { "kind": "neighbor_reports", "observed_by": ["A", "C"], "silent_for_ms": 4632 },
  "telemetry": {
    "session": 3170526542,
    "counters": { "created": 0, "acknowledged": 0, "lost": 0, "no_route": 0,
                  "delivered": 0, "forwarded": 25, "dropped": 1, "failovers": 0, "send_failures": 0 },
    "received_ms": 9722,
    "age_ms": 10278,
    "known_by": "B (own counters, delivered by flooding)"
  }
}
```

| Field | Meaning |
| --- | --- |
| `status` | `reachable`: its own report is fresh. `lost_contact`: every neighbor with a fresh report lists it as lost. `stale`: its reports expired and nothing newer exists. `unknown`: never heard. There is deliberately **no "powered off"**: a timeout cannot prove that. |
| `since_ms` | Simulated time the status last changed. |
| `session` | The node's own latest session id; a change means it restarted. |
| `last_report_age_ms` | Age of its latest topology report, or `null`. |
| `source` | Present only for `lost_contact`: who observed it and for how long. Show it next to the status. |
| `telemetry` | The node's own counters (cumulative since its boot), or `null` before the first message. `age_ms` is how old they are: a stopped node's counters get older. |

#### `links[]`

Only links that have been reported on appear.

```json
{
  "a": "A", "b": "B", "state": "up", "usable": true,
  "directions": { "B->A": true, "A->B": true },
  "since_ms": 1552,
  "rtt_ms": { "A": 30, "B": 30 },
  "cost_ms": 30
}
```

| Field | Meaning |
| --- | --- |
| `state` | `up`: both ends report two-way with fresh reports. `one_way`: only one direction is confirmed. `down`: neither direction is heard, or an endpoint is in lost contact. `stale`: an end's report has expired. |
| `usable` | `true` only for `up`. |
| `directions` | `"X->Y": true` means frames sent by X are confirmed to reach Y. |
| `rtt_ms` | The round trip each end **measured on its own clock**, in ms; `null` for an end with no fresh measurement. A round trip, not a one-way delay. The values are the last reported ones: meaningful when `state` is `up`. |
| `cost_ms` | What route calculation uses (the larger of the two ends, only if both measured and the link is `up`), else `null`. A link with `null` cost is not used and is never "free". |

Draw `one_way` and `stale` differently from `up` (for example dashed), and never draw a planned link as healthy without this evidence.

#### `route`

```json
{
  "flow": "A->C", "epoch": 1000, "version": 2, "kind": "route", "state": "active",
  "path": ["A", "D", "C"], "cost_ms": 130,
  "confirmed_by": ["D", "A"], "waiting_for": [],
  "backups": { "A": "D" }, "backup_entries": { "D": "C" }
}
```

| Field | Meaning |
| --- | --- |
| `kind` | `route`, or `no_route` when the controller has declared that no path exists. |
| `state` | `waiting` (no plan yet), `computed`, `installing`, `active`, `failed` (a node refused), `unconfirmed` (acknowledgements missing after bounded retries), `superseded`. **Only `active` may be drawn as the current route.** With no controller (a manual-route scenario) it is `no_controller`. |
| `path` | The **plan**. Keep it apart from observed paths. Empty for `no_route`. |
| `confirmed_by` / `waiting_for` | Nodes whose installation has / has not been acknowledged. |
| `backups` | Each forwarding node on the route and its installed backup next hop (empty if none). |
| `backup_entries` | Nodes **off** the route that were given an entry so a backup works. |

A node that failed over locally is using its backup even though `route.path` still shows the primary, until the controller's new version is active. The dashboard learns of the failover from the `local_failover` event; it must not infer it from the route.

#### `readings`

```json
{
  "delivered_unique": 49,
  "duplicates": 0,
  "recent": [ { "origin": "A", "session": 1664252997, "seq": 45, "value": 20.13,
                "value_hundredths": 2013, "via": "B", "hops": 2, "ttl": 7,
                "observed_path": ["A", "B", "C"], "received_ms": 6030 } ]
}
```

`recent` holds the last 50 unique readings. `delivered_unique` counts each reading once (a repeat caused by a lost acknowledgement is counted under `duplicates`).

#### `counts`

Each entry is `{ "value", "known_by", "as_of_ms", "age_ms" }` or `null` before the first telemetry message:

```json
{
  "delivered": { "value": 7, "known_by": "C", "as_of_ms": 18063, "age_ms": 1937 },
  "duplicates": { "value": 0, "known_by": "C" },
  "created": { "value": 9, "known_by": "A (telemetry)", "as_of_ms": 19198, "age_ms": 802 },
  "acknowledged": { "...": "..." }, "lost": { "...": "..." }, "no_route": { "...": "..." },
  "pending": { "value": 0, "known_by": "A (telemetry, derived)", "as_of_ms": 19198, "age_ms": 802 }
}
```

*Delivered* is known by C and is as fresh as the last reading. *Created, acknowledged, lost, no_route* are known only by A and arrive by telemetry every ~5 s, so they lag; `pending` is derived from them (created − acknowledged − lost − no_route) and can differ from reality by up to a telemetry interval. `no_route` counts readings A dropped at creation for lack of a route.

#### `presenter`

```json
{
  "channel": "environment",
  "note": "What the presenter did to the simulated environment. This is not something the controller knows.",
  "conditions": {
    "stopped_nodes": ["B"], "links_down": ["A->B"],
    "link_overrides": { "B->C": { "loss": 0.3, "delay_ms": 80 } },
    "gateway_attached": true
  }
}
```

This is **presenter knowledge**, shown so a viewer can see which conditions were injected. Never merge it into the node and link views.

### `GET /api/events?after_id=N&limit=M`

State-changing events with `event_id > N`, oldest first (default `limit` 200, maximum 1000).

```json
{ "events": [ { "event_id": 151, "type": "...", "data": { } } ],
  "last_event_id": 154, "oldest_event_id": 12, "complete": true, "run_id": 1 }
```

The history is bounded (default 2000). `complete: false` means it no longer reaches back to `after_id`: fetch the snapshot instead.

### `GET /api/scenarios`

```json
{ "scenarios": ["b-failure-auto-reroute", "live-demo", "normal-via-b"], "current": "live-demo" }
```

## Presenter controls (HTTP POST, simulation mode only)

Every control changes **only the simulated environment**. None of them calls the controller or tells it to reroute: the controller must discover the effect through the gateway, as it would on hardware. A test proves it (the controller's state and the commands it sent are identical before and after each control).

| Endpoint | Body | Effect |
| --- | --- | --- |
| `POST /api/control/node` | `{"node": "B", "action": "stop"}` or `"start"` | Power a node off (RAM lost) or on (new session). |
| `POST /api/control/link` | `{"link": "A->B", "down": true}` | Take **one direction** of a link down or up. |
| `POST /api/control/link-config` | `{"link": "B->C", "loss": 0.2, "delay_ms": 80}` | Set loss (0–1) and/or one-way delay (0–5000 ms) of one direction. |
| `POST /api/control/gateway` | `{"attached": false}` | Detach/attach the controller from node C: it then sees and sends nothing. C itself keeps receiving and acknowledging. (C does not buffer readings while detached; that is a later phase.) |
| `POST /api/control/pause` | `{}` | Stop simulated time. |
| `POST /api/control/resume` | `{}` | Continue. |
| `POST /api/control/speed` | `{"factor": 5}` | Simulated seconds per real second, 0.1–1000. |
| `POST /api/control/reset` | `{"scenario": "b-failure-auto-reroute"}` | Replace the run with a fresh one from `scenarios/<name>.json`. `run_id` increases and a `reset` event is sent. |
| `POST /api/control/step` | `{"ms": 5000}` | Advance simulated time by a fixed amount now (tests, debugging; also while paused). |

Nodes are `A`–`D`; links are written `"A->B"` (the A-to-B direction).

Success: `200` with

```json
{ "ok": true, "applied": "node", "run_id": 1, "last_event_id": 219,
  "clock": { "sim_ms": 155619, "speed": 10.0, "paused": false } }
```

### Errors

Every error has the same shape and a stable `code`:

```json
{ "error": { "code": "json_required", "message": "send the body as application/json (form posts are refused)" } }
```

| Status | `code` | When |
| --- | --- | --- |
| 400 | `bad_request` | Invalid JSON, a body that is not an object, unknown fields, or a bad value. Nothing was applied. |
| 400 | `bad_scenario` | The scenario file could not be loaded. |
| 403 | `origin_not_allowed` | `Origin` is present and not an allowed origin, or `Sec-Fetch-Site` is `cross-site`. |
| 404 | `unknown_scenario` | No such scenario (names are file stems; no paths). |
| 409 | `not_available_in_hardware_mode` | The backend is in hardware mode, which has no simulator. |
| 415 | `json_required` | The body is not `application/json` (a plain HTML form cannot send it). |

State-changing requests are protected three ways: the server binds to `127.0.0.1` by default; a present `Origin` header must be an allowed origin; and the body must be JSON. A browser always sends `Origin` on a cross-site POST and cannot send a JSON body cross-site without a CORS preflight, which only allowed origins pass. Requests with no `Origin` (curl, scripts, the watcher) are accepted; that is why the default bind is localhost.

## WebSocket `/ws`

The browser opens one WebSocket. The server sends JSON text messages; anything the client sends is ignored. A foreign `Origin` is closed with code 1008. A client that falls more than 2000 messages behind is closed with 1013 and should reconnect and re-read the snapshot.

### Envelope

| Field | Type | Meaning |
| --- | --- | --- |
| `v` | integer | Envelope version. Currently `1`. |
| `seq` | integer | Starts at `1` on each connection and increases by 1 per message. |
| `ts` | string | Server wall-clock time, ISO 8601 UTC with milliseconds. |
| `type` | string | Event type (below). |
| `data` | object | Event payload. State-changing events carry `data.sim_ms`. |
| `event_id` | integer, optional | Backend-wide, strictly increasing id (never restarts, not even on reset). Present **only** on state-changing events; absent on `status` and `heartbeat`. |

`seq` and `event_id` do different jobs. `seq` is per connection and counts every message, so a jump reveals a lost message on that connection. `event_id` is global and counts only state changes, so it orders events against snapshots. Clients must ignore unknown `type` values and unknown fields in `data`. A `v` the client does not understand is an error.

### Connection state and the reconnect rule

- **connected** — socket open and a message (any type) arrived within the last 3 × `heartbeat_interval_s`.
- **stale** — socket open but nothing for longer than that. Show data as possibly out of date.
- **disconnected** — socket closed or errored. Retry with backoff (suggested: 1 s doubling to a 10 s cap).

**Reconnect rule:** every time a WebSocket connects, fetch `GET /api/snapshot` and replace all state with it. The `status` event carries `snapshot_required: true` as the prompt.

**Snapshot/event ordering rule:** open the WebSocket first and buffer incoming events, then fetch the snapshot, apply it, then apply buffered and later events, **discarding any state-changing event with `event_id <= last_event_id`**. If a received `event_id` jumps by more than 1 from the previous one (within a `run_id`), a change was missed: re-fetch. A `reset` event, or a snapshot with a different `run_id`, means replace everything. The fixtures include a mid-run snapshot to test this.

### Non-state-changing messages

| `type` | `data` |
| --- | --- |
| `status` | Once, immediately after connecting (`seq` 1): `backend`, `mode`, `version`, `heartbeat_interval_s`, `snapshot_required` (always `true`), `run_id`. |
| `heartbeat` | Every `heartbeat_interval_s` (default 2): `interval_s`, `sim_ms`, `speed`, `paused`. Use it for the connection indicator and the simulated clock. |

### State-changing events

All carry `event_id` and `data.sim_ms`. Examples are real output (fields abbreviated only where noted).

**The controller's view of the topology**

| `type` | `data` (besides `sim_ms`) |
| --- | --- |
| `node_seen` | `node`, `session` — first time a node is reachable. e.g. `{"node":"B","session":3170526542}` |
| `node_lost_contact` | `node`, `observed_by` (list), `silent_for_ms`, `basis` (text). e.g. `{"node":"B","observed_by":["A","C"],"silent_for_ms":3050,"basis":"fresh neighbor reports list it as lost (no HELLO heard)"}` |
| `node_regained_contact` | `node`, `session`, `restarted` (true if the session changed). |
| `node_restarted` | `node`, `old_session`, `session` — rebooted so fast it was never declared lost. |
| `node_stale` | `node`, `reason` — its reports expired. |
| `link_state_changed` | `link` (`"A-B"`), `old` (or `null`), `new`, `usable`, `directions`. e.g. `{"link":"B-C","old":null,"new":"stale","usable":false,"directions":{"C->B":true,"B->C":false}}` |

**The controller's routing and recovery**

| `type` | `data` (besides `sim_ms`) |
| --- | --- |
| `route_computed` | `flow`, `epoch`, `version`, `path`, `cost_ms`, `graph` (links used with `cost_ms`), `held_down` (`link`, `eligible_in_ms`), `trigger` (`initial`, `topology_changed`, `node_restarted`, `retry_after_failure`, `epoch_advanced`, `local_failover_report`), `backups`, `backup_entries`. |
| `route_installing` | `version`, `epoch`, `kind`, `phase` (`backup_relays`, `relays`, `source`), `nodes`. One event per phase. |
| `route_active` | `version`, `epoch`, `path`, `cost_ms`, `confirmed_by`, `distribution_ms` (computed → every node acknowledged), `trigger`, `backups`. e.g. `{"version":1,"epoch":1000,"path":["A","B","C"],"cost_ms":65,"confirmed_by":["D","B","A"],"distribution_ms":170,"trigger":"initial","backups":{"A":"D"}}` |
| `route_install_failed` | `version`, `epoch`, `kind`, `state` (`failed` or `unconfirmed`), `reason`, `nodes`, `attempts`. |
| `no_available_route` | `flow`, `epoch`, `version`, `reason`, `graph`, `held_down`, `trigger`, `source_notified` (always `false` here). Headline text for the UI: **"No available route"**. |
| `no_route_confirmed` | `version`, `epoch`, `confirmed_by`, `distribution_ms` — the source acknowledged being told there is no route. If it never arrives, the source was not told. |
| `link_held_down` | `link`, `hold_down_ms`, `eligible_at_ms` — a link that came back (or that a node abandoned) is not used for routes until then. |
| `epoch_advanced` | `old_epoch`, `held_epoch`, `epoch`. |
| `local_failover` | `node`, `from` (abandoned next hop), `to` (backup now used, or `null`), `reason` (`neighbor_lost` = T1, `send_failures` = T2), `epoch`, `version`, `link`, `matches_current_route`, `outcome` (`backup_in_use` or `no_route`). e.g. `{"node":"A","from":"B","to":"D","reason":"send_failures","epoch":1000,"version":1,"link":"A-B","matches_current_route":true,"outcome":"backup_in_use"}`. **Label this recovery "local failover".** |

How to label recoveries: a `local_failover` event is the node's own reaction. A `route_active` whose `trigger` is `local_failover_report` is the controller *confirming* that choice. A `route_active` with `topology_changed` after a `node_lost_contact` and no failover is a *controller reroute*. If the controller is unreachable only the first can happen.

**What reached C**

| `type` | `data` (besides `sim_ms`) |
| --- | --- |
| `reading_received` | `origin`, `session`, `seq` (identity), `value` (units), `value_hundredths`, `via` (the neighbor that handed C the frame), `hops` (transmissions, from the TTL left), `ttl`, `observed_path` (origin … `via`, `"C"`; `null` for relays C cannot name), `received_ms`. Only unique readings; repeats are counted in `readings.duplicates`. e.g. `{"origin":"A","session":1664252997,"seq":45,"value":20.13,"value_hundredths":2013,"via":"B","hops":2,"ttl":7,"observed_path":["A","B","C"],"received_ms":6030}` |
| `telemetry` | `node`, `session`, `counters` (the nine counters, cumulative since that node's boot), `received_ms`. Only the node knows these. |

`hops` assumes every node uses the configured data TTL (8). It is the number of radio transmissions, so A → B → C is 2.

**The environment channel and the runtime**

| `type` | `data` (besides `sim_ms`) |
| --- | --- |
| `environment` | `channel` (`"environment"`), `source` (`presenter` or `scenario`), `action` (`stop_node`, `start_node`, `link_down`, `link_config`, `gateway`, or a scenario action), `target`, `detail`, `label`. e.g. `{"channel":"environment","source":"presenter","action":"stop_node","target":"B","detail":{},"label":"Stop B (injected by presenter, simulation)"}` |
| `runtime` | `change` (`paused`, `resumed`, `speed`), `paused`, `speed`. |
| `reset` | `run_id`, `scenario`. Replace all state: fetch the snapshot. |

## Recorded fixtures

[tests/dashboard-fixtures/](../tests/dashboard-fixtures/):

- `demo-run.snapshot-initial.json` — the snapshot at connect time (`last_event_id` 0).
- `demo-run.events.json` — every WebSocket message of a recorded demo, in order with `seq`, `event_id` and deterministic timestamps: normal operation, Stop B, local failover to D, the controller confirming, Start B, link hold-down, back to B.
- `demo-run.snapshot-mid.json` — a snapshot taken at 20 s, to test the discard rule.

Regenerate after a deliberate shape change with `uv run python -m iot_backend.live.record` (from `backend/`). Watch a live backend in a terminal with `uv run python -m iot_backend.watch`.

## Not implemented yet

- Controller heartbeat, C's bounded buffer and replay of readings while the controller is unreachable, and the "delayed" label for replayed readings (Phase 5b). While the gateway is detached nothing is buffered: readings in that window are not delivered to the controller.
- Experiment history, charts and CSV export (Phase 10).
- Adaptive routing policies (Phase 9); the route cost is the measured RTT.
- Hardware mode: `mode: "hardware"` exists only as a refusal.
- `event_id` is not persisted across backend restarts: after a restart `last_event_id` restarts and `run_id` is 1. A client that sees either go backwards must replace all state from the snapshot.

## Known limits of this draft

- No authentication; the backend binds to `127.0.0.1` by default and is intended for local use only.
- The `Origin` check keeps other web pages out; it does not stop non-browser programs on the laptop, which is why the default bind is localhost.
- Simulated timing: delays, loss and the 5 ms radio service time are configured values.
