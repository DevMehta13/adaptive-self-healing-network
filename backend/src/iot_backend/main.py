"""Application factory and server entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from iot_backend import __version__
from iot_backend.api import control, routes, ws
from iot_backend.config import Settings
from iot_backend.live import LiveConfig, LiveSimulation


def build_live(settings: Settings) -> LiveSimulation | None:
    """The live simulation, or None in hardware mode (which has no simulator)."""
    if settings.mode != "simulation":
        return None
    return LiveSimulation(
        LiveConfig(
            scenarios_dir=settings.scenarios_path,
            scenario=settings.live_scenario,
            speed=settings.live_speed,
            tick_s=settings.live_tick_seconds,
            history=settings.live_history,
        )
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    live = build_live(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if live is not None and settings.live_autostart:
            live.start()  # pace the simulation against the wall clock
        try:
            yield
        finally:
            if live is not None:
                await live.stop()

    app = FastAPI(title="IoT backend", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.live = live
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )
    app.include_router(routes.router)
    app.include_router(control.router)
    app.include_router(ws.router)
    return app


def run() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
