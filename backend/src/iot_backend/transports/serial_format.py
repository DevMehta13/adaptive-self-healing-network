"""The serial line format between node C and the laptop (docs/SERIAL_PROTOCOL.md, draft).

One message per line, newline-terminated. Three kinds of line:

* a **data line**: a compact JSON object followed by `*` and a CRC-16 of the JSON text in four
  hex digits, e.g. `{"v":1,"t":"hb","b":0,"ack":0}*1D2A`. Only these are ever parsed as data,
  and only when every check passes; otherwise the line is rejected whole and counted.
* a **log line**: starts with `#`. Shown to people, never parsed.
* anything else is **noise** (the ESP8266 prints garbage at boot, for example): counted, shown
  as a log, never parsed.

`decode_up` is the laptop's parser for C's lines; `decode_down` is C's parser for the
laptop's lines (used by the simulated C and by the firmware test vectors).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from iot_backend.protocol import DecodeError, MessageType, decode
from iot_backend.transports.wire import (
    AckDown,
    DownMessage,
    FrameUp,
    GoneUp,
    HeartbeatDown,
    HelloUp,
    ReplayEndUp,
    ReplayStartUp,
    ResendDown,
    StatusUp,
    TransmitDown,
    UpMessage,
)

SERIAL_VERSION = 1
DEFAULT_BAUD = 115_200
# Longest line each side may send, newline included. Downlink lines must fit the ESP8266's
# serial receive buffer (256 bytes by default in the Arduino core, behind a 128-byte hardware
# FIFO) with room to spare, so they are kept under the FIFO size. Uplink lines carry a whole
# radio frame in hex (up to 250 bytes = 500 hex digits); the laptop has no such limit.
MAX_DOWN_LINE = 128
MAX_UP_LINE = 640
MAX_TX_PAYLOAD = 40  # bytes, so a `tx` line stays under MAX_DOWN_LINE
LOG_PREFIX = b"#"
U32_MAX = 0xFFFF_FFFF

# Frame types C hands up: what it consumes for the controller, and the DATA it accepts.
UP_FRAME_TYPES = frozenset(
    {
        MessageType.DATA,
        MessageType.TOPOLOGY_REPORT,
        MessageType.ROUTE_INSTALL_ACK,
        MessageType.LOCAL_FAILOVER_REPORT,
        MessageType.TELEMETRY,
    }
)

# Stable reasons a line is refused (they appear in tests/serial-examples/).
REJECT_REASONS = (
    "too_long",  # longer than the limit for its direction
    "truncated",  # the port closed in the middle of the line
    "no_checksum",  # starts like data but has no *XXXX ending
    "bad_checksum",  # the CRC does not match: bytes were lost or changed
    "not_ascii",  # bytes outside printable ASCII in a data line
    "bad_json",  # the text is not one valid JSON object
    "duplicate_key",
    "bad_version",
    "unknown_type",
    "missing_field",
    "unknown_field",
    "bad_value",  # a field has the wrong type or is out of range
    "bad_frame",  # the hex frame was refused by the protocol codec
    "unexpected_frame",  # a valid frame of a type C should not hand up
)


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: polynomial 0x1021, initial value 0xFFFF, no reflection, no final XOR.

    Check value: crc16(b"123456789") == 0x29B1.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


class LineError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        assert reason in REJECT_REASONS, reason
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ParsedLine:
    """What one received line turned out to be."""

    kind: str  # "message", "log", "noise" or "rejected"
    message: Any = None  # the decoded wire message, for kind "message"
    reason: str = ""  # for kind "rejected"
    detail: str = ""
    text: str = ""  # the line as text (for logs, noise and diagnostics)


# ---------------------------------------------------------------- field validation


def _u32(value: Any, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= U32_MAX:
        raise LineError("bad_value", f"{value!r} is not an integer {low}..{U32_MAX}")
    return value


def _bit(value: Any) -> bool:
    if isinstance(value, bool) or value not in (0, 1):
        raise LineError("bad_value", f"{value!r} is not 0 or 1")
    return value == 1


def _node(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 254:
        raise LineError("bad_value", f"{value!r} is not a node id 1..254")
    return value


def _u8(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
        raise LineError("bad_value", f"{value!r} is not 1..255")
    return value


_HEX = re.compile(r"^(?:[0-9a-fA-F]{2})*$")
_FIRMWARE = re.compile(r"^[A-Za-z0-9._-]{0,16}$")


def _hex(value: Any, max_bytes: int) -> bytes:
    if not isinstance(value, str) or not _HEX.match(value) or len(value) > 2 * max_bytes:
        raise LineError("bad_value", f"not hex of at most {max_bytes} bytes")
    return bytes.fromhex(value)


MAX_GONE_RANGE = 64


def _kinds(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[RMS?]{{1,{MAX_GONE_RANGE}}}", value):
        raise LineError("bad_value", f"kinds must be 1..{MAX_GONE_RANGE} of R M S ?")
    return value


def _firmware(value: Any) -> str:
    if not isinstance(value, str) or not _FIRMWARE.match(value):
        raise LineError("bad_value", "fw must be up to 16 of A-Z a-z 0-9 . _ -")
    return value


# (wire key, attribute, validator). Order is the order the laptop writes them in.
Spec = list[tuple[str, str, Callable[[Any], Any]]]

_UP: dict[str, tuple[type, Spec]] = {
    "fr": (
        FrameUp,
        [
            ("b", "boot", lambda v: _u32(v, 1)),
            ("n", "n", lambda v: _u32(v, 1)),
            ("age", "age_ms", _u32),
            ("rp", "replayed", _bit),
            ("via", "via", _node),
            ("dup", "duplicate", _bit),  # DATA only (checked below)
            ("hex", "frame", lambda v: _hex(v, 250)),
        ],
    ),
    "st": (
        StatusUp,
        [
            ("b", "boot", lambda v: _u32(v, 1)),
            ("last", "last", _u32),
            ("low", "low", lambda v: _u32(v, 1)),
            ("reach", "controller_reachable", _bit),
            ("held", "held", _u32),
            ("unacked", "unacked", _u32),
            ("cap", "capacity", _u32),
            ("win", "window", _u32),
            ("drop", "dropped_overflow", _u32),
            ("rdrop", "dropped_readings", _u32),
            ("coal", "coalesced", _u32),
            ("rep", "replayed", _u32),
            ("rtx", "retransmitted", _u32),
            ("out", "outages", _u32),
        ],
    ),
    "rps": (
        ReplayStartUp,
        [
            ("b", "boot", lambda v: _u32(v, 1)),
            ("ent", "entries", _u32),
            ("held", "held", _u32),
            ("drop", "dropped_overflow", _u32),
            ("rdrop", "dropped_readings", _u32),
            ("coal", "coalesced", _u32),
            ("out_ms", "outage_ms", _u32),
        ],
    ),
    "rpe": (
        ReplayEndUp,
        [
            ("b", "boot", lambda v: _u32(v, 1)),
            ("rep", "replayed", _u32),
            ("drop", "dropped_overflow", _u32),
            ("rdrop", "dropped_readings", _u32),
            ("coal", "coalesced", _u32),
        ],
    ),
    "gone": (
        GoneUp,
        [
            ("b", "boot", lambda v: _u32(v, 1)),
            ("from", "first", lambda v: _u32(v, 1)),
            ("to", "last", lambda v: _u32(v, 1)),
            ("kinds", "kinds", _kinds),
        ],
    ),
    "hello": (
        HelloUp,
        [
            ("b", "boot", lambda v: _u32(v, 1)),
            ("node", "node", _node),
            ("cap", "capacity", _u32),
            ("win", "window", _u32),
            ("fw", "firmware", _firmware),
        ],
    ),
}

_DOWN: dict[str, tuple[type, Spec]] = {
    "hb": (HeartbeatDown, [("b", "boot", _u32), ("ack", "ack", _u32)]),
    "ack": (AckDown, [("b", "boot", lambda v: _u32(v, 1)), ("ack", "ack", _u32)]),
    "rs": (
        ResendDown,
        [("b", "boot", lambda v: _u32(v, 1)), ("from", "from_n", lambda v: _u32(v, 1))],
    ),
    "tx": (
        TransmitDown,
        [
            ("dst", "dst", _node),
            ("mt", "msg_type", _u8),
            ("p", "payload", lambda v: _hex(v, MAX_TX_PAYLOAD)),
        ],
    ),
}

_TYPE_OF: dict[type, tuple[str, Spec]] = {
    cls: (name, spec) for table in (_UP, _DOWN) for name, (cls, spec) in table.items()
}


# ---------------------------------------------------------------- encoding


def _wire_value(key: str, value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, bytes):
        return value.hex()
    return value


def encode(message: UpMessage | DownMessage) -> bytes:
    """One complete data line, newline included."""
    name, spec = _TYPE_OF[type(message)]
    body: dict[str, Any] = {"v": SERIAL_VERSION, "t": name}
    for key, attr, _ in spec:
        if isinstance(message, FrameUp) and key == "dup" and not _is_data(message.frame):
            continue  # dup is carried only for DATA frames
        body[key] = _wire_value(key, getattr(message, attr))
    text = json.dumps(body, separators=(",", ":")).encode("ascii")
    line = text + b"*" + f"{crc16(text):04X}".encode("ascii") + b"\n"
    limit = MAX_UP_LINE if name in _UP else MAX_DOWN_LINE
    if len(line) > limit:
        raise ValueError(f"{name} line is {len(line)} bytes, limit {limit}")
    return line


def log_line(text: str) -> bytes:
    """A diagnostic line (C's side): `#` then printable text."""
    clean = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)
    return b"# " + clean.encode("ascii")[: MAX_UP_LINE - 3] + b"\n"


def _is_data(frame: bytes) -> bool:
    return len(frame) >= 2 and frame[1] == MessageType.DATA


# ---------------------------------------------------------------- decoding


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise LineError("duplicate_key", key)
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise LineError("bad_value", f"{name} is not allowed")


def _reject_float(text: str) -> Any:
    raise LineError("bad_value", f"{text} is not an integer")


def _object(line: bytes) -> dict[str, Any]:
    if len(line) < 6 or line[-5:-4] != b"*":
        raise LineError("no_checksum")
    text, check = line[:-5], line[-4:]
    if any(b < 0x20 or b > 0x7E for b in line):
        raise LineError("not_ascii")
    if not re.fullmatch(rb"[0-9A-Fa-f]{4}", check):
        raise LineError("no_checksum", "the checksum is not 4 hex digits")
    expected = int(check, 16)
    if crc16(text) != expected:
        raise LineError("bad_checksum", f"got {check.decode()}, computed {crc16(text):04X}")
    try:
        value = json.loads(
            text.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except LineError:
        raise
    except ValueError as error:
        raise LineError("bad_json", str(error)) from error
    if not isinstance(value, dict):  # unreachable for text starting with "{", kept as a guard
        raise LineError("bad_json", "not an object")
    return value


def _build(obj: dict[str, Any], table: dict[str, tuple[type, Spec]]) -> Any:
    if "v" not in obj or "t" not in obj:
        raise LineError("missing_field", "v and t are required")
    if isinstance(obj["v"], bool) or obj["v"] != SERIAL_VERSION:
        raise LineError("bad_version", repr(obj["v"]))
    kind = obj["t"]
    if not isinstance(kind, str) or kind not in table:
        raise LineError("unknown_type", repr(kind))
    cls, spec = table[kind]
    keys = {key for key, _, _ in spec}
    unknown = set(obj) - keys - {"v", "t"}
    if unknown:
        raise LineError("unknown_field", ",".join(sorted(unknown)))
    values: dict[str, Any] = {}
    for key, attr, check in spec:
        if key not in obj:
            if cls is FrameUp and key == "dup":
                continue  # decided below, once the frame type is known
            raise LineError("missing_field", key)
        try:
            values[attr] = check(obj[key])
        except LineError as error:
            raise LineError(error.reason, f"{key}: {error.detail}") from None
    if cls is FrameUp:
        try:
            frame = decode(values["frame"])
        except DecodeError as error:
            raise LineError("bad_frame", error.reason.value) from None
        if frame.msg_type not in UP_FRAME_TYPES:
            raise LineError("unexpected_frame", frame.msg_type.name)
        is_data = frame.msg_type is MessageType.DATA
        if is_data and "duplicate" not in values:
            raise LineError("missing_field", "dup (required for DATA)")
        if not is_data and "duplicate" in values:
            raise LineError("unknown_field", "dup (only for DATA)")
        values.setdefault("duplicate", False)
    if cls is StatusUp and values["low"] > values["last"] + 1:
        raise LineError("bad_value", "low is above last + 1")
    if cls is GoneUp and (
        values["first"] > values["last"]
        or len(values["kinds"]) != values["last"] - values["first"] + 1
    ):
        raise LineError("bad_value", "kinds must have one letter per number from..to")
    return cls(**values)


def _parse(line: bytes, table: dict[str, tuple[type, Spec]], limit: int) -> ParsedLine:
    line = line.rstrip(b"\r")
    text = line.decode("ascii", errors="replace")
    if len(line) + 1 > limit:
        return ParsedLine("rejected", reason="too_long", text=text[:80])
    if line.startswith(LOG_PREFIX):
        return ParsedLine("log", text=text[1:].strip())
    if not line.startswith(b"{"):
        return ParsedLine("noise", text=text)
    try:
        return ParsedLine("message", message=_build(_object(line), table), text=text)
    except LineError as error:
        return ParsedLine("rejected", reason=error.reason, detail=error.detail, text=text)


def decode_up(line: bytes) -> ParsedLine:
    """The laptop's view of one line from C (without its newline)."""
    return _parse(line, _UP, MAX_UP_LINE)


def decode_down(line: bytes) -> ParsedLine:
    """C's view of one line from the laptop (without its newline)."""
    return _parse(line, _DOWN, MAX_DOWN_LINE)


# ---------------------------------------------------------------- splitting a byte stream


@dataclass
class LineSplitter:
    """Cut a byte stream into lines, with a bounded buffer.

    A line longer than `limit` is discarded up to its newline and reported once as `too_long`;
    the splitter then resynchronises on the next newline. Memory never exceeds `limit` bytes.
    """

    limit: int
    _buffer: bytearray = field(default_factory=bytearray)
    _discarding: bool = False

    def feed(self, data: bytes) -> list[bytes | None]:
        """Complete lines (without the newline); None marks a discarded over-long line."""
        out: list[bytes | None] = []
        for chunk in data.split(b"\n")[:-1] if b"\n" in data else []:
            out.extend(self._complete(chunk))
        tail = data.rsplit(b"\n", 1)[-1] if b"\n" in data else data
        if not self._discarding:
            self._buffer.extend(tail)
            if len(self._buffer) >= self.limit:
                self._buffer.clear()
                self._discarding = True
                out.append(None)
        return out

    def _complete(self, chunk: bytes) -> list[bytes | None]:
        if self._discarding:
            self._discarding = False
            self._buffer.clear()
            return []
        self._buffer.extend(chunk)
        line = bytes(self._buffer)
        self._buffer.clear()
        if len(line) >= self.limit:
            return [None]
        return [line]

    def flush(self) -> bytes | None:
        """The port closed: return (and forget) a partial line, if there was one."""
        partial = bytes(self._buffer)
        self._buffer.clear()
        self._discarding = False
        return partial or None
