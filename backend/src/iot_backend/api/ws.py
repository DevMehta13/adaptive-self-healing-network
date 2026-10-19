"""Live event stream.

On connect: a `status` event. Then every state-changing event as it happens (each carries an
`event_id`), plus a `heartbeat` every interval (no `event_id`). `seq` counts every message on
this connection.
"""

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from iot_backend import __version__
from iot_backend.envelope import make_envelope
from iot_backend.live import LiveEvent, LiveSimulation

router = APIRouter()

# A client that falls this far behind is dropped; it reconnects and re-reads the snapshot.
MAX_BACKLOG = 2000


@router.websocket("/ws")
async def events(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    live: LiveSimulation | None = getattr(websocket.app.state, "live", None)
    interval = settings.heartbeat_interval_seconds

    # Browsers do not apply CORS to WebSockets, so check the Origin header ourselves.
    # A missing Origin (non-browser client, tests) is allowed.
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in settings.cors_origin_list:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    queue: asyncio.Queue[LiveEvent] = asyncio.Queue()
    overflowed = False

    def on_event(event: LiveEvent) -> None:
        nonlocal overflowed
        if queue.qsize() >= MAX_BACKLOG:
            overflowed = True
        else:
            queue.put_nowait(event)

    unsubscribe = live.subscribe(on_event) if live is not None else (lambda: None)
    seq = 0

    async def send(event_type: str, data: dict[str, Any], event_id: int | None = None) -> None:
        nonlocal seq
        seq += 1  # numbering is per connection, so a new connection starts again at 1
        await websocket.send_json(make_envelope(seq, event_type, data, event_id))

    async def pump() -> None:
        loop = asyncio.get_running_loop()
        next_beat = loop.time() + interval
        while True:
            if overflowed:
                await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
                return
            remaining = next_beat - loop.time()
            if remaining <= 0:
                beat: dict[str, Any] = {"interval_s": interval}
                if live is not None:
                    beat.update(sim_ms=live.sim_ms, speed=live.speed, paused=live.paused)
                await send("heartbeat", beat)
                next_beat = loop.time() + interval
                continue
            try:
                event = await asyncio.wait_for(queue.get(), timeout=min(remaining, 0.25))
            except TimeoutError:
                continue
            await send(event.type, event.data, event.event_id)

    async def drain() -> None:
        # Whatever the client sends is ignored; waiting lets us notice a disconnect promptly.
        while True:
            await websocket.receive_text()

    try:
        await send(
            "status",
            {
                "backend": "ok",
                "mode": settings.mode,
                "version": __version__,
                "heartbeat_interval_s": interval,
                # Always true: after (re)connecting, fetch /api/snapshot again.
                "snapshot_required": True,
                "run_id": live.run_id if live is not None else 0,
            },
        )
        tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
