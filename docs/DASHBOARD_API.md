# Dashboard API contract

**Draft — pending Dev's review.** Written by Harshal (backend owner) for Dev (dashboard owner). Nothing here is frozen until Dev approves it; change requests go in a pull request to this file. Phase 0 scope only: later phases will add event types and snapshot fields.

## Changes in this draft revision

- Added `last_event_id` to `GET /api/snapshot`.
- Added optional `event_id` to the WebSocket envelope, present only on state-changing events.
- Added the snapshot/event ordering rule (below), which replaces the former "no sequence marker" limit.
- `/ws` now rejects browser connections whose `Origin` is not in the allowed origins (a missing `Origin` is allowed).
- Heartbeats are paced by time only; messages from the client never trigger one.
- **Proposed (Phase 3, not wired yet):** element shapes for `nodes` and `links`, and the topology event types, so the graph view can be built early. See *Topology in the snapshot and events*.
- **Proposed (Phase 4b, not wired yet):** the `local_failover` route event, two new `trigger` values and a `backup_relays` install phase, `backups` in the `route` object, and per-node forwarding state. See *Local failover in the snapshot and events*.
- **Proposed (Phase 4a, not wired yet):** `rtt_ms` and `cost_ms` on links, the `node_restarted` topology event, a `route` object in the snapshot, and the route event types. See *Routes in the snapshot and events*.

Base URL in development: `http://127.0.0.1:8000` (HTTP) and `ws://127.0.0.1:8000/ws` (WebSocket). Host and port are configurable (see [backend/README.md](../backend/README.md)).

## HTTP endpoints

### `GET /health`

```json
{ "status": "ok", "mode": "simulation", "version": "0.1.0" }
```

Liveness check. `mode` is always `"simulation"` in Phase 0.

### `GET /api/snapshot`

The complete current state. It is the source of truth; WebSocket events only describe changes after it.

```json
{
  "v": 1,
  "mode": "simulation",
  "backend": { "status": "ok", "version": "0.1.0" },
  "server_time": "2026-10-11T09:30:00.123Z",
  "last_event_id": 0,
  "nodes": [],
  "links": []
}
```

`last_event_id` is the `event_id` of the newest state-changing event already reflected in this snapshot (`0` if there has been none). `nodes` and `links` are always empty so far. Their element shapes will be defined in later phases and added here before the dashboard depends on them.

## WebSocket `/ws`

The browser opens one WebSocket. The server sends JSON text messages; anything the client sends is ignored.

### Envelope

Every message has exactly these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `v` | integer | Envelope version. Currently `1`. |
| `seq` | integer | Starts at `1` on each connection and increases by 1 per message. |
| `ts` | string | Server time, ISO 8601 UTC with milliseconds, e.g. `2026-10-11T09:30:00.123Z`. |
| `type` | string | Event type (below). |
| `data` | object | Event payload; shape depends on `type`. |
| `event_id` | integer, optional | Backend-wide, strictly increasing id (starts at 1, never reused while the backend runs). Present **only** on state-changing events; absent on `status` and `heartbeat`. |

`seq` and `event_id` do different jobs. `seq` is per connection and counts every message, so a jump reveals a lost message on that connection. `event_id` is global and only counts changes to state, so it orders events against snapshots.

```json
{ "v": 1, "seq": 2, "ts": "2026-10-11T09:30:02.120Z", "type": "heartbeat", "data": { "interval_s": 2.0 } }
```

Clients should ignore unknown `type` values and unknown fields in `data`, so new events do not break an older dashboard. A `v` the client does not understand should be treated as an error and shown as such.

### Event types (Phase 0)

| `type` | When | `data` |
| --- | --- | --- |
| `status` | Once, immediately after connecting (always `seq` 1) | `backend`: `"ok"`; `mode`: `"simulation"`; `version`: backend version; `heartbeat_interval_s`: number; `snapshot_required`: `true` |
| `heartbeat` | Every `heartbeat_interval_s` seconds (default 2) | `interval_s`: number |

### Connection state and the reconnect rule

The dashboard derives its connection indicator from what it observes:

- **connected** — socket open and a message (any type) arrived within the last 3 × `interval_s`.
- **stale** — socket open but nothing arrived for more than 3 × `interval_s`. Show data as possibly out of date.
- **disconnected** — socket closed or errored. Retry with backoff (suggested: 1 s doubling to a 10 s cap).

**Reconnect rule:** every time a WebSocket connects — first time or after a drop — the client must fetch `GET /api/snapshot` and replace its state with it. The `status` event carries `snapshot_required: true` as the explicit prompt. `seq` restarts at 1 on each connection, so it is only for detecting gaps within one connection (a jump means the client missed events and should re-fetch the snapshot).

**Snapshot/event ordering rule:** open the WebSocket first and buffer incoming events, then fetch the snapshot, apply it, and then apply buffered and later events — discarding any state-changing event with `event_id <= last_event_id`. Those are already reflected in the snapshot, so nothing is applied twice or missed. If a received `event_id` jumps by more than 1 from the previous one, the client missed a change and should re-fetch the snapshot. (No state-changing events exist yet; the rule is fixed now so the dashboard is built correctly.)

## Topology in the snapshot and events (proposed — not wired)

Phase 3 builds this in the backend (`TopologyView.snapshot()` and its event stream). It is **not** served yet: `/api/snapshot` still returns empty `nodes` and `links`, and `/ws` sends no topology events. Phase 5 will wire them. The shapes below are what Dev can build the graph view against.

### `nodes` element

```json
{
  "id": "B",
  "status": "lost_contact",
  "since_ms": 10527,
  "session": 1291090604,
  "last_report_age_ms": 2400,
  "source": { "kind": "neighbor_reports", "observed_by": ["A", "C"], "silent_for_ms": 3050 }
}
```

| Field | Meaning |
| --- | --- |
| `id` | `"A"`–`"D"`. |
| `status` | `reachable`: its own report is fresh. `lost_contact`: every neighbor with a fresh report lists it as lost. `stale`: its reports have expired and nothing newer exists. `unknown`: never heard. There is deliberately **no "powered off"**: a timeout cannot prove that. |
| `since_ms` | When the status last changed, in ms on the controller's run clock (Phase 5 adds wall-clock timestamps). |
| `session` | Its latest own session id; a change means it restarted. |
| `last_report_age_ms` | Age of its latest report, or `null`. |
| `source` | Present only for `lost_contact`: who observed it and how long they had heard nothing. Show it next to the status. |

### `links` element

```json
{
  "a": "A", "b": "B", "state": "one_way", "usable": false,
  "directions": { "B->A": true, "A->B": false },
  "since_ms": 8576
}
```

| Field | Meaning |
| --- | --- |
| `state` | `up`: both ends report two-way with fresh reports. `one_way`: only one direction is confirmed. `down`: neither is heard, or an endpoint is lost contact. `stale`: an end's report has expired. |
| `usable` | `true` only for `up`. |
| `directions` | `"X->Y": true` means frames sent by X are confirmed to reach Y. |

Draw `one_way` and `stale` differently from `up` (for example dashed), and never draw a link from the planned topology as healthy without this evidence.

### Events (all state-changing, so each carries the envelope's `event_id`)

| `type` | `data` |
| --- | --- |
| `node_seen` | `node`, `session` — first time a node is reachable. |
| `node_lost_contact` | `node`, `observed_by` (list), `silent_for_ms`, `basis` (text). |
| `node_regained_contact` | `node`, `session`, `restarted` (true if the session changed). |
| `node_stale` | `node`, `reason` — its reports expired. |
| `link_state_changed` | `link` (`"A-B"`), `old`, `new`, `usable`, `directions`. |
| `node_restarted` | `node`, `old_session`, `session` — a node rebooted so fast that it was never declared lost (its RAM, and any route it held, is gone). *Added in Phase 4a.* |

Freshness rule: a report is fresh for 15 s after it arrives (three report intervals). If reports stop, nodes and links turn `stale` instead of staying "live". The snapshot's `last_event_id` and the ordering rule above apply unchanged.

## Routes in the snapshot and events (proposed — not wired)

Phase 4a builds this in the backend (`RouteController.route_state()` and its event stream). It is **not** served yet: `/api/snapshot` has no `route` field and `/ws` sends no route events. Phase 5 will wire them. Everything below is a proposal for Dev to build the route view against. Simulated link delays are configured settings; the dashboard must label them so (see *Labels*).

### Link additions

Each `links` element gains:

```json
{ "rtt_ms": { "A": 31, "B": 29 }, "cost_ms": 31 }
```

| Field | Meaning |
| --- | --- |
| `rtt_ms` | The round-trip time each end measured for this link **on its own clock**, in ms; `null` for an end with no fresh measurement. These are round trips, not one-way delays, and the two numbers are never subtracted from each other. |
| `cost_ms` | What the route calculation uses: the larger of the two, only if **both** ends measured it and the link is `up`; otherwise `null`. A link with `cost_ms: null` is not used for routing and must not be drawn or described as zero or free. |

### `route` object (snapshot)

```json
{
  "flow": "A->C",
  "epoch": 1700000000,
  "version": 3,
  "kind": "route",
  "state": "active",
  "path": ["A", "D", "C"],
  "cost_ms": 130,
  "confirmed_by": ["D", "A"],
  "waiting_for": []
}
```

| Field | Meaning |
| --- | --- |
| `kind` | `route`, or `no_route` when the controller has declared that no path exists. |
| `state` | `waiting` (no plan yet; discovery in progress), `computed`, `installing`, `active`, `failed` (a node refused), `unconfirmed` (acknowledgements missing after the bounded retries), `superseded`. **Only `active` may be drawn as the current route.** |
| `path` | Calculated route (empty for `no_route`). This is the *plan*; keep it visually separate from the observed packet path, which comes from packet events. |
| `confirmed_by` / `waiting_for` | Nodes whose installation has / has not been acknowledged yet. |

### Route events (state-changing, so each carries the envelope's `event_id`)

Internally the route controller numbers its own events; Phase 5 merges the streams and assigns the backend-wide `event_id`.

| `type` | `data` |
| --- | --- |
| `route_computed` | `flow`, `epoch`, `version`, `path`, `cost_ms`, `graph` (links used with their `cost_ms`), `held_down` (links excluded by hold-down, with `eligible_in_ms`), `trigger` (`initial`, `topology_changed`, `node_restarted`, `retry_after_failure`, `epoch_advanced`). Computation time is **not** in the event (see below). |
| `route_installing` | `version`, `epoch`, `kind`, `phase` (`relays` or `source`), `nodes`. One event per phase. |
| `route_active` | `version`, `epoch`, `path`, `cost_ms`, `confirmed_by`, `distribution_ms` (computed → every node acknowledged, simulated time). |
| `route_install_failed` | `version`, `epoch`, `kind`, `state` (`failed` or `unconfirmed`), `reason`, `nodes`, `attempts`. |
| `no_available_route` | `flow`, `epoch`, `version`, `reason`, `graph`, `held_down`, `trigger`, `source_notified` (always `false` here; see `no_route_confirmed`). The headline text for the UI is **"No available route"**. |
| `no_route_confirmed` | `version`, `epoch`, `confirmed_by`, `distribution_ms` — the source acknowledged being told there is no route. If it never arrives, the source was not told (for example, it is cut off). |
| `link_held_down` | `link`, `hold_down_ms`, `eligible_at_ms` — a link that came back is not used for routes until then. |
| `epoch_advanced` | `old_epoch`, `held_epoch`, `epoch` — nodes held a newer controller epoch than this controller started with, so it moved past it. |

### Labels and measurements

- Show every simulated link delay as a **simulated setting**. The simulated cheaper B links make B the preferred relay; that is configuration, not a measurement.
- Route **computation time** is wall-clock CPU time on the laptop (microseconds) and is reported separately (`ControllerStats.compute_us`); **distribution time** is simulated milliseconds. Do not add or compare them.
- Recovery timings (failure detection, route active, service recovery, delivery gap) are referenced to the *scripted* failure time and are simulation results, not hardware results.

## Local failover in the snapshot and events (proposed — not wired)

Phase 4b builds this in the backend. It is **not** served yet; Phase 5 wires it. Docs only.

### Additions to the `route` object

```json
{ "backups": { "A": "D" }, "backup_entries": { "D": "C" } }
```

`backups` maps each forwarding node on the route to its installed backup next hop (empty if none). `backup_entries` lists nodes **off** the route that were given a forwarding entry so a backup can work. Draw backups distinctly from the route (for example dotted) and never as an observed path.

### New and extended events

| `type` | `data` |
| --- | --- |
| `local_failover` | `node`, `from` (abandoned next hop), `to` (backup now in use, or `null`), `reason` (`neighbor_lost` = T1, `send_failures` = T2), `epoch`, `version`, `link` (`"A-B"`), `matches_current_route` (the report is about the route the controller currently believes in), `outcome` (`backup_in_use` or `no_route`). **Label the recovery "local failover", not "controller reroute".** |
| `route_computed` | now also `backups`, `backup_entries`; `trigger` may be `local_failover_report` |
| `route_installing` | `phase` may be `backup_relays` (before `relays`, then `source`) |
| `route_active` | now also `trigger` (what caused this version: `initial`, `topology_changed`, `local_failover_report`, ...) and `backups` |
| `link_held_down` | also emitted for a link a node abandoned: the controller will not route over it again until it has been routable for the hold-down |

The node-side trace events `local_failover` (with `trigger` = `T1_neighbor_state` or `T2_send_failures` and its timing), `local_no_route`, `send_failed` and `frame_rerouted` are simulator trace, not API events, until Phase 5 decides which are surfaced.

### Recovery labelling

A recovery is *local failover* when the source's table was switched by the node itself and *controller reroute* when it was switched by a route version. The ordering is visible in time: the node's switch comes first, and the controller's route that follows is labelled with `trigger: local_failover_report`. When the controller is unreachable only the first can happen, and the second appears after reconnection.

## Known limits of this draft

- No authentication; the backend binds to `127.0.0.1` by default and is intended for local use only.
- The `Origin` check keeps other web pages out; it does not stop non-browser programs on the laptop, which is why the default bind is `127.0.0.1`.
- `event_id` is not persisted: it restarts when the backend restarts. A client that sees `last_event_id` go backwards, or a new backend `version`/start, must replace all state from the snapshot. A persistent run identifier may be added with the experiment history.
