"""The gateway over a serial line: C's text lines in, the laptop's text lines out.

`SerialGateway` is `LaptopGateway` with a line codec in front of it. It does not know where the
bytes come from: on hardware a `SerialPortWorker` (transports/serial_port.py) reads the USB
port; in tests and in the simulator they come from the simulated C (`simulator/serial_device`)
through memory. Every line is checked completely before anything in it is used: a corrupted,
truncated or over-long line is dropped and counted, never half-applied. Log lines from C are
kept (bounded) for people to read, and never parsed.
"""

from collections import Counter, deque
from collections.abc import Callable
from typing import Any

from iot_backend.transports.laptop_gateway import Clock, LaptopGateway, UplinkConfig
from iot_backend.transports.serial_format import (
    MAX_UP_LINE,
    LineSplitter,
    ParsedLine,
    decode_up,
    encode,
)
from iot_backend.transports.wire import DownMessage


class SerialGateway(LaptopGateway):
    def __init__(
        self,
        clock: Clock,
        write: Callable[[bytes], None],
        config: UplinkConfig | None = None,
        log_lines: int = 50,
    ) -> None:
        super().__init__(clock, self._to_c, config)
        self._write = write
        self._splitter = LineSplitter(MAX_UP_LINE)
        self.lines: Counter[str] = Counter()  # "message", "log", "noise", "rejected:<reason>"
        self.c_log: deque[tuple[int, str]] = deque(maxlen=log_lines)  # (laptop ms, text)
        self.on_line: Callable[[ParsedLine], None] | None = None  # for the probe
        self.port_state = "open"  # set by whoever owns the port: open, closed, waiting
        self.port_name: str | None = None

    def _to_c(self, message: DownMessage) -> None:
        self._write(encode(message))

    def feed(self, data: bytes) -> None:
        """Bytes read from the port, in any chunking."""
        for raw in self._splitter.feed(data):
            if raw is None:
                self._handle(ParsedLine("rejected", reason="too_long"))
            else:
                self._handle(decode_up(raw))

    def port_closed(self) -> None:
        """The port went away. A line cut in the middle is counted and forgotten."""
        if self._splitter.flush() is not None:
            self._handle(ParsedLine("rejected", reason="truncated"))

    def _handle(self, parsed: ParsedLine) -> None:
        if parsed.kind == "rejected":
            self.lines[f"rejected:{parsed.reason}"] += 1
        else:
            self.lines[parsed.kind] += 1
        if parsed.kind in ("log", "noise"):
            self.c_log.append(
                (
                    self.now_ms(),
                    parsed.text if parsed.kind == "log" else f"(unframed) {parsed.text}",
                )
            )
        if self.on_line is not None:
            self.on_line(parsed)
        if parsed.kind == "message":
            self.receive(parsed.message)

    def describe_serial(self) -> dict[str, Any]:
        rejected = {
            k.split(":", 1)[1]: v for k, v in self.lines.items() if k.startswith("rejected:")
        }
        return {
            "port": self.port_name,
            "state": self.port_state,
            "lines": {
                "messages": self.lines["message"],
                "logs": self.lines["log"],
                "noise": self.lines["noise"],
                "rejected": rejected,
            },
            "c_log": [{"at_ms": at, "text": text} for at, text in self.c_log],
        }
