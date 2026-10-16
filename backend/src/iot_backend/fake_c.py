"""A fake node C on a TCP port: the simulated network behind the real serial line format.

    uv run python -m iot_backend.fake_c                  # live-demo on 127.0.0.1:7777
    uv run python -m iot_backend.fake_c --scenario b-failure-auto-reroute --listen 127.0.0.1:7778

Then run the backend in hardware mode against it, exactly as against a board:

    IOT_MODE=hardware IOT_SERIAL_PORT=socket://127.0.0.1:7777 uv run python -m iot_backend

The simulated nodes A to D run in real time (the backend's controller uses the wall clock, so
the device must too). Node C's side speaks the serial protocol of docs/SERIAL_PROTOCOL.md over
the socket, byte for byte what the firmware is meant to send. There is no controller in here:
the routes come from the backend, through the line format, as they will on hardware.

Type commands on this program's standard input while it runs:

    stop B / start B           power a node off or on
    link A->B down / link A->B up
    unplug                     C loses power (its buffer is lost) and the connection closes
    replug                     C powers up again (a new boot) and accepts the laptop again
    noise <text>               send an unframed line (like the ESP8266's boot garbage)
    garble                     corrupt the next line C sends (one byte changed)
    status                     what C holds and has sent
    quit
"""

import argparse
import asyncio
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from iot_backend.simulator import Simulation
from iot_backend.simulator.scenario_file import apply_actions, load_scenario, parse_link
from iot_backend.simulator.serial_device import SerialCDevice

_REPO_SCENARIOS = Path(__file__).resolve().parents[3] / "scenarios"
# Scenario actions that belong to the network, not to the laptop side.
_NETWORK_ACTIONS = {"stop_node", "start_node", "restart_node", "link_down", "set_link"}


class FakeCServer:
    def __init__(
        self,
        scenario: Path,
        host: str = "127.0.0.1",
        port: int = 7777,
        seed: int | None = None,
        tick_s: float = 0.02,
        echo: Callable[[str], None] = print,
    ) -> None:
        spec = load_scenario(scenario)
        spec.config.transport = "external"
        if seed is not None:
            spec.config.seed = seed
        self.sim = Simulation(spec.config)
        apply_actions(self.sim, [a for a in spec.actions if a["action"] in _NETWORK_ACTIONS])
        self.device: SerialCDevice = self.sim.link  # type: ignore[assignment]
        self.device.connected = False  # nobody is listening until the laptop connects
        self.device.write_up = self._send
        self._host, self.port = host, port
        self._tick_s = tick_s
        self._echo = echo
        self._writer: asyncio.StreamWriter | None = None
        self.plugged = True
        self.connections = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stopping: asyncio.Event | None = None

    # ---- the socket (C's USB serial line) ----

    def _send(self, data: bytes) -> None:
        if self._writer is not None:
            self._writer.write(data)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.plugged or self._writer is not None:
            writer.close()  # unplugged, or a second laptop: refused
            return
        self._writer = writer
        self.connections += 1
        self.device.connected = True
        self._echo("[fake C] laptop connected")
        try:
            while data := await reader.read(4096):
                self.device.feed_down(data)
        except (ConnectionError, OSError):
            pass
        finally:
            if self._writer is writer:
                self._writer = None
                self.device.connected = False
                self._echo("[fake C] laptop disconnected")
            writer.close()

    def _drop_connection(self) -> None:
        if self._writer is not None:
            writer, self._writer = self._writer, None
            self.device.connected = False
            writer.close()

    # ---- time ----

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        server = await asyncio.start_server(self._client, self._host, self.port)
        self.port = server.sockets[0].getsockname()[1]
        self._echo(f"[fake C] listening on socket://{self._host}:{self.port} (SIMULATION)")
        self._ready.set()
        last = self._loop.time()
        async with server:
            while not self._stopping.is_set():
                await asyncio.sleep(self._tick_s)
                now = self._loop.time()
                self.sim.run(min(5_000, int((now - last) * 1000)))
                last = now
        self._drop_connection()

    def start_in_thread(self) -> "FakeCServer":
        """For tests: run in a background thread until `stop()`."""
        thread = threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True)
        thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("fake C did not start")
        self._thread = thread
        return self

    def stop(self) -> None:
        if self._loop is not None and self._stopping is not None:
            self._loop.call_soon_threadsafe(self._stopping.set)
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(5)

    def call(self, function: Callable[[], None]) -> None:
        """Run `function` on the device's event loop (thread-safe)."""
        assert self._loop is not None
        done = threading.Event()

        def run() -> None:
            try:
                function()
            finally:
                done.set()

        self._loop.call_soon_threadsafe(run)
        done.wait(5)

    # ---- commands ----

    def command(self, text: str) -> str:
        words = text.strip().split()
        if not words:
            return ""
        verb, rest = words[0].lower(), words[1:]
        if verb in ("stop", "start") and rest:
            node = rest[0].upper()
            (self.sim.stop_node if verb == "stop" else self.sim.start_node)(node)
            return f"{verb} {node}"
        if verb == "link" and len(rest) == 2:
            src, dst = parse_link(rest[0])
            self.sim.set_link_down(src, dst, rest[1].lower() == "down")
            return f"link {src}->{dst} {rest[1]}"
        if verb == "unplug":
            self.plugged = False
            self._drop_connection()
            self.sim.stop_node("C")  # the cable is C's power
            return "C unplugged: powered off, buffer lost"
        if verb == "replug":
            self.plugged = True
            self.sim.start_node("C")
            return "C plugged in: new boot"
        if verb == "noise":
            self._send(" ".join(rest).encode("ascii", "replace") + b"\n")
            return "noise sent"
        if verb == "garble":
            self.device.fault = self._garble_once
            return "the next line will be corrupted"
        if verb == "status":
            b = self.device.bridge
            return (
                f"boot {b.boot:08x} reachable={b.reachable} held={b.held} unacked={b.unacked} "
                f"sent up to {b.next_n - 1}, acknowledged {b.acked}"
            )
        return f"unknown command {text!r}"

    def _garble_once(self, data: bytes) -> bytes:
        self.device.fault = None
        if len(data) > 10:
            middle = len(data) // 2
            data = data[:middle] + bytes([data[middle] ^ 0x01]) + data[middle + 1 :]
        return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A fake node C speaking the serial protocol.")
    parser.add_argument("--scenario", default="live-demo", help="name in scenarios/ or a path")
    parser.add_argument("--listen", default="127.0.0.1:7777", help="host:port")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    path = Path(args.scenario)
    if not path.suffix:
        path = _REPO_SCENARIOS / f"{args.scenario}.json"
    host, _, port = args.listen.rpartition(":")
    server = FakeCServer(path, host or "127.0.0.1", int(port), args.seed)

    def read_commands() -> None:
        for line in sys.stdin:
            if line.strip().lower() in ("quit", "exit"):
                server.stop()
                return
            if server._loop is not None:
                server._loop.call_soon_threadsafe(
                    lambda t=line: print(f"[fake C] {server.command(t)}")
                )

    threading.Thread(target=read_commands, daemon=True).start()
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
