"""HTTP endpoints for the dashboard (read-only). Presenter controls are in control.py."""

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from iot_backend import __version__
from iot_backend.api.control import error_response
from iot_backend.envelope import PROTOCOL_VERSION, utc_now
from iot_backend.live import LiveSimulation

router = APIRouter()


def _live(request: Request) -> LiveSimulation | None:
    return getattr(request.app.state, "live", None)


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    return {"status": "ok", "mode": request.app.state.settings.mode, "version": __version__}


@router.get("/api/snapshot")
def snapshot(request: Request) -> dict[str, Any]:
    live = _live(request)
    base = {
        "v": PROTOCOL_VERSION,
        "mode": request.app.state.settings.mode,
        "backend": {"status": "ok", "version": __version__},
        "server_time": utc_now(),
    }
    if live is None:  # hardware mode: nothing is simulated, nothing is known
        return {
            **base,
            "run_id": 0,
            "scenario": None,
            "last_event_id": 0,
            "nodes": [],
            "links": [],
        }
    return {**base, **live.snapshot()}


@router.get("/api/events", response_model=None)
def events(
    request: Request,
    after_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
) -> Any:
    """State-changing events after `after_id`, oldest first (bounded history).

    `complete: false` means the history no longer reaches back that far: fetch the snapshot.
    """
    live = _live(request)
    if live is None:
        return error_response("not_available_in_hardware_mode", "no simulation is running", 409)
    selected, complete = live.events_after(after_id, limit)
    return JSONResponse(
        {
            "events": [{"event_id": e.event_id, "type": e.type, "data": e.data} for e in selected],
            "last_event_id": live.last_event_id,
            "oldest_event_id": live.oldest_event_id,
            "complete": complete,
            "run_id": live.run_id,
        }
    )


@router.get("/api/scenarios", response_model=None)
def scenarios(request: Request) -> Any:
    live = _live(request)
    if live is None:
        return error_response("not_available_in_hardware_mode", "no simulation is running", 409)
    return {"scenarios": live.scenario_names(), "current": live.scenario}
