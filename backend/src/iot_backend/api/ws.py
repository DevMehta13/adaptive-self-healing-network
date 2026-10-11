"""Live event stream: one status event on connect, then periodic heartbeats."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from iot_backend import __version__
from iot_backend.envelope import make_envelope

router = APIRouter()


@router.websocket("/ws")
async def events(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    interval = settings.heartbeat_interval_seconds
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

    try:
        while True:
            # Waiting on the client doubles as the sleep between heartbeats and lets us
            # notice a disconnect promptly. Anything the client sends is ignored.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=interval)
            except TimeoutError:
                pass
            seq += 1
            await websocket.send_json(make_envelope(seq, "heartbeat", {"interval_s": interval}))
    except WebSocketDisconnect:
        return
