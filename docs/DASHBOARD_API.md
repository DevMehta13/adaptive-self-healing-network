# Dashboard API contract

**Draft — pending Dev's review.** Written by Harshal (backend owner) for Dev (dashboard owner). Nothing here is frozen until Dev approves it; change requests go in a pull request to this file. Phase 0 scope only: later phases will add event types and snapshot fields.

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
  "nodes": [],
  "links": []
}
```

`nodes` and `links` are always empty in Phase 0. Their element shapes will be defined in Phase 1+ and added here before the dashboard depends on them.

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

## Known limits of this draft

- No authentication; the backend binds to `127.0.0.1` by default and is intended for local use only.
- CORS applies to HTTP. Browsers do not enforce CORS on WebSockets, and the server does not check the WebSocket `Origin` yet.
- The snapshot has no sequence marker, so an event that happens between the snapshot read and the WebSocket subscription cannot be ordered precisely. This will matter once there is real state; to be decided with Dev in Phase 1.
