"""The simulated gateway: virtual node C handing its messages to the laptop side directly.

The laptop side is `transports.LaptopGateway`, the same code that runs behind the serial line
on hardware; only the carrier differs. Here C's bridge (`bridge.py`) hands its wire messages
straight to it, and the laptop's heartbeats, acknowledgements and commands go straight back.
`simulator/serial_device.py` is the other carrier: the same messages as serial text lines.

`connected = False` models the laptop side being gone (the controller process stopped, or its
serial reader): everything going down and every message going up is lost, exactly as it would
be on hardware. C notices by the missing heartbeats, not by this flag.
"""

from iot_backend.simulator.bridge import BridgeConfig, CBridge
from iot_backend.simulator.engine import Engine
from iot_backend.transports.laptop_gateway import EngineClock, LaptopGateway, UplinkConfig
from iot_backend.transports.wire import DownMessage, HeartbeatDown, UpMessage


class SimulatedGateway(LaptopGateway):
    def __init__(
        self,
        engine: Engine,
        bridge: BridgeConfig | None = None,
        uplink: UplinkConfig | None = None,
    ) -> None:
        super().__init__(EngineClock(engine), self._to_c, uplink)
        self._engine = engine
        self.bridge = CBridge(engine, bridge or BridgeConfig(), self._to_laptop)
        self.connected = True
        self.events_lost = 0  # messages from C lost while the laptop side was gone
        self.commands_lost = 0  # heartbeats, acknowledgements and commands lost
        # Test aid: the heartbeat path alone is cut while the up path still works.
        self.heartbeats_blocked = False

    def _to_c(self, message: DownMessage) -> None:
        if not self.connected or (self.heartbeats_blocked and isinstance(message, HeartbeatDown)):
            self.commands_lost += 1
            return
        # Delivered on the next turn of the engine, as a line arriving on C's serial port is.
        self._engine.schedule(0, lambda: self.bridge.receive(message))

    def _to_laptop(self, message: UpMessage) -> None:
        if not self.connected:
            self.events_lost += 1
            return
        self.receive(message)
