# Backend

Laptop-side backend: FastAPI app serving a small HTTP API and a live WebSocket stream to the dashboard. In Phase 0 it only reports its own status; there is no simulator, protocol, controller, or database yet.

The dashboard contract is [docs/DASHBOARD_API.md](../docs/DASHBOARD_API.md) (draft, pending Dev's review).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs the right Python and the locked dependencies for you). Python 3.11 or newer.

| | Windows (PowerShell) | macOS |
| --- | --- | --- |
| Install uv | `winget install --id=astral-sh.uv -e` or `pip install uv` | `brew install uv` or `pip install uv` |
| Check | `uv --version` | `uv --version` |

## Install

From the repository root:

```sh
cd backend
uv sync
```

This creates `backend/.venv` and installs the exact versions pinned in `uv.lock`. Commands are the same on Windows and macOS.

## Run the server

```sh
uv run python -m iot_backend
```

Then open <http://127.0.0.1:8000/health> or <http://127.0.0.1:8000/docs>. Stop with Ctrl+C.

Optional configuration: copy `.env.example` to `.env` (`Copy-Item .env.example .env` on Windows, `cp .env.example .env` on macOS) and edit it, or set `IOT_*` environment variables. Defaults work without any file.

## Run the tests and linter

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Layout

```text
backend/
  pyproject.toml       project metadata, dependencies, ruff and pytest settings
  uv.lock              exact dependency versions (commit changes to this file)
  .env.example         documented configuration defaults
  src/iot_backend/
    config.py          settings from IOT_* environment variables
    envelope.py        the versioned WebSocket message envelope
    main.py            app factory and server entry point
    api/               dashboard-facing endpoints (routes.py, ws.py)
  tests/               pytest checks
```

`src/iot_backend/` is the Python package from plan.md's `backend/` tree. `protocol/`, `controller/`, `simulator/`, `transports/` and `storage/` will be added inside it by the phases that need them. Tests live in `backend/tests/` so the backend can be tested on its own.

## How the live updates work

A **WebSocket** is a long-lived two-way connection between the browser and the backend. With normal HTTP the browser must ask ("polling") each time it wants news, which wastes requests and shows changes late, up to one polling interval. With a WebSocket the browser connects once and the backend pushes each event the moment it happens. That is what the dashboard needs for packets, failures and recovery.

The **heartbeat** is a small event sent every 2 seconds even when nothing else happens. Silence on a WebSocket is ambiguous: "nothing changed" looks the same as "the backend froze or the connection died". Because heartbeats are regular, the dashboard can tell the difference: arriving heartbeats mean *connected*, a missed few mean *stale*, and a closed socket means *disconnected*.

The two channels split the job: `GET /api/snapshot` gives the full current state (used on first load and on every reconnect), and `/ws` streams the changes afterwards.

## Limitations

- Tested on Windows only so far; macOS is covered by CI after push.
- No authentication; keep the default `127.0.0.1` binding.
- `httpx` emits a deprecation warning under pytest (Starlette's test client suggests `httpx2`); harmless for now.
