"""Application factory and server entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from iot_backend import __version__
from iot_backend.api import control, history, policy, routes, ws
from iot_backend.config import Settings
from iot_backend.live import LiveConfig, LiveSimulation
from iot_backend.live.hardware import HardwareConfig, LiveHardware
from iot_backend.storage import RunRecorder, RunStore, StoreLimits


def build_live(settings: Settings) -> LiveSimulation | LiveHardware:
    """The live runtime: the paced simulation, or the serial port in hardware mode."""
    if settings.mode == "hardware":
        return LiveHardware(
            HardwareConfig(
                controller_scenario=settings.scenarios_path / f"{settings.live_scenario}.json",
                port=settings.serial_port,
                baud=settings.serial_baud,
                history=settings.live_history,
                policy=settings.routing_policy,
            )
        )
    return LiveSimulation(
        LiveConfig(
            scenarios_dir=settings.scenarios_path,
            scenario=settings.live_scenario,
            speed=settings.live_speed,
            tick_s=settings.live_tick_seconds,
            history=settings.live_history,
            transport=settings.live_transport,
            policy=settings.routing_policy,
        )
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    live = build_live(settings)
    store = (
        RunStore(
            settings.history_db,
            StoreLimits(
                settings.history_max_runs,
                settings.history_max_events,
                settings.history_max_readings,
            ),
        )
        if settings.history_db
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Hardware always starts: the controller must run for as long as the backend does.
        recorder = RunRecorder(store, live) if store is not None else None
        if isinstance(live, LiveHardware) or settings.live_autostart:
            live.start()
        try:
            yield
        finally:
            await live.stop()
            if recorder is not None:
                recorder.close()
            if store is not None:
                store.close()

    app = FastAPI(title="IoT backend", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.live = live
    app.state.history = store
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )
    app.include_router(routes.router)
    app.include_router(control.router)
    app.include_router(policy.router)
    app.include_router(history.router)
    app.include_router(ws.router)
    return app


def run() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
