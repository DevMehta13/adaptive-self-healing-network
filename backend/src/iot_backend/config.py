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
    # "simulation" runs the live simulator. "hardware" reads node C's USB serial port
    # (serial_port) and refuses every presenter control with a clear error.
    mode: Literal["simulation", "hardware"] = "simulation"
    # Hardware mode (Phase 6-prep): the port C is on (COM3 on Windows, /dev/cu.usbserial-...
    # on macOS, or a pyserial URL such as socket://127.0.0.1:7777 for the fake C device).
    serial_port: str = ""
    serial_baud: int = Field(default=115_200, ge=1_200, le=2_000_000)
    # The simulated C can also speak the serial line format in simulation mode ("serial").
    live_transport: Literal["direct", "serial"] = "direct"
    heartbeat_interval_seconds: float = Field(default=2.0, gt=0)
    # Phase 9: the routing policy the controller starts with. Empty = whatever the scenario's
    # controller section says (the live-demo scenario: immediate_rtt, the Phase 4 behaviour).
    # It can also be changed while running with POST /api/policy.
    routing_policy: Literal["", "immediate_rtt", "lowest_delay", "highest_reliability"] = ""

    # Run history (Phase 10): a SQLite file with each run's configuration, events and
    # readings, exportable as CSV. Empty = no history is kept. The limits bound the file.
    history_db: str = ""
    history_max_runs: int = Field(default=10, ge=1)
    history_max_events: int = Field(default=20_000, ge=1)
    history_max_readings: int = Field(default=10_000, ge=1)

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
