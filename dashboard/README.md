# Dashboard

Dev's React + TypeScript + Vite dashboard: five pages behind a top bar (Overview, Network, Readings, Delivery, Saved runs), live from the backend and, for Saved runs, from recordings that work offline. Run history browsing is not part of it.
The backend is still simulation-only or tested against fake C; no real board is verified.

## Start on macOS or Windows

Install Node.js **24 LTS** with npm and [uv](https://docs.astral.sh/uv/).
Node 22.12+ also meets the package requirements. Shared dependencies are pinned in
`package.json` and `package-lock.json`; use `npm ci` on a fresh checkout.

From the repository root, in terminal 1:

```sh
cd backend
uv sync --locked
uv run python -m iot_backend
```

In terminal 2, from the repository root:

```sh
cd dashboard
npm ci
npm run dev
```

Open **http://localhost:5173**. Use `localhost` so the browser's origin matches the
backend's existing allow-list. The port is fixed: if 5173 is already occupied, Vite
reports an error rather than silently choosing a port the backend would refuse.
Stop either server with Ctrl+C. The commands above are the same in PowerShell and macOS.

No board, sensor, account or internet connection is needed to run after dependencies
have been installed. All fonts and UI assets are local.

## What to observe

- **Mode unconfirmed:** no valid snapshot has arrived yet. The dashboard does not guess.
- **Simulation / Hardware:** copied from the backend snapshot, with its full explanation.
  Hardware mode connected to fake C still does not prove a physical hardware test.
- **Connecting:** opening the stream or loading a fresh snapshot.
- **Connected:** a snapshot is loaded and valid live messages are arriving.
- **Stale:** the socket is open but silent for more than three advertised heartbeat
  intervals (6 seconds with the default 2-second heartbeat).
- **Disconnected:** the socket failed/closed, a request timed out, or the backend sent
  an incompatible message. Automatic retries back off from 1 second to a 10-second cap.

To try recovery, stop the backend while leaving the browser and Vite running, then
start the backend again. The indicator returns to Connected after a fresh snapshot
and live message. The last successful snapshot remains labelled with its load time.
Backend connectivity does **not** prove C is reachable or a sensor packet was delivered.

## Pages

Hash routes, no router library; an unknown hash opens the Overview, refresh keeps the page, and
switching pages never reconnects (one connection and one set of buffers serve the whole app).

| Page | Route | What it shows |
| --- | --- | --- |
| Overview | `#/` | Latest temperature (marked replayed if late), planned route beside the last observed path, one status line per board, created / delivered / lost in total, the major network changes, and the backend facts. |
| Network | `#/network` | The diamond with link states, the planned route and node trouble marked; node cards; planned route vs observed path; the controls (routing policy in both modes; in simulation also Stop/Start B and D, one link direction, Detach/Attach C, Pause/Resume, Reset run); and the timeline of network changes ("Major changes only" is on by default; presenter actions are marked "What we did"). |
| Readings | `#/readings` | An interactive uPlot chart (1/5/15 min and All, Live/Paused, drag to zoom, hover details; colour by relay, hollow points for replayed readings, markers for failover, route change, node and link events) and a table of recent readings. |
| Delivery | `#/delivery` | Created at A, delivered at C and acknowledged at A as a flow, then the loss and late counters, each with its source and age. |
| Saved runs | `#/runs` | The two recordings (relay failure, controller outage), stepped through moment by moment with Previous/Next, the dots or a list. Works with the backend off. |

The top bar shows the mode pill (Simulation / Hardware / Mode unconfirmed), the connection state (its details
open from the pill) and a light/dark toggle. The theme follows the system until you choose; the choice is
remembered in `localStorage`.

When the backend is down, stale or reconnecting, every live page says so in a banner, dims its numbers and
names the time of the last snapshot; controls are disabled until the live connection is healthy.

Times: in hardware mode the Readings chart and the timeline use the laptop's clock (HH:MM:SS), which stays
continuous across backend restarts, and the buffers are kept across a restart (so the controller-loss demo shows
the points from before the outage). In simulation they use the simulated clock (mm:ss) and are cleared on a
reset. A late reading is always written "replayed · delayed N s". The backend keeps only the latest 100 events;
the dashboard keeps its own buffers (about 2,000 readings and 500 network changes) while the page is open.

None of these browser checks proves radio delivery; hardware mode connected to the fake C is not a hardware test.

## Connection contract

See [DASHBOARD_API.md](../docs/DASHBOARD_API.md). The WebSocket opens first; updates are
buffered while the HTTP snapshot loads. Events already represented by the snapshot are
discarded. Event/connection sequence gaps and scenario resets cause a fresh snapshot;
reconnect always replaces it, including when a backend restart lowers the ids. D0 only
tracks stream continuity and snapshot metadata, not a live network-state reducer.

Both buffered events (2000) and retry waits (10 seconds) are bounded. Socket opening and
snapshot fetch each time out after 5 seconds. Requests are aborted and timers/socket
handlers cleaned up when a connection is replaced or the component unmounts.

Vite proxies `/api` and `/ws` to `http://127.0.0.1:8000`, so no backend edits are needed.
If the backend runs at another address, copy `.env.example` to `.env.local`, edit
`IOT_DASHBOARD_BACKEND`, and restart Vite. It must be an HTTP(S) base URL. Local env files
are ignored by Git. This setting applies to the dev/preview server, not the built files.

## Checks and production preview

From `dashboard/`:

```sh
npm run check
npm test
npm run build
npm run preview
```

`check` checks TypeScript; `test` checks the connection state machine with controlled
sockets/timers/HTTP responses, the recorded-run viewer against the actual fixtures,
and live snapshot/action handling;
`build` type-checks and writes `dist/`; `preview` serves
that build on localhost:5173 with the same backend proxy. Stop the dev server before
starting preview. A standalone static host needs its own `/api` and `/ws` proxy.

The dashboard CI workflow runs install, build and tests on macOS and Windows with Node 24.
CI configuration is not evidence of a successful run; actual results are recorded in
[STATUS.md](../docs/STATUS.md).
