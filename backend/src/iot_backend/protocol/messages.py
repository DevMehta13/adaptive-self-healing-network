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


@dataclass(frozen=True)
class TopologyReportPayload:
    """A node's current neighbor observations."""

    entries: tuple[ReportEntry, ...] = ()

    msg_type = MessageType.TOPOLOGY_REPORT


Payload = DataPayload | DeliveryAckPayload | HelloPayload | TopologyReportPayload


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
