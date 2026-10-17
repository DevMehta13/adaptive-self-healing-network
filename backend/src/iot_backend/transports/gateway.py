"""The gateway interface: everything the controller may know about, and say to, the network.

Both modes implement it. In simulation a `SimulatedGateway` is attached to virtual node C; in
hardware a serial gateway will turn lines from C's USB port into the same messages. The
controller is written against this interface only, so it cannot read simulator state and works
unchanged with real boards.

The laptop-to-C traffic is a small, explicit set of **typed messages**, so the serial encoding
(newline-delimited JSON, a later step) can map to them one to one without redesign. They never
travel on the radio.

| Direction | Message (wire name) | What it is |
| --- | --- | --- |
| down | `Heartbeat` (`heartbeat`) | "the controller is alive", every heartbeat interval |
| down | `RouteUpdateCommand` (`route_update`) | an instruction C turns into a ROUTE_UPDATE frame |
| up | `TopologyReportEvent` (`topology_report`) | a node's neighbor observations |
| up | `RouteInstallAckEvent` (`route_ack`) | a node's answer to a route update |
| up | `LocalFailoverEvent` (`local_failover`) | a node switched to its backup |
| up | `ReadingEvent` (`reading`) | a DATA reading accepted at C, with C's evidence |
| up | `TelemetryEvent` (`telemetry`) | a node's own counters |
| up | `GatewayStatusEvent` (`status`) | C's answer to a heartbeat: buffer and outage counters |
| up | `ReplayStartEvent` (`replay_start`) | C is about to replay what it held |
| up | `ReplayEndEvent` (`replay_end`) | the replay is complete, with counts |

Every up message that describes something that *happened* carries `age_ms` and `replayed`.
`age_ms` is how long ago C received it, **on C's own clock** (C has no wall clock). The laptop
turns that into an approximate original time on *its* clock (`received_ms = now - age_ms`); no
clock is ever compared with another. `replayed` is true for entries C held while the controller
was unreachable.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class NeighborObservation:
    """What a reporting node says about one neighbor."""

    neighbor: int
    state: str  # "one_way" | "two_way" | "lost"
    session: int  # the neighbor's session in its latest HELLO
    last_heard_ms_ago: int  # on the reporter's clock when it built the report (saturating)
    # Round trip to this neighbor, measured by the reporter on its own clock; None = unmeasured.
    rtt_ms: int | None = None
    # Of the reporter's last `probes_total` completed probes to this neighbor, `probes_ok` were
    # answered (Phase 9); None = too few completed probes to say.
    probes_ok: int | None = None
    probes_total: int | None = None


@dataclass(frozen=True)
class TopologyReportEvent:
    """A TOPOLOGY_REPORT delivered to the gateway."""

    received_ms: int  # laptop clock when it arrived at C (estimated from age_ms if replayed)
    origin: int  # node that created the report
    session: int  # that node's session
    seq: int
    neighbors: tuple[NeighborObservation, ...]
    age_ms: int = 0  # C's own clock: how long C held it before handing it up
    replayed: bool = False


@dataclass(frozen=True)
class RouteInstallAckEvent:
    """A ROUTE_INSTALL_ACK delivered to the gateway.

    For status "stale", `epoch` and `version` are the values the node *holds*, not the update's.
    """

    received_ms: int
    origin: int  # the node that installed (or refused) the update
    session: int
    seq: int
    epoch: int
    version: int
    destination: int
    status: str  # "installed" | "stale" | "rejected"
    age_ms: int = 0
    replayed: bool = False


@dataclass(frozen=True)
class RouteUpdateCommand:
    """Ask node `node` to use `next_hop` for frames addressed to `destination`.

    `next_hop` None means "there is no route" (an explicit statement, distinct from never having
    been told). `backup_next_hop` is the node's installed fallback (Phase 4b).
    """

    node: int
    epoch: int
    version: int
    destination: int
    next_hop: int | None
    backup_next_hop: int | None = None


@dataclass(frozen=True)
class LocalFailoverEvent:
    """A LOCAL_FAILOVER_REPORT delivered to the gateway (Phase 4b).

    `to_next_hop` None means the node found no usable backup and now has no route.
    `reason` is "neighbor_lost" (T1: primary no longer two-way) or "send_failures" (T2).
    """

    received_ms: int
    origin: int
    session: int
    seq: int
    epoch: int
    version: int
    destination: int
    from_next_hop: int
    to_next_hop: int | None
    reason: str
    age_ms: int = 0
    replayed: bool = False


@dataclass(frozen=True)
class ReadingEvent:
    """A DATA reading that arrived at C, with the evidence C itself has about its journey.

    `via` is the link-layer sender (the neighbor that handed C the frame) and `ttl` is the
    frame's remaining TTL. `hops` is derived from the TTL assuming the configured data TTL, so
    it is the number of transmissions. Nothing else about the path is known to C.
    """

    received_ms: int
    origin: int
    session: int
    seq: int
    value: int  # hundredths of a unit, as on the wire
    via: int
    ttl: int
    hops: int
    duplicate: bool  # a repeat of a reading already counted (its ACK is sent again)
    age_ms: int = 0
    replayed: bool = False


@dataclass(frozen=True)
class TelemetryEvent:
    """A node's own counters, delivered by flooding. Only the node knows its true values."""

    received_ms: int
    origin: int
    session: int
    seq: int
    created: int
    acknowledged: int
    lost: int
    no_route: int
    delivered: int
    forwarded: int
    dropped: int
    failovers: int
    send_failures: int
    age_ms: int = 0
    replayed: bool = False


@dataclass(frozen=True)
class Heartbeat:
    """Laptop to C: the controller is alive. Sent every heartbeat interval; never on the radio."""

    sent_ms: int  # the laptop's clock, for the laptop's own logs; C ignores it


@dataclass(frozen=True)
class GatewayStatusEvent:
    """C's answer to a heartbeat, so the laptop can see that C and the link are alive.

    The counters are cumulative since C booted.
    """

    received_ms: int
    controller_reachable: bool
    held: int  # entries in the buffer waiting for (re)delivery right now
    capacity: int
    dropped_overflow: int  # held entries lost because the buffer was full
    coalesced: int  # entries superseded by a newer one from the same origin
    replayed: int  # entries handed up in replays
    outages: int  # times C declared the controller unreachable


@dataclass(frozen=True)
class ReplayStartEvent:
    """C announces a replay: what it buffered, replayed and lost since the outage began."""

    received_ms: int
    entries: int  # how many entries follow (between this marker and the end marker)
    held: int  # of those, made while the controller was unreachable
    dropped_overflow: int  # lost to overflow during this outage
    coalesced: int  # superseded during this outage
    outage_ms: int  # C's own measure of how long the controller was unreachable


@dataclass(frozen=True)
class ReplayEndEvent:
    """The replay is complete."""

    received_ms: int
    replayed: int
    dropped_overflow: int
    coalesced: int


@dataclass(frozen=True)
class UplinkLossEvent:
    """The laptop's own finding: numbered messages from C that will never arrive (Phase 6-prep).

    Produced by the laptop side of the gateway, never by C directly. `reason` is "gone_at_c"
    (C said it no longer holds them, and what each was) or "c_restarted" (C rebooted: its buffer
    is RAM-only, so whatever it held is gone; `count` is then a lower bound, the numbers the
    laptop knew about, and their kinds are unknown).
    """

    received_ms: int
    reason: str
    boot: int  # the boot of C the numbers belonged to
    first: int
    last: int
    count: int  # how many of first..last were never received
    readings: int = 0  # of those, readings (DATA)
    unknown: int = 0  # of those, of a kind C could not say


GatewayEvent = (
    TopologyReportEvent
    | RouteInstallAckEvent
    | LocalFailoverEvent
    | ReadingEvent
    | TelemetryEvent
    | GatewayStatusEvent
    | ReplayStartEvent
    | ReplayEndEvent
    | UplinkLossEvent
)
GatewayHandler = Callable[[GatewayEvent], None]

# The complete message set, by name. Each maps to exactly one serial line type
# (docs/SERIAL_PROTOCOL.md): heartbeat -> `hb`; route_update -> `tx`; the five network events
# -> `fr` (the radio frame itself, decoded on the laptop); status -> `st`; replay_start ->
# `rps`; replay_end -> `rpe`. uplink_loss is made by the laptop from `gone`, `st` or a change
# of C's boot, and has no line of its own.
DOWN_MESSAGES: dict[str, type] = {"heartbeat": Heartbeat, "route_update": RouteUpdateCommand}
UP_MESSAGES: dict[str, type] = {
    "topology_report": TopologyReportEvent,
    "route_ack": RouteInstallAckEvent,
    "local_failover": LocalFailoverEvent,
    "reading": ReadingEvent,
    "telemetry": TelemetryEvent,
    "status": GatewayStatusEvent,
    "replay_start": ReplayStartEvent,
    "replay_end": ReplayEndEvent,
    "uplink_loss": UplinkLossEvent,
}


class Gateway(Protocol):
    """Events from the network in, commands out, and the laptop's clock and timers."""

    def subscribe(self, handler: GatewayHandler) -> None:
        """Call `handler` for every message that arrives from C."""

    def now_ms(self) -> int:
        """The laptop clock in milliseconds (virtual time in simulation)."""

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        """Run `callback` once after `delay_ms` on that clock."""

    def send_route_update(self, command: RouteUpdateCommand) -> None:
        """Have C originate a ROUTE_UPDATE management frame for `command.node`."""

    def send_heartbeat(self, heartbeat: Heartbeat) -> None:
        """Tell C the controller is alive."""
