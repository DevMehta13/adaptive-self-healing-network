"""Runtime configuration, read from IOT_* environment variables or a local .env file."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/iot_backend/config.py -> the repository root holds scenarios/
_DEFAULT_SCENARIOS = Path(__file__).resolve().parents[3] / "scenarios"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IOT_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: str = "http://localhost:5173"
    # "simulation" runs the live simulator. "hardware" does not exist yet: the backend then
    # serves status only and rejects every presenter control with a clear error.
    mode: Literal["simulation", "hardware"] = "simulation"
    heartbeat_interval_seconds: float = Field(default=2.0, gt=0)

    # Live simulation (Phase 5a)
    live_autostart: bool = True  # pace the simulation against the wall clock at startup
    live_scenario: str = "live-demo"
    live_speed: float = Field(default=1.0, ge=0.1, le=1000.0)
    live_tick_seconds: float = Field(default=0.05, gt=0)
    live_history: int = Field(default=2000, ge=10)  # state-changing events kept for catch-up
    scenarios_dir: str = ""  # empty = the repository's scenarios/ folder

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def scenarios_path(self) -> Path:
        return Path(self.scenarios_dir) if self.scenarios_dir else _DEFAULT_SCENARIOS
