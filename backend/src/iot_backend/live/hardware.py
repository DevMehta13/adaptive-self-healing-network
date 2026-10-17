"""Hardware mode: the same controller and dashboard model, fed by node C's USB serial port.

Nothing here is simulated. The controller (topology view, route controller, heartbeat session,
reading ledger) is built exactly as in a simulated run, on a `SerialGateway` instead of a
`SimulatedGateway`, so the controller code is the same in both modes. Timers run on the asyncio
event loop; the serial port is read by a background thread that hands bytes to the loop.

Times: there is no simulated clock. Every `sim_ms` in hardware mode is milliseconds on the
laptop's own clock since the backend started (the field name is kept so the dashboard has one
shape in both modes). Presenter controls are refused: there is no environment to change.

If the USB device disappears the gateway reports the port closed, the controller's own
heartbeat timer declares the link to C lost, and the port is retried every second; when it
comes back, C's reliable uplink fills the gap (or names what is gone).
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from iot_backend.controller import (
    GatewaySession,
    ReadingLedger,
    RouteController,
    RoutingConfig,
    TopologyView,
)
from iot_backend.live.model import DashboardModel
from iot_backend.simulator.scenario_file import load_scenario
from iot_backend.transports.laptop_gateway import LaptopGateway, UplinkConfig
from iot_backend.transports.serial_format import DEFAULT_BAUD
from iot_backend.transports.serial_gateway import SerialGateway
from iot_backend.transports.serial_port import SerialPortWorker, list_ports

HARDWARE_LABEL = (
    "HARDWARE: messages from real boards through node C's USB serial port. Times are the "
    "laptop's clock since the backend started."
)


class AsyncioClock:
    """The laptop's monotonic clock, with timers on the running event loop."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        loop = self._loop or asyncio.get_running_loop()
        loop.call_later(max(0, delay_ms) / 1000, callback)


@dataclass(frozen=True)
class HardwareConfig:
    controller_scenario: Path  # the scenario file whose `controller` section configures routing
    port: str = ""  # COM3, /dev/cu.usbserial-0001, socket://127.0.0.1:7777, ...
    baud: int = DEFAULT_BAUD
    history: int = 2_000
    recent_readings: int = 50
    recent_events_in_snapshot: int = 100
    policy: str = ""  # routing policy override ("" = the scenario's controller section)


class LiveHardware(DashboardModel):
    mode = "hardware"
    label = HARDWARE_LABEL

    def __init__(self, config: HardwareConfig) -> None:
        super().__init__(config.history, config.recent_readings, config.recent_events_in_snapshot)
        self._config = config
        spec = load_scenario(config.controller_scenario)
        # A new epoch for every start of the backend: its start time in seconds (PROTOCOL.md).
        self._routing: RoutingConfig = replace(
            spec.controller, epoch=max(1, int(time.time()) & 0xFFFF_FFFF)
        )
        if config.policy:
            self._routing = replace(self._routing, policy=config.policy)
        self.run_id = 1
        self.scenario = None
        self._clock = AsyncioClock()
        self.gateway = SerialGateway(
            self._clock, self._write, UplinkConfig(data_ttl=spec.config.ttl)
        )
        self.gateway.port_name = config.port or None
        self.gateway.port_state = "waiting" if config.port else "no_port"
        self.gateway.subscribe(self._on_gateway_event)
        self._worker: SerialPortWorker | None = None
        self._started = False

    @property
    def sim_ms(self) -> int:
        return self._clock.now_ms()

    def start(self) -> None:
        """Start the controller and open the port (inside the running event loop)."""
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._clock.bind(loop)
        view = TopologyView(self.gateway)
        controller = RouteController(view, self.gateway, self._routing)
        ledger = ReadingLedger(self.gateway)
        session = GatewaySession(
            self.gateway, self._routing.heartbeat_interval_ms, self._routing.link_timeout_ms
        )
        self._on_controller(view, controller, session, ledger)
        if self._config.port:
            self._worker = SerialPortWorker(
                self._config.port,
                self._config.baud,
                on_bytes=self.gateway.feed,
                on_state=self._on_port_state,
                post=loop.call_soon_threadsafe,
            )
            self._worker.start()

    async def stop(self) -> None:
        if self._controller is not None:
            self._controller.stop()
        if self._session is not None:
            self._session.stop()
        if self._worker is not None:
            await asyncio.to_thread(self._worker.stop)
            self._worker = None

    def _write(self, data: bytes) -> None:
        if self._worker is not None:
            self._worker.write(data)

    def _on_port_state(self, state: str, detail: str) -> None:
        self.gateway.port_state = state
        if state == "closed":
            self.gateway.port_closed()
        self._emit(
            "serial_port", {"port": self._config.port or None, "state": state, "detail": detail}
        )

    def _laptop_gateway(self) -> LaptopGateway:
        return self.gateway

    def _gateway_state(self) -> dict[str, Any]:
        state = super()._gateway_state()
        serial = state.setdefault("serial", self.gateway.describe_serial())
        if self._worker is not None:
            serial.update(
                opens=self._worker.opens,
                closes=self._worker.closes,
                writes_dropped=self._worker.writes_dropped,
            )
        if self.gateway.port_state != "open":
            serial["available_ports"] = [
                {"device": p.device, "description": p.description, "chip": p.chip}
                for p in list_ports()
            ]
        return state

    def _presenter(self) -> dict[str, Any]:
        return {
            "channel": "environment",
            "note": "Hardware mode: there is no simulated environment, so nothing is injected.",
            "conditions": None,
        }
