"""Runtime configuration, read from IOT_* environment variables or a local .env file."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IOT_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: str = "http://localhost:5173"
    # Only simulation exists in Phase 0; later phases add a hardware mode.
    mode: Literal["simulation"] = "simulation"
    heartbeat_interval_seconds: float = Field(default=2.0, gt=0)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
