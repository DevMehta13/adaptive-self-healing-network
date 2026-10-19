"""Read the run history and download a run's readings or events as CSV (Phase 10).

Read-only. The history exists only if `IOT_HISTORY_DB` names a file; otherwise these answer 409
`history_disabled`.
"""

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from iot_backend.api.control import error_response
from iot_backend.storage import KINDS, HistoryError, RunStore, csv_lines

router = APIRouter(prefix="/api/runs")


def _store(request: Request) -> RunStore | None:
    return getattr(request.app.state, "history", None)


def _disabled() -> JSONResponse:
    return error_response(
        "history_disabled", "no history database is configured (set IOT_HISTORY_DB)", 409
    )


@router.get("", response_model=None)
def list_runs(request: Request) -> Any:
    store = _store(request)
    if store is None:
        return _disabled()
    return {"runs": store.runs(), "limits": vars(store.limits)}


@router.get("/{run_id}", response_model=None)
def get_run(request: Request, run_id: int) -> Any:
    store = _store(request)
    if store is None:
        return _disabled()
    try:
        return store.run(run_id)
    except HistoryError as error:
        return error_response("unknown_run", str(error), 404)


@router.get("/{run_id}/export", response_model=None)
def export(request: Request, run_id: int, kind: str = Query("readings")) -> Any:
    store = _store(request)
    if store is None:
        return _disabled()
    if kind not in KINDS:
        return error_response("bad_request", f"kind must be one of {list(KINDS)}", 400)
    try:
        store.run(run_id)
    except HistoryError as error:
        return error_response("unknown_run", str(error), 404)
    return StreamingResponse(
        csv_lines(store, run_id, kind),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="run-{run_id}-{kind}.csv"'},
    )
