# Dashboard fixtures

A recorded, simulated demo run of the live backend, as JSON, so the dashboard can be built and
tested without running the backend. Contract: [docs/DASHBOARD_API.md](../../docs/DASHBOARD_API.md).

| File | What it is |
| --- | --- |
| `demo-run.snapshot-initial.json` | `GET /api/snapshot` at connect time (`last_event_id` 0) |
| `demo-run.events.json` | every WebSocket message of the run, in order (`seq`, `event_id`, deterministic timestamps), plus `presenter_script` listing what the presenter did and when |
| `demo-run.snapshot-mid.json` | a snapshot taken at 20 s, for testing the "discard events with `event_id <= last_event_id`" rule |

The run: normal operation (A > B > C, D is A's backup), **Stop B** at 12 s, A fails over to D
by itself, the controller confirms A > D > C, the controller declares loss of contact with B,
**Start B** at 32 s, B's links come back and are held down for 5 s, the route returns to B.
Everything is simulated time with a fixed seed.

These files are generated and checked by a test. After a deliberate change to a shape, from
`backend/`:

```sh
uv run python -m iot_backend.live.record          # rewrite
uv run python -m iot_backend.live.record --check  # exit 1 if stale
```
