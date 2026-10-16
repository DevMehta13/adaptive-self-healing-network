"""A serial console for bringing node C up: see what it says, and talk to it like the backend.

    uv run python -m iot_backend.serial_probe --list
    uv run python -m iot_backend.serial_probe --port COM3                    # Windows
    uv run python -m iot_backend.serial_probe --port /dev/cu.usbserial-0001  # macOS
    uv run python -m iot_backend.serial_probe --port socket://127.0.0.1:7777 # the fake C

Every line C sends is shown once, sorted by kind: `DATA` (a checked data line, decoded),
`LOG` (C's # lines), `NOISE` (anything unframed, such as the ESP8266 boot ROM's text) and
`BAD` (a line that looked like data and failed a check, with the reason). The probe sends a
heartbeat every second with the cumulative acknowledgement and asks for gaps, exactly as the
backend does, so C behaves as if the controller were there. `--no-heartbeat` lets you watch C
decide that the controller is unreachable.

Commands typed while it runs:

    route <node> <destination> <next> [backup]   have C send a ROUTE_UPDATE (version counts up)
    noroute <node> <destination>                 the same, saying "no route"
    hb                                           one heartbeat now
    raw <json object>                            send it with a correct checksum (test C's parser)
    rawbad <json object>                         send it with a wrong checksum
    stats                                        counters so far
    quit
"""

import argparse
import asyncio
import sys
import threading
import time

from iot_backend.protocol import NODE_IDS, NODE_NAMES, MessageType, decode
from iot_backend.transports import Heartbeat, RouteUpdateCommand
from iot_backend.transports.serial_format import DEFAULT_BAUD, ParsedLine, crc16
from iot_backend.transports.serial_gateway import SerialGateway
from iot_backend.transports.serial_port import SerialPortWorker, list_ports
from iot_backend.transports.wire import FrameUp


def _node(text: str) -> int:
    key = text.upper()
    if key in NODE_IDS:
        return NODE_IDS[key]
    return int(text)


def describe(parsed: ParsedLine) -> str:
    """One readable line for whatever C sent."""
    if parsed.kind == "log":
        return f"LOG   {parsed.text}"
    if parsed.kind == "noise":
        return f"NOISE {parsed.text[:120]}"
    if parsed.kind == "rejected":
        return f"BAD   {parsed.reason} {parsed.detail}: {parsed.text[:100]}"
    m = parsed.message
    if isinstance(m, FrameUp):
        frame = decode(m.frame)
        who = NODE_NAMES.get(frame.src, frame.src)
        via = NODE_NAMES.get(m.via, m.via)
        late = f" late {m.age_ms} ms" if m.replayed else ""
        kind = frame.msg_type.name
        detail = ""
        if frame.msg_type is MessageType.DATA:
            detail = f" seq {frame.seq} value {frame.payload.value / 100:.2f} ttl {frame.ttl}"
            detail += " (repeat)" if m.duplicate else ""
        return f"DATA  #{m.n} {kind} from {who} via {via}{detail}{late}"
    fields = {k: v for k, v in vars(m).items() if k != "boot"}
    return f"DATA  {type(m).__name__} {fields}"


async def run(port: str, baud: int, heartbeat_ms: int, seconds: float, commands: list[str]) -> int:
    from iot_backend.live.hardware import AsyncioClock

    loop = asyncio.get_running_loop()
    clock = AsyncioClock()
    clock.bind(loop)
    started = time.monotonic()
    worker: SerialPortWorker | None = None
    gateway = SerialGateway(clock, lambda data: worker.write(data) if worker else None)

    def show(text: str) -> None:
        print(f"[{time.monotonic() - started:8.3f}] {text}", flush=True)

    gateway.on_line = lambda parsed: show(describe(parsed))
    worker = SerialPortWorker(
        port,
        baud,
        on_bytes=gateway.feed,
        on_state=lambda state, detail: (
            show(f"PORT  {state} {detail}".rstrip()),
            gateway.port_closed() if state == "closed" else None,
        ),
        post=loop.call_soon_threadsafe,
    )
    worker.start()
    epoch = max(1, int(time.time()) & 0xFFFF_FFFF)
    version = 0
    done = asyncio.Event()

    def command(text: str) -> None:
        nonlocal version
        words = text.split()
        if not words:
            return
        verb = words[0].lower()
        try:
            if verb in ("route", "noroute"):
                version += 1
                node, dst = _node(words[1]), _node(words[2])
                hop = None if verb == "noroute" else _node(words[3])
                backup = _node(words[4]) if verb == "route" and len(words) > 4 else None
                gateway.send_route_update(
                    RouteUpdateCommand(node, epoch, version, dst, hop, backup)
                )
                show(f"SENT  route update v{version} for {words[1]} (epoch {epoch})")
            elif verb == "hb":
                gateway.send_heartbeat(Heartbeat(clock.now_ms()))
            elif verb in ("raw", "rawbad"):
                body = text.split(None, 1)[1].encode("ascii")
                check = crc16(body) ^ (0x0001 if verb == "rawbad" else 0)
                worker.write(body + f"*{check:04X}\n".encode("ascii"))
                show(f"SENT  {body.decode()}")
            elif verb == "stats":
                show(f"STATS lines {dict(gateway.lines)} uplink {gateway.describe()}")
            elif verb in ("quit", "exit"):
                done.set()
            else:
                show(f"?     unknown command {text!r}")
        except (IndexError, ValueError) as error:
            show(f"?     {error}")

    for text in commands:
        loop.call_later(1.5, command, text)

    def read_stdin() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(command, line.strip())

    threading.Thread(target=read_stdin, daemon=True).start()

    async def beat() -> None:
        while heartbeat_ms > 0:
            await asyncio.sleep(heartbeat_ms / 1000)
            gateway.send_heartbeat(Heartbeat(clock.now_ms()))

    beating = loop.create_task(beat())
    try:
        await asyncio.wait_for(done.wait(), timeout=seconds if seconds > 0 else None)
    except TimeoutError:
        pass
    finally:
        beating.cancel()
        await asyncio.to_thread(worker.stop)
    show(f"STATS lines {dict(gateway.lines)} uplink {gateway.describe()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serial console for node C.")
    parser.add_argument("--list", action="store_true", help="list the serial ports and exit")
    parser.add_argument("--port", help="COM3, /dev/cu.usbserial-..., or socket://host:port")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--no-heartbeat", action="store_true", help="do not send heartbeats")
    parser.add_argument("--heartbeat-ms", type=int, default=1000)
    parser.add_argument("--seconds", type=float, default=0, help="stop after this long")
    parser.add_argument(
        "--send", action="append", default=[], help="a command to run once at start (repeatable)"
    )
    args = parser.parse_args(argv)
    if args.list or not args.port:
        ports = list_ports()
        if not ports:
            print("no serial ports found (is the board plugged in, and its USB driver installed?)")
        for p in ports:
            chip = f"  <- {p.chip}, likely a NodeMCU" if p.chip else ""
            print(f"{p.device:28} {p.description}{chip}")
        if not args.port:
            return 0 if args.list else 2
    heartbeat = 0 if args.no_heartbeat else args.heartbeat_ms
    try:
        return asyncio.run(run(args.port, args.baud, heartbeat, args.seconds, args.send))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
