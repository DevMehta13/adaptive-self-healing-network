"""Opening, reading and re-opening a serial port (pyserial), and listing the ports there are.

`SerialPortWorker` owns one port in a background thread. It hands every chunk of bytes it reads
to a callback through `post` (in the backend, `loop.call_soon_threadsafe`, so all gateway logic
runs on the event loop), writes queued lines, and when the device disappears (USB unplugged, or
the far end of a `socket://` test port closes) it reports the port closed and keeps trying to
open it again. Nothing here raises into the backend.

Port names are never hard-coded: `COM3` on Windows, `/dev/cu.usbserial-0001` or similar on
macOS, or any pyserial URL such as `socket://127.0.0.1:7777` (the fake C device) or `loop://`.

Opening a port normally asserts DTR and RTS, and on a NodeMCU those lines are wired to reset
the ESP8266: opening the port would reboot C and lose its buffer. The worker therefore clears
both before opening. Whether this avoids the reset on every USB chip and operating system must
be checked on the boards (Phase 6).
"""

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

import serial
from serial.tools import list_ports as _list_ports

# USB-to-serial chips found on NodeMCU boards (vendor id -> name), to point at likely ports.
KNOWN_CHIPS = {0x1A86: "CH340/CH341", 0x10C4: "CP210x", 0x0403: "FTDI"}


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str
    chip: str | None  # a USB-serial chip a NodeMCU uses, if recognised


def list_ports() -> list[PortInfo]:
    """The serial ports this computer has now (Windows: COMn; macOS: /dev/cu.*)."""
    found = []
    for port in sorted(_list_ports.comports(), key=lambda p: p.device):
        chip = KNOWN_CHIPS.get(port.vid) if port.vid is not None else None
        found.append(PortInfo(port.device, port.description or "", port.hwid or "", chip))
    return found


def open_port(name: str, baud: int, timeout_s: float = 0.1) -> serial.SerialBase:
    """Open a port or pyserial URL without pulsing the board's reset lines."""
    port = serial.serial_for_url(name, baudrate=baud, timeout=timeout_s, do_not_open=True)
    port.write_timeout = 1.0
    port.dtr = False  # on a NodeMCU, DTR/RTS reset the ESP8266
    port.rts = False
    port.open()
    return port


class SerialPortWorker:
    def __init__(
        self,
        name: str,
        baud: int,
        on_bytes: Callable[[bytes], None],
        on_state: Callable[[str, str], None],
        post: Callable[[Callable[[], None]], None],
        retry_s: float = 1.0,
        opener: Callable[[str, int], serial.SerialBase] = open_port,
    ) -> None:
        self.name = name
        self._baud = baud
        self._on_bytes = on_bytes
        self._on_state = on_state  # (state, detail): "open", "closed", "waiting"
        self._post = post
        self._retry_s = retry_s
        self._opener = opener
        self._writes: queue.Queue[bytes] = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.opens = 0
        self.closes = 0
        self.writes_dropped = 0  # lines not written because the port was not open
        self.state = "waiting"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name=f"serial {self.name}", daemon=True
            )
            self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    def write(self, data: bytes) -> None:
        """Queue one line. If the port is not open it is dropped: stale commands are useless."""
        if self.state != "open":
            self.writes_dropped += 1
            return
        try:
            self._writes.put_nowait(data)
        except queue.Full:
            self.writes_dropped += 1

    def _set_state(self, state: str, detail: str = "") -> None:
        if state == self.state:
            return
        self.state = state
        self._post(lambda: self._on_state(state, detail))

    def _run(self) -> None:
        port: serial.SerialBase | None = None
        while not self._stop.is_set():
            if port is None:
                try:
                    port = self._opener(self.name, self._baud)
                except (serial.SerialException, OSError, ValueError) as error:
                    self._set_state("waiting", str(error))
                    self._stop.wait(self._retry_s)
                    continue
                self.opens += 1
                self._drain_stale()
                self._set_state("open")
            try:
                while not self._writes.empty():
                    port.write(self._writes.get_nowait())
                data = port.read(max(1, port.in_waiting))
                if data:
                    self._post(lambda data=data: self._on_bytes(data))
            except (serial.SerialException, OSError) as error:
                self.closes += 1
                try:
                    port.close()
                except (serial.SerialException, OSError):
                    pass
                port = None
                self._set_state("closed", str(error))
                self._stop.wait(self._retry_s)
        if port is not None:
            try:
                port.close()
            except (serial.SerialException, OSError):
                pass

    def _drain_stale(self) -> None:
        while not self._writes.empty():
            self._writes.get_nowait()
            self.writes_dropped += 1
