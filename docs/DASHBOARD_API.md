# Dashboard API contract

**Draft — pending Dev's review.** Written by Harshal (backend owner) for Dev (dashboard owner). Nothing here is frozen until Dev approves it; change requests go in a pull request to this file. Phase 0 scope only: later phases will add event types and snapshot fields.

## Changes in this draft revision

- Added `last_event_id` to `GET /api/snapshot`.
- Added optional `event_id` to the WebSocket envelope, present only on state-changing events.
- Added the snapshot/event ordering rule (below), which replaces the former "no sequence marker" limit.
- `/ws` now rejects browser connections whose `Origin` is not in the allowed origins (a missing `Origin` is allowed).
- Heartbeats are paced by time only; messages from the client never trigger one.

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

## Known limits of this draft

- No authentication; the backend binds to `127.0.0.1` by default and is intended for local use only.
- The `Origin` check keeps other web pages out; it does not stop non-browser programs on the laptop, which is why the default bind is `127.0.0.1`.
- `event_id` is not persisted: it restarts when the backend restarts. A client that sees `last_event_id` go backwards, or a new backend `version`/start, must replace all state from the snapshot. A persistent run identifier may be added with the experiment history.
