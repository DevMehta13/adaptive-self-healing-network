"""Structured event trace: the simulator's observable record of what happened."""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iot_backend.protocol import NODE_NAMES, PacketIdentity

# Event names
NODE_BOOTED = "node_booted"
FRAME_SENT = "frame_sent"
FRAME_DROPPED_BY_LINK = "frame_dropped_by_link"
FRAME_RECEIVED = "frame_received"
FRAME_REJECTED = "frame_rejected"
FRAME_DROPPED = "frame_dropped"
NODE_STOPPED = "node_stopped"
READING_DELIVERED = "reading_delivered"
ACK_RECEIVED = "ack_received"
ACK_IGNORED = "ack_ignored"
ACK_TIMEOUT = "ack_timeout"


@dataclass(frozen=True)
class TraceEvent:
    time_ms: int
    node: str
    event: str
    identity: PacketIdentity | None = None
    info: Mapping[str, Any] = field(default_factory=dict)


class Trace:
    """Bounded list of events. If the bound is hit, the oldest are dropped and counted."""

    def __init__(self, max_events: int = 100_000) -> None:
        self._events: deque[TraceEvent] = deque(maxlen=max_events)
        self.dropped = 0

    def record(
        self,
        time_ms: int,
        node: str,
        event: str,
        identity: PacketIdentity | None = None,
        **info: Any,
    ) -> None:
        if len(self._events) == self._events.maxlen:
            self.dropped += 1
        self._events.append(TraceEvent(time_ms, node, event, identity, info))

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    def of_type(self, event: str) -> list[TraceEvent]:
        return [e for e in self._events if e.event == event]


def format_identity(identity: PacketIdentity | None) -> str:
    if identity is None:
        return "-"
    name = NODE_NAMES.get(identity.src, str(identity.src))
    return f"{name}:{identity.session:08x}#{identity.seq}"


def format_event(event: TraceEvent) -> str:
    info = " ".join(f"{key}={value}" for key, value in event.info.items())
    parts = [f"{event.time_ms / 1000:8.3f}s", f"{event.node:<2}", f"{event.event:<22}"]
    parts.append(f"{format_identity(event.identity):<18}")
    return "  ".join(parts) + " " + info
