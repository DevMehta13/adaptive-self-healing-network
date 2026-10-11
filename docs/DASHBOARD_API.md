# Dashboard API contract

**Draft — pending Dev's review.** Written by Harshal (backend owner) for Dev (dashboard owner). Nothing here is frozen until Dev approves it; change requests go in a pull request to this file. Phase 0 scope only: later phases will add event types and snapshot fields.

## Changes in this draft revision

- Added `last_event_id` to `GET /api/snapshot`.
- Added optional `event_id` to the WebSocket envelope, present only on state-changing events.
- Added the snapshot/event ordering rule (below), which replaces the former "no sequence marker" limit.
- `/ws` now rejects browser connections whose `Origin` is not in the allowed origins (a missing `Origin` is allowed).
- Heartbeats are paced by time only; messages from the client never trigger one.
- **Proposed (Phase 3, not wired yet):** element shapes for `nodes` and `links`, and the topology event types, so the graph view can be built early. See *Topology in the snapshot and events*.

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

Freshness rule: a report is fresh for 15 s after it arrives (three report intervals). If reports stop, nodes and links turn `stale` instead of staying "live". The snapshot's `last_event_id` and the ordering rule above apply unchanged.

## Known limits of this draft

- No authentication; the backend binds to `127.0.0.1` by default and is intended for local use only.
- The `Origin` check keeps other web pages out; it does not stop non-browser programs on the laptop, which is why the default bind is `127.0.0.1`.
- `event_id` is not persisted: it restarts when the backend restarts. A client that sees `last_event_id` go backwards, or a new backend `version`/start, must replace all state from the snapshot. A persistent run identifier may be added with the experiment history.
