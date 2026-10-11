# Backend

Laptop-side backend. It contains:

- a FastAPI app serving a small HTTP API and a live WebSocket stream to the dashboard (reports its own status only);
- the node **protocol** module: binary frame encoding and validated decoding;
- a deterministic **simulator** with two virtual nodes, A and B.

There is no controller, routing, gateway interface, or database yet.

Contracts (both drafts, pending Dev's review): [docs/DASHBOARD_API.md](../docs/DASHBOARD_API.md) and [docs/PROTOCOL.md](../docs/PROTOCOL.md).

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

## Run the simulator demo

The same command works in PowerShell and in a macOS terminal (run it inside `backend/`):

```sh
uv run python -m iot_backend.simulator --seed 42 --duration 20 --drop A-B:3
```

It prints a trace of every event, then a summary. Simulated time passes instantly; nothing waits for real seconds. Useful options:

| Option | Meaning |
| --- | --- |
| `--seed N` | Random seed. The same seed always gives the same output. |
| `--duration S` | Simulated seconds to run. |
| `--drop A-B:3` | Drop the 3rd frame offered to the A→B link. `B-A:2,4` drops several. Repeatable. |
| `--loss A-B:0.2` | Randomly lose 20 % of frames on a link (seeded, so repeatable). |
| `--delay-ms N` | One-way link delay (default 20). |
| `--ack-timeout-ms N` | How long A waits for an acknowledgement (default 1000). |
| `--restart-a S` | Power-cycle A at second S: new session, sequence numbers start again. |
| `--malformed-at S` | Deliver a corrupted 3-byte frame to B at second S. |

Try `--drop B-A:2` to see a reading that B accepted but A reports as unacknowledged.

The trace shows each frame as `A:3905a56d#1`: sender, session id in hex, then sequence number. Event names: `frame_sent`, `frame_dropped_by_link`, `frame_received`, `frame_rejected`, `reading_delivered`, `ack_received`, `ack_ignored`, `ack_timeout`, `node_booted`.

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
    protocol/          frame constants, message classes, encode()/decode()
    simulator/         engine.py (virtual clock), link.py, nodes.py, trace.py,
                       scenario.py (Simulation), __main__.py (CLI demo)
  tests/               pytest checks (test_protocol.py loads ../tests/protocol-examples/)
```

`src/iot_backend/` is the Python package from plan.md's `backend/` tree. `controller/`, `transports/` and `storage/` will be added inside it by the phases that need them. Backend tests live in `backend/tests/` so the backend can be tested on its own; the shared protocol examples that the firmware will also use live at the repository top level, in `tests/protocol-examples/`.

## How the live updates work

A **WebSocket** is a long-lived two-way connection between the browser and the backend. With normal HTTP the browser must ask ("polling") each time it wants news, which wastes requests and shows changes late, up to one polling interval. With a WebSocket the browser connects once and the backend pushes each event the moment it happens. That is what the dashboard needs for packets, failures and recovery.

The **heartbeat** is a small event sent every 2 seconds even when nothing else happens. Silence on a WebSocket is ambiguous: "nothing changed" looks the same as "the backend froze or the connection died". Because heartbeats are regular, the dashboard can tell the difference: arriving heartbeats mean *connected*, a missed few mean *stale*, and a closed socket means *disconnected*.

The two channels split the job: `GET /api/snapshot` gives the full current state (used on first load and on every reconnect), and `/ws` streams the changes afterwards.

## Packets, sessions, ACKs and the simulator, in plain language

**Packet header.** Every frame starts with a small fixed header, like the address and label on a parcel, so any node can understand it without guessing:

| Field | Why it exists |
| --- | --- |
| `version` | Lets a node refuse a format it does not understand instead of misreading it. |
| `type` | Says what the payload is (a reading, an acknowledgement, later a HELLO...). |
| `src` | Which node *created* the frame, so the reply knows where to go. |
| `dst` | Which node the frame is finally for, even when relays carry it. |
| `session` | A random number chosen at each boot (next paragraph). |
| `seq` | Counts the frames a node has created in this session, so each can be named. |
| `ttl` | A hop budget that relays will decrement, so a frame can never circulate forever. |
| `payload_len` | Lets a receiver check it got the whole frame and nothing extra. |

**Why a session ID.** A board that reboots forgets its counter and starts again at 1. Without more information, a brand-new reading #1 looks identical to the old reading #1, and an acknowledgement from before the reboot could be mistaken for the new one. A random session number, chosen at boot, makes the pair (`src`, `session`, `seq`) unique across restarts. The test `test_restarted_a_has_new_session...` shows an in-flight ACK from the old session being ignored.

**What an ACK proves, and what it doesn't.** A `DELIVERY_ACK` proves that B received that exact reading intact (it names origin, session and sequence). It does **not** prove B has done anything further with it. And **not receiving** an ACK proves nothing about whether the reading arrived: when the ACK itself is lost, B has the reading but A must report it *unacknowledged*. Try `--drop B-A:2` to see exactly this. Sending is not delivery, and delivery is not confirmed delivery.

**Discrete-event simulator with a virtual clock.** Instead of running in real time, the simulator keeps a to-do list of future events ("at 2000 ms, A creates a reading", "at 2020 ms, this frame reaches B") sorted by time. It repeatedly takes the earliest one, sets its clock to that time, and runs it, possibly adding new events. Nothing sleeps, so 20 simulated seconds take milliseconds, and the result does not depend on how busy the laptop is.

**Why seeds make tests repeatable.** "Random" loss is generated by a pseudo-random generator that is fully determined by its starting number, the seed. The same seed gives the same sequence of lucky and unlucky frames, so the same trace, every run and on every computer. A different seed gives a different (but equally repeatable) run. That turns "it sometimes fails" into an experiment we can replay and assert on.

## Limitations

- Tested on Windows only so far; macOS is covered by CI after push.
- No authentication; keep the default `127.0.0.1` binding.
- The simulator has only A and B, no retries, no forwarding, no duplicate suppression and no routing (Phase 2+). B counts every valid frame it receives. A simulation shows protocol logic, not radio behavior.
- `httpx` emits a deprecation warning under pytest (Starlette's test client suggests `httpx2`); harmless for now.
