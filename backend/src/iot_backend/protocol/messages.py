"""Message classes for the node protocol."""

from dataclasses import dataclass
from typing import NamedTuple

from iot_backend.protocol.constants import MessageType


class PacketIdentity(NamedTuple):
    """Uniquely names a frame: who created it, in which boot, and which number."""

    src: int
    session: int
    seq: int


@dataclass(frozen=True)
class DataPayload:
    """A sensor reading as a signed fixed-point integer (hundredths, so 2345 = 23.45)."""

    value: int

    msg_type = MessageType.DATA


@dataclass(frozen=True)
class DeliveryAckPayload:
    """Names the reading being acknowledged by its origin, session and sequence."""

    origin: int
    session: int
    seq: int

    msg_type = MessageType.DELIVERY_ACK

    @property
    def acked(self) -> PacketIdentity:
        return PacketIdentity(self.origin, self.session, self.seq)


@dataclass(frozen=True)
class HelloNeighbor:
    """A neighbor the HELLO's sender currently hears, with the session it heard it in."""

    node: int
    session: int


@dataclass(frozen=True)
class HelloPayload:
    """Local keep-alive listing the neighbors the sender hears (lets a receiver confirm 2-way)."""

    neighbors: tuple[HelloNeighbor, ...] = ()

    msg_type = MessageType.HELLO


@dataclass(frozen=True)
class ReportEntry:
    """One neighbor observation: its id, state (NeighborCode), session, ms since last heard."""

    neighbor: int
    state: int
    session: int
    age_ms: int
    # Round-trip time to this neighbor measured by the reporter on its own clock (median of a
    # small window), or None if there is no fresh measurement. None is never a zero cost.
    rtt_ms: int | None = None


@dataclass(frozen=True)
class TopologyReportPayload:
    """A node's current neighbor observations."""

    entries: tuple[ReportEntry, ...] = ()

    msg_type = MessageType.TOPOLOGY_REPORT


@dataclass(frozen=True)
class ProbePayload:
    """One-hop round-trip probe. Empty: the frame header's session and seq identify it."""

    msg_type = MessageType.PROBE


@dataclass(frozen=True)
class ProbeReplyPayload:
    """Echoes the probe's session and seq so the prober can match it on its own clock."""

    probe_session: int
    probe_seq: int

    msg_type = MessageType.PROBE_REPLY


@dataclass(frozen=True)
class RouteUpdatePayload:
    """Controller instruction (header dst = the node that must install it).

    `next_hop` None means "no route to the destination" (explicitly told). `backup_next_hop` is
    carried now so the format does not change in Phase 4b; Phase 4a always sends None.
    """

    epoch: int
    version: int
    destination: int
    next_hop: int | None
    backup_next_hop: int | None = None

    msg_type = MessageType.ROUTE_UPDATE


@dataclass(frozen=True)
class RouteInstallAckPayload:
    """A node's answer to a ROUTE_UPDATE. For status STALE, epoch/version are the held values."""

    epoch: int
    version: int
    destination: int
    status: int  # RouteAckStatus

    msg_type = MessageType.ROUTE_INSTALL_ACK


@dataclass(frozen=True)
class LocalFailoverReportPayload:
    """A node reports that it switched from its primary to its backup (defined; sent in 4b)."""

    epoch: int
    version: int
    destination: int
    from_next_hop: int
    to_next_hop: int
    reason: int  # FailoverReason

    msg_type = MessageType.LOCAL_FAILOVER_REPORT


Payload = (
    DataPayload
    | DeliveryAckPayload
    | HelloPayload
    | TopologyReportPayload
    | ProbePayload
    | ProbeReplyPayload
    | RouteUpdatePayload
    | RouteInstallAckPayload
    | LocalFailoverReportPayload
)


@dataclass(frozen=True)
class Frame:
    """One protocol frame. `src` is the node that created it, not the previous hop."""

    src: int
    dst: int
    session: int
    seq: int
    ttl: int
    payload: Payload

    @property
    def msg_type(self) -> MessageType:
        return self.payload.msg_type

    @property
    def identity(self) -> PacketIdentity:
        return PacketIdentity(self.src, self.session, self.seq)
