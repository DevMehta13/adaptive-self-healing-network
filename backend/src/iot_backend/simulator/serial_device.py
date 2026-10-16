"""Simulated node C behind the serial line format.

The same C bridge as in the direct simulation, but everything it sends becomes a text line
(`transports/serial_format.py`) written to a byte stream, and everything it receives is parsed
from lines, exactly as the firmware will do. Running a scenario through this and a
`SerialGateway` exercises the real serial path end to end without boards.

`connected = False` models the laptop side being gone (bytes in both directions are lost).
`fault` is a hook that may change the bytes on their way up (tests inject corruption, cut
lines, interleave log lines or flood the line with it).
"""

from collections import Counter
from collections.abc import Callable

from iot_backend.simulator.bridge import BridgeConfig, CBridge
from iot_backend.simulator.engine import Engine
from iot_backend.transports.serial_format import (
    MAX_DOWN_LINE,
    LineSplitter,
    decode_down,
    encode,
    log_line,
)
from iot_backend.transports.wire import HeartbeatDown, UpMessage


class SerialCDevice:
    def __init__(
        self,
        engine: Engine,
        config: BridgeConfig,
        write_up: Callable[[bytes], None],
        logs: bool = True,
    ) -> None:
        self._engine = engine
        self._write_up = write_up
        self._logs = logs
        self.bridge = CBridge(engine, config, self._up, log=self._log)
        self._splitter = LineSplitter(MAX_DOWN_LINE)
        self.connected = True
        self.heartbeats_blocked = False  # test aid, as in SimulatedGateway
        self.events_lost = 0
        self.commands_lost = 0
        self.down_lines: Counter[str] = Counter()  # what C made of the laptop's lines
        self.fault: Callable[[bytes], bytes] | None = None

    def _write(self, data: bytes) -> None:
        if not self.connected:
            self.events_lost += 1
            return
        if self.fault is not None:
            data = self.fault(data)
        if data:
            self._write_up(data)

    def _up(self, message: UpMessage) -> None:
        self._write(encode(message))

    def _log(self, text: str) -> None:
        if self._logs:
            self._write(log_line(text))

    def feed_down(self, data: bytes) -> None:
        """Bytes the laptop wrote to C's serial port."""
        if not self.connected:
            self.commands_lost += 1
            return
        for raw in self._splitter.feed(data):
            parsed = decode_down(raw) if raw is not None else None
            if parsed is None or parsed.kind != "message":
                self.down_lines["rejected" if parsed is None else parsed.kind] += 1
                continue
            message = parsed.message
            if self.heartbeats_blocked and isinstance(message, HeartbeatDown):
                self.commands_lost += 1
                continue
            self.down_lines["message"] += 1
            self._engine.schedule(0, lambda m=message: self.bridge.receive(m))
