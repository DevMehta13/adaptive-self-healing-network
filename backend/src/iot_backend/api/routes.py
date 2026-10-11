"""HTTP endpoints for the dashboard."""

from typing import Any

from fastapi import APIRouter, Request

from iot_backend import __version__
from iot_backend.envelope import PROTOCOL_VERSION, utc_now

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    return {"status": "ok", "mode": request.app.state.settings.mode, "version": __version__}


@router.get("/api/snapshot")
def snapshot(request: Request) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "mode": request.app.state.settings.mode,
        "backend": {"status": "ok", "version": __version__},
        "server_time": utc_now(),
        "last_event_id": 0,  # no state-changing events exist yet
        "nodes": [],
        "links": [],
    }
