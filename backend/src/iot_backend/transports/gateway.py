"""The gateway interface: everything the controller may know about the network.

Both modes implement it. In simulation a `SimulatedGateway` is attached to virtual node C; in
hardware a serial gateway (Phase 6/7) will turn lines from C's USB port into the same events.
The controller is written against this interface only, so it cannot read simulator state and
works unchanged with real boards.

Phase 3 carries one kind of node event: a topology report that arrived at C. Readings, route
acknowledgements and commands *to* nodes are added in later phases.
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


@dataclass(frozen=True)
class TopologyReportEvent:
    """A TOPOLOGY_REPORT delivered to the gateway."""

    received_ms: int  # laptop (gateway-side) clock when it arrived
    origin: int  # node that created the report
    session: int  # that node's session
    seq: int
    neighbors: tuple[NeighborObservation, ...]


GatewayEvent = TopologyReportEvent
GatewayHandler = Callable[[GatewayEvent], None]


class Gateway(Protocol):
    """Events from the network in, and the laptop's clock and timers."""

    def subscribe(self, handler: GatewayHandler) -> None:
        """Call `handler` for every node event that arrives at C."""

    def now_ms(self) -> int:
        """The laptop clock in milliseconds (virtual time in simulation)."""

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        """Run `callback` once after `delay_ms` on that clock."""
