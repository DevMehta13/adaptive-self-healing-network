"""Structured event trace: the simulator's observable record of what happened."""

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from iot_backend.protocol import NODE_NAMES, PacketIdentity

# Event names
NODE_BOOTED = "node_booted"
NODE_STOPPED = "node_stopped"
NEIGHBOR_CHANGED = "neighbor_changed"  # a neighbor-table state changed (HELLO / timeout)
NEIGHBOR_RESTARTED = "neighbor_restarted"  # a neighbor's session changed
FRAME_SENT = "frame_sent"  # a node transmitted a frame it created
FRAME_FORWARDED = "frame_forwarded"  # a relay transmitted a frame it received for someone else
FRAME_DROPPED_BY_LINK = "frame_dropped_by_link"
FRAME_RECEIVED = "frame_received"  # intact bytes reached a node's radio
FRAME_REJECTED = "frame_rejected"  # a node refused a received frame
FRAME_DROPPED = "frame_dropped"  # a node discarded a frame (no route, TTL, queue, stopped)
READING_DELIVERED = "reading_delivered"  # first receipt at the destination
DUPLICATE_RECEIVED = "duplicate_received"
READING_LOST = "reading_lost"  # the source gave up after its last attempt
ACK_RECEIVED = "ack_received"
ACK_IGNORED = "ack_ignored"
ACK_TIMEOUT = "ack_timeout"  # one attempt went unacknowledged
ROUTE_UPDATE_SENT = "route_update_sent"  # the gateway node originated a ROUTE_UPDATE
ROUTE_INSTALLED = "route_installed"  # a node applied a ROUTE_UPDATE to its table
ROUTE_UPDATE_IGNORED = "route_update_ignored"  # duplicate, stale, or refused by the node
SEND_FAILED = "send_failed"  # the link-layer callback said the next hop's radio did not ack
LOCAL_FAILOVER = "local_failover"  # a node switched to its installed backup (T1 or T2)
LOCAL_NO_ROUTE = "local_no_route"  # no usable backup: the node now has no route
FRAME_REROUTED = "frame_rerouted"  # a queued frame was sent to a different next hop
GATEWAY_DETACHED = "gateway_detached"  # the controller can no longer see or command the network
GATEWAY_ATTACHED = "gateway_attached"


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


def observed_paths(events: Iterable[TraceEvent], identity: PacketIdentity) -> list[list[str]]:
    """Reconstruct where a packet actually went, from received-frame evidence only.

    Returns one path per transmission attempt, e.g. [["A", "B", "C"]]. A path ends where the
    frame was last received: a frame that was lost on a link, or reached a stopped node, shows
    as a path that stops early. This reads what happened; it never consults routing tables.
    """
    paths: list[list[str]] = []
    for event in events:
        if event.identity != identity:
            continue
        if event.event == FRAME_SENT:
            paths.append([event.node])
        elif event.event == FRAME_RECEIVED:
            sender = event.info["sender"]
            for path in reversed(paths):
                if path[-1] == sender:
                    path.append(event.node)
                    break
            else:
                paths.append([sender, event.node])
    return paths


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
