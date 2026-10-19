"""Presenter controls: change the simulated environment, nothing else.

These endpoints never call the controller. The controller has to discover the effect of an
action through the gateway, exactly as it would on hardware.

State-changing requests are protected three ways: the server binds to localhost by default; the
`Origin` header, when present, must be an allowed origin (a browser always sends it on a
cross-site POST); and the body must be `application/json`, which a plain HTML form cannot send.
"""

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from iot_backend.live import ControlError, LiveSimulation

router = APIRouter(prefix="/api/control")


def error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _live(request: Request) -> LiveSimulation:
    settings = request.app.state.settings
    live: LiveSimulation | None = getattr(request.app.state, "live", None)
    if settings.mode != "simulation" or live is None:
        raise ControlError(
            "not_available_in_hardware_mode",
            "presenter controls change the simulated environment; there is no simulator in "
            "hardware mode",
            409,
        )
    return live


def _guard(request: Request) -> None:
    """Origin and content-type checks for every state-changing request."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in request.app.state.settings.cors_origin_list:
        raise ControlError("origin_not_allowed", f"origin {origin!r} may not change state", 403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ControlError("origin_not_allowed", "cross-site requests may not change state", 403)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise ControlError(
            "json_required", "send the body as application/json (form posts are refused)", 415
        )


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as error:
        raise ControlError("bad_request", f"body is not valid JSON: {error}") from error
    if not isinstance(body, dict):
        raise ControlError("bad_request", "body must be a JSON object")
    return body


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        raise ControlError("bad_request", f"unknown fields {sorted(extra)}")


def _result(live: LiveSimulation, applied: str) -> dict[str, Any]:
    return {
        "ok": True,
        "applied": applied,
        "run_id": live.run_id,
        "last_event_id": live.last_event_id,
        "clock": {"sim_ms": live.sim_ms, "speed": live.speed, "paused": live.paused},
    }


async def _handle(request: Request, name: str, fields: set[str], action: Any) -> JSONResponse:
    try:
        _guard(request)
        live = _live(request)
        body = await _body(request)
        _only(body, fields)
        action(live, body)
        return JSONResponse(_result(live, name))
    except ControlError as error:
        return error_response(error.code, error.message, error.status)


@router.post("/node")
async def node(request: Request) -> JSONResponse:
    def act(live: LiveSimulation, body: dict[str, Any]) -> None:
        verb = body.get("action")
        if verb == "stop":
            live.stop_node(body.get("node"))
        elif verb == "start":
            live.start_node(body.get("node"))
        else:
            raise ControlError("bad_request", "action must be 'stop' or 'start'")

    return await _handle(request, "node", {"node", "action"}, act)


@router.post("/link")
async def link(request: Request) -> JSONResponse:
    return await _handle(
        request,
        "link",
        {"link", "down"},
        lambda live, body: live.link_down(body.get("link"), body.get("down")),
    )


@router.post("/link-config")
async def link_config(request: Request) -> JSONResponse:
    return await _handle(
        request,
        "link-config",
        {"link", "loss", "delay_ms"},
        lambda live, body: live.link_config(
            body.get("link"), body.get("loss"), body.get("delay_ms")
        ),
    )


@router.post("/gateway")
async def gateway(request: Request) -> JSONResponse:
    return await _handle(
        request, "gateway", {"attached"}, lambda live, body: live.set_gateway(body.get("attached"))
    )


@router.post("/pause")
async def pause(request: Request) -> JSONResponse:
    return await _handle(request, "pause", set(), lambda live, body: live.pause())


@router.post("/resume")
async def resume(request: Request) -> JSONResponse:
    return await _handle(request, "resume", set(), lambda live, body: live.resume())


@router.post("/speed")
async def speed(request: Request) -> JSONResponse:
    return await _handle(
        request, "speed", {"factor"}, lambda live, body: live.set_speed(body.get("factor"))
    )


@router.post("/reset")
async def reset(request: Request) -> JSONResponse:
    return await _handle(
        request, "reset", {"scenario"}, lambda live, body: live.reset(body.get("scenario"))
    )


@router.post("/step")
async def step(request: Request) -> JSONResponse:
    """Advance simulated time by a fixed amount now (tests, debugging; works while paused)."""

    def act(live: LiveSimulation, body: dict[str, Any]) -> None:
        ms = body.get("ms")
        if isinstance(ms, bool) or not isinstance(ms, int) or not 0 < ms <= 3_600_000:
            raise ControlError("bad_request", "ms must be an integer from 1 to 3600000")
        live.advance(ms)

    return await _handle(request, "step", {"ms"}, act)
