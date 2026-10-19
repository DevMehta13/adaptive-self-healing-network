# Dashboard

Dev's React + TypeScript + Vite dashboard. **D0 implements the connection shell only.**
The network graph, packet view, presenter controls and history come in later increments.
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
sockets/timers/HTTP responses; `build` type-checks and writes `dist/`; `preview` serves
that build on localhost:5173 with the same backend proxy. Stop the dev server before
starting preview. A standalone static host needs its own `/api` and `/ws` proxy.

The dashboard CI workflow runs install, build and tests on macOS and Windows with Node 24.
CI configuration is not evidence of a successful run; actual results are recorded in
[STATUS.md](../docs/STATUS.md).
