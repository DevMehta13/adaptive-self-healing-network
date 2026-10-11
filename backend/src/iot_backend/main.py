"""Application factory and server entry point."""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from iot_backend import __version__
from iot_backend.api import routes, ws
from iot_backend.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="IoT backend", version=__version__)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(routes.router)
    app.include_router(ws.router)
    return app


def run() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
