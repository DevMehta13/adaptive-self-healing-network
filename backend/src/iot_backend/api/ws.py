"""Live event stream: one status event on connect, then periodic heartbeats."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from iot_backend import __version__
from iot_backend.envelope import make_envelope

router = APIRouter()


@router.websocket("/ws")
async def events(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    interval = settings.heartbeat_interval_seconds

    # Browsers do not apply CORS to WebSockets, so check the Origin header ourselves.
    # A missing Origin (non-browser client, tests) is allowed.
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in settings.cors_origin_list:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    seq = 1  # numbering is per connection, so a new connection starts again at 1
    await websocket.send_json(
        make_envelope(
            seq,
            "status",
            {
                "backend": "ok",
                "mode": settings.mode,
                "version": __version__,
                "heartbeat_interval_s": interval,
                # Always true: after (re)connecting, fetch /api/snapshot again.
                "snapshot_required": True,
            },
        )
    )

    loop = asyncio.get_running_loop()
    next_beat = loop.time() + interval
    try:
        while True:
            # Heartbeats are paced by the clock alone. Waiting on the client lets us notice
            # a disconnect promptly; whatever it sends is ignored and does not shorten the wait.
            remaining = next_beat - loop.time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
                except TimeoutError:
                    pass
                continue
            seq += 1
            await websocket.send_json(make_envelope(seq, "heartbeat", {"interval_s": interval}))
            next_beat = loop.time() + interval
    except WebSocketDisconnect:
        return
