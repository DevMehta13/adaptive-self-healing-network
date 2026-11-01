"""Choose the routing policy while the backend runs (Phase 9).

This is a setting of the controller, not a change to the simulated environment, so it is not one
of the presenter controls: it works in hardware mode too, where those are refused. It has the
same protections as every state-changing request (localhost, `Origin`, JSON only).
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from iot_backend.api.control import _body, _guard, _only, error_response
from iot_backend.controller.routing import POLICIES
from iot_backend.live import ControlError, DashboardModel

router = APIRouter()


@router.post("/api/policy")
async def set_policy(request: Request) -> JSONResponse:
    try:
        _guard(request)
        live: DashboardModel | None = getattr(request.app.state, "live", None)
        controller = None if live is None else live.controller
        if live is None or controller is None:
            raise ControlError("not_available", "this run has no route controller", 409)
        body: dict[str, Any] = await _body(request)
        _only(body, {"policy"})
        policy = body.get("policy")
        if not isinstance(policy, str) or policy not in POLICIES:
            raise ControlError("bad_request", f"policy must be one of {list(POLICIES)}")
        changed = controller.set_policy(policy)
        return JSONResponse(
            {
                "ok": True,
                "policy": controller.policy,
                "changed": changed,
                "run_id": live.run_id,
                "last_event_id": live.last_event_id,
            }
        )
    except ControlError as error:
        return error_response(error.code, error.message, error.status)
