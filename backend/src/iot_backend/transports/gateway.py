"""The gateway interface: everything the controller may know about, and say to, the network.

Both modes implement it. In simulation a `SimulatedGateway` is attached to virtual node C; in
hardware a serial gateway (Phase 6/7) will turn lines from C's USB port into the same events.
The controller is written against this interface only, so it cannot read simulator state and
works unchanged with real boards.

Events in (Phase 4a): a topology report that arrived at C, and a route-install acknowledgement
that arrived at C. Commands out: a route update, which C turns into a management frame that it
originates on the controller's behalf. Commands never travel along the data route.
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


@dataclass(frozen=True)
class TopologyReportEvent:
    """A TOPOLOGY_REPORT delivered to the gateway."""

    received_ms: int  # laptop (gateway-side) clock when it arrived
    origin: int  # node that created the report
    session: int  # that node's session
    seq: int
    neighbors: tuple[NeighborObservation, ...]


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


@dataclass(frozen=True)
class RouteUpdateCommand:
    """Ask node `node` to use `next_hop` for frames addressed to `destination`.

    `next_hop` None means "there is no route" (an explicit statement, distinct from never having
    been told). `backup_next_hop` exists for Phase 4b; Phase 4a always passes None.
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


GatewayEvent = TopologyReportEvent | RouteInstallAckEvent | LocalFailoverEvent
GatewayHandler = Callable[[GatewayEvent], None]


class Gateway(Protocol):
    """Events from the network in, commands out, and the laptop's clock and timers."""

    def subscribe(self, handler: GatewayHandler) -> None:
        """Call `handler` for every node event that arrives at C."""

    def now_ms(self) -> int:
        """The laptop clock in milliseconds (virtual time in simulation)."""

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        """Run `callback` once after `delay_ms` on that clock."""

    def send_route_update(self, command: RouteUpdateCommand) -> None:
        """Have C originate a ROUTE_UPDATE management frame for `command.node`."""
