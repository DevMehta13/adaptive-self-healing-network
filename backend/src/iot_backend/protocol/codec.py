"""Binary encoding and validated decoding of protocol frames."""

import struct

from iot_backend.protocol.constants import (
    AGE_MAX_MS,
    DATA_PAYLOAD_SIZE,
    DELIVERY_ACK_PAYLOAD_SIZE,
    HEADER_SIZE,
    HEADER_STRUCT,
    HELLO_ENTRY_SIZE,
    I32_MAX,
    I32_MIN,
    IMPLEMENTED_TYPES,
    INVALID_NODE_IDS,
    LOCAL_FAILOVER_REPORT_PAYLOAD_SIZE,
    MAX_FRAME_SIZE,
    MAX_HELLO_NEIGHBORS,
    MAX_REPORT_ENTRIES,
    NO_NODE,
    PROBE_PAYLOAD_SIZE,
    PROBE_REPLY_PAYLOAD_SIZE,
    PROTOCOL_VERSION,
    REPORT_ENTRY_SIZE,
    ROUTE_INSTALL_ACK_PAYLOAD_SIZE,
    ROUTE_UPDATE_PAYLOAD_SIZE,
    RTT_ENTRY_SIZE,
    RTT_MAX_MS,
    U8_MAX,
    U32_MAX,
    FailoverReason,
    MessageType,
    NeighborCode,
    RejectReason,
    RouteAckStatus,
)
from iot_backend.protocol.messages import (
    DataPayload,
    DeliveryAckPayload,
    Frame,
    HelloNeighbor,
    HelloPayload,
    LocalFailoverReportPayload,
    Payload,
    ProbePayload,
    ProbeReplyPayload,
    ReportEntry,
    RouteInstallAckPayload,
    RouteUpdatePayload,
    TopologyReportPayload,
)

_DATA = struct.Struct("<i")
_ACK = struct.Struct("<BII")
_HELLO_ENTRY = struct.Struct("<BI")
_REPORT_ENTRY = struct.Struct("<BBIH")
_RTT_ENTRY = struct.Struct("<BH")
_PROBE_REPLY = struct.Struct("<II")
_ROUTE_UPDATE = struct.Struct("<IIBBB")
_ROUTE_ACK = struct.Struct("<IIBB")
_FAILOVER = struct.Struct("<IIBBBB")


class DecodeError(ValueError):
    """A received frame was rejected. `reason` says why."""

    def __init__(self, reason: RejectReason, detail: str = ""):
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason


class EncodeError(ValueError):
    """A frame could not be encoded because a field is out of range."""


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise EncodeError(f"{name}={value!r} outside {low}..{high}")


def _check_node(name: str, value: int) -> None:
    _check_range(name, value, 0, U8_MAX)
    if value in INVALID_NODE_IDS:
        raise EncodeError(f"{name}={value} is not a valid node id")


def _encode_hop(name: str, value: int | None) -> int:
    """A next-hop field: a node id, or None (sent as 0)."""
    if value is None:
        return NO_NODE
    _check_node(name, value)
    return value


def encode(frame: Frame) -> bytes:
    """Encode a frame to its wire bytes, raising EncodeError for out-of-range fields."""
    _check_node("src", frame.src)
    _check_node("dst", frame.dst)
    _check_range("session", frame.session, 1, U32_MAX)
    _check_range("seq", frame.seq, 0, U32_MAX)
    _check_range("ttl", frame.ttl, 0, U8_MAX)

    payload = frame.payload
    if isinstance(payload, DataPayload):
        _check_range("value", payload.value, I32_MIN, I32_MAX)
        body = _DATA.pack(payload.value)
    elif isinstance(payload, DeliveryAckPayload):
        _check_node("origin", payload.origin)
        _check_range("acked session", payload.session, 1, U32_MAX)
        _check_range("acked seq", payload.seq, 0, U32_MAX)
        body = _ACK.pack(payload.origin, payload.session, payload.seq)
    elif isinstance(payload, HelloPayload):
        if len(payload.neighbors) > MAX_HELLO_NEIGHBORS:
            raise EncodeError(f"HELLO lists {len(payload.neighbors)} neighbors")
        parts = [bytes([len(payload.neighbors)])]
        for n in payload.neighbors:
            _check_node("hello neighbor", n.node)
            _check_range("hello neighbor session", n.session, 1, U32_MAX)
            parts.append(_HELLO_ENTRY.pack(n.node, n.session))
        body = b"".join(parts)
    elif isinstance(payload, TopologyReportPayload):
        if len(payload.entries) > MAX_REPORT_ENTRIES:
            raise EncodeError(f"report has {len(payload.entries)} entries")
        parts = [bytes([len(payload.entries)])]
        for e in payload.entries:
            _check_node("report neighbor", e.neighbor)
            if e.state not in tuple(NeighborCode):
                raise EncodeError(f"report state={e.state!r} is not a NeighborCode")
            _check_range("report session", e.session, 1, U32_MAX)
            _check_range("report age_ms", e.age_ms, 0, AGE_MAX_MS)
            parts.append(_REPORT_ENTRY.pack(e.neighbor, e.state, e.session, e.age_ms))
        measured = [e for e in payload.entries if e.rtt_ms is not None]
        if measured:  # the optional RTT trailer; absent when nothing is measured
            parts.append(bytes([len(measured)]))
            for e in measured:
                _check_range("report rtt_ms", e.rtt_ms, 0, RTT_MAX_MS)  # type: ignore[arg-type]
                parts.append(_RTT_ENTRY.pack(e.neighbor, e.rtt_ms))
        body = b"".join(parts)
    elif isinstance(payload, ProbePayload):
        body = b""
    elif isinstance(payload, ProbeReplyPayload):
        _check_range("probe session", payload.probe_session, 1, U32_MAX)
        _check_range("probe seq", payload.probe_seq, 0, U32_MAX)
        body = _PROBE_REPLY.pack(payload.probe_session, payload.probe_seq)
    elif isinstance(payload, RouteUpdatePayload):
        _check_range("epoch", payload.epoch, 1, U32_MAX)
        _check_range("version", payload.version, 1, U32_MAX)
        _check_node("destination", payload.destination)
        next_hop = _encode_hop("next_hop", payload.next_hop)
        backup = _encode_hop("backup_next_hop", payload.backup_next_hop)
        if backup != NO_NODE and (next_hop == NO_NODE or backup == next_hop):
            raise EncodeError("backup_next_hop needs a different primary next_hop")
        body = _ROUTE_UPDATE.pack(
            payload.epoch, payload.version, payload.destination, next_hop, backup
        )
    elif isinstance(payload, RouteInstallAckPayload):
        _check_range("epoch", payload.epoch, 1, U32_MAX)
        _check_range("version", payload.version, 1, U32_MAX)
        _check_node("destination", payload.destination)
        if payload.status not in tuple(RouteAckStatus):
            raise EncodeError(f"status={payload.status!r} is not a RouteAckStatus")
        body = _ROUTE_ACK.pack(payload.epoch, payload.version, payload.destination, payload.status)
    elif isinstance(payload, LocalFailoverReportPayload):
        _check_range("epoch", payload.epoch, 1, U32_MAX)
        _check_range("version", payload.version, 1, U32_MAX)
        _check_node("destination", payload.destination)
        _check_node("from_next_hop", payload.from_next_hop)
        to_hop = _encode_hop("to_next_hop", payload.to_next_hop)
        if payload.from_next_hop == to_hop:
            raise EncodeError("a failover must change the next hop")
        if payload.reason not in tuple(FailoverReason):
            raise EncodeError(f"reason={payload.reason!r} is not a FailoverReason")
        body = _FAILOVER.pack(
            payload.epoch,
            payload.version,
            payload.destination,
            payload.from_next_hop,
            to_hop,
            payload.reason,
        )
    else:
        raise EncodeError(f"unsupported payload {type(payload).__name__}")

    header = HEADER_STRUCT.pack(
        PROTOCOL_VERSION,
        frame.msg_type,
        frame.src,
        frame.dst,
        frame.session,
        frame.seq,
        frame.ttl,
        len(body),
    )
    data = header + body
    if len(data) > MAX_FRAME_SIZE:  # guards payloads that grow
        raise EncodeError(f"frame is {len(data)} bytes, limit is {MAX_FRAME_SIZE}")
    return data


def _counted_entries(body: bytes, entry_size: int, maximum: int, what: str) -> int:
    """Check `count` against the payload length and the bound; return the count."""
    if not body:
        raise DecodeError(RejectReason.BAD_PAYLOAD_LENGTH, f"{what} payload is empty")
    count = body[0]
    if count > maximum or len(body) != 1 + count * entry_size:
        raise DecodeError(
            RejectReason.BAD_PAYLOAD_LENGTH,
            f"{what} count={count} with {len(body)} payload bytes",
        )
    return count


def _decode_hello(body: bytes) -> HelloPayload:
    count = _counted_entries(body, HELLO_ENTRY_SIZE, MAX_HELLO_NEIGHBORS, "HELLO")
    neighbors = []
    for i in range(count):
        node, session = _HELLO_ENTRY.unpack_from(body, 1 + i * HELLO_ENTRY_SIZE)
        if node in INVALID_NODE_IDS:
            raise DecodeError(RejectReason.BAD_NODE_ID, f"HELLO neighbor {node}")
        if session == 0:
            raise DecodeError(RejectReason.BAD_SESSION, "HELLO neighbor session 0")
        neighbors.append(HelloNeighbor(node, session))
    return HelloPayload(tuple(neighbors))


def _decode_report(body: bytes) -> TopologyReportPayload:
    if not body:
        raise DecodeError(RejectReason.BAD_PAYLOAD_LENGTH, "TOPOLOGY_REPORT payload is empty")
    count = body[0]
    base = 1 + count * REPORT_ENTRY_SIZE
    if count > MAX_REPORT_ENTRIES or len(body) < base:
        raise DecodeError(
            RejectReason.BAD_PAYLOAD_LENGTH,
            f"TOPOLOGY_REPORT count={count} with {len(body)} payload bytes",
        )
    rtt_count = 0
    if len(body) > base:  # optional RTT trailer: count, then (neighbor, rtt_ms) pairs
        rtt_count = body[base]
        if not 1 <= rtt_count <= MAX_REPORT_ENTRIES or len(body) != (
            base + 1 + rtt_count * RTT_ENTRY_SIZE
        ):
            raise DecodeError(
                RejectReason.BAD_PAYLOAD_LENGTH,
                f"TOPOLOGY_REPORT count={count} with {len(body)} payload bytes",
            )
    rtts: dict[int, int] = {}
    for i in range(rtt_count):
        neighbor, rtt = _RTT_ENTRY.unpack_from(body, base + 1 + i * RTT_ENTRY_SIZE)
        if neighbor in INVALID_NODE_IDS:
            raise DecodeError(RejectReason.BAD_NODE_ID, f"report rtt neighbor {neighbor}")
        if rtt > RTT_MAX_MS:
            raise DecodeError(RejectReason.BAD_VALUE, f"report rtt {rtt}")
        rtts[neighbor] = rtt
    entries = []
    for i in range(count):
        neighbor, state, session, age = _REPORT_ENTRY.unpack_from(body, 1 + i * REPORT_ENTRY_SIZE)
        if neighbor in INVALID_NODE_IDS:
            raise DecodeError(RejectReason.BAD_NODE_ID, f"report neighbor {neighbor}")
        if session == 0:
            raise DecodeError(RejectReason.BAD_SESSION, "report session 0")
        if state not in tuple(NeighborCode):
            raise DecodeError(RejectReason.BAD_VALUE, f"report state {state}")
        entries.append(ReportEntry(neighbor, state, session, age, rtts.get(neighbor)))
    return TopologyReportPayload(tuple(entries))


def _check_size(name: str, body: bytes, expected: int) -> None:
    if len(body) != expected:
        raise DecodeError(RejectReason.BAD_PAYLOAD_LENGTH, f"{name} payload {len(body)} bytes")


def _decode_route_update(body: bytes) -> RouteUpdatePayload:
    _check_size("ROUTE_UPDATE", body, ROUTE_UPDATE_PAYLOAD_SIZE)
    epoch, version, destination, next_hop, backup = _ROUTE_UPDATE.unpack(body)
    if destination in INVALID_NODE_IDS or next_hop == U8_MAX or backup == U8_MAX:
        raise DecodeError(RejectReason.BAD_NODE_ID, "ROUTE_UPDATE node id")
    if epoch == 0 or version == 0:
        raise DecodeError(RejectReason.BAD_VALUE, "ROUTE_UPDATE epoch/version 0 is reserved")
    if backup != NO_NODE and (next_hop == NO_NODE or backup == next_hop):
        raise DecodeError(RejectReason.BAD_VALUE, "ROUTE_UPDATE backup without a distinct primary")
    return RouteUpdatePayload(
        epoch,
        version,
        destination,
        None if next_hop == NO_NODE else next_hop,
        None if backup == NO_NODE else backup,
    )


def _decode_route_ack(body: bytes) -> RouteInstallAckPayload:
    _check_size("ROUTE_INSTALL_ACK", body, ROUTE_INSTALL_ACK_PAYLOAD_SIZE)
    epoch, version, destination, status = _ROUTE_ACK.unpack(body)
    if destination in INVALID_NODE_IDS:
        raise DecodeError(RejectReason.BAD_NODE_ID, "ROUTE_INSTALL_ACK destination")
    if epoch == 0 or version == 0:
        raise DecodeError(RejectReason.BAD_VALUE, "ROUTE_INSTALL_ACK epoch/version 0")
    if status not in tuple(RouteAckStatus):
        raise DecodeError(RejectReason.BAD_VALUE, f"ROUTE_INSTALL_ACK status {status}")
    return RouteInstallAckPayload(epoch, version, destination, status)


def _decode_failover(body: bytes) -> LocalFailoverReportPayload:
    _check_size("LOCAL_FAILOVER_REPORT", body, LOCAL_FAILOVER_REPORT_PAYLOAD_SIZE)
    epoch, version, destination, from_hop, to_hop, reason = _FAILOVER.unpack(body)
    # to_hop 0 means "no usable backup": the node now has no route (Phase 4b)
    if destination in INVALID_NODE_IDS or from_hop in INVALID_NODE_IDS or to_hop == U8_MAX:
        raise DecodeError(RejectReason.BAD_NODE_ID, "LOCAL_FAILOVER_REPORT node id")
    if epoch == 0 or version == 0 or from_hop == to_hop or reason not in tuple(FailoverReason):
        raise DecodeError(RejectReason.BAD_VALUE, "LOCAL_FAILOVER_REPORT value")
    return LocalFailoverReportPayload(
        epoch, version, destination, from_hop, None if to_hop == NO_NODE else to_hop, reason
    )


def decode(data: bytes) -> Frame:
    """Validate and decode wire bytes, raising DecodeError with a RejectReason.

    Checks run in a fixed order (see docs/PROTOCOL.md) so every invalid input has
    exactly one expected reason.
    """
    if len(data) > MAX_FRAME_SIZE:
        raise DecodeError(RejectReason.OVERSIZE, f"{len(data)} bytes")
    if len(data) < HEADER_SIZE:
        raise DecodeError(RejectReason.TRUNCATED, f"{len(data)} bytes")

    version, raw_type, src, dst, session, seq, ttl, payload_len = HEADER_STRUCT.unpack_from(data)
    if version != PROTOCOL_VERSION:
        raise DecodeError(RejectReason.BAD_VERSION, f"version {version}")
    try:
        msg_type = MessageType(raw_type)
    except ValueError:
        raise DecodeError(RejectReason.UNKNOWN_TYPE, f"type 0x{raw_type:02x}") from None
    if msg_type not in IMPLEMENTED_TYPES:
        raise DecodeError(RejectReason.RESERVED_TYPE, msg_type.name)
    if src in INVALID_NODE_IDS or dst in INVALID_NODE_IDS:
        raise DecodeError(RejectReason.BAD_NODE_ID, f"src={src} dst={dst}")
    if session == 0:
        raise DecodeError(RejectReason.BAD_SESSION, "session 0 is reserved")
    if payload_len != len(data) - HEADER_SIZE:
        raise DecodeError(
            RejectReason.LENGTH_MISMATCH,
            f"header says {payload_len}, frame carries {len(data) - HEADER_SIZE}",
        )

    body = data[HEADER_SIZE:]
    payload: Payload
    if msg_type is MessageType.DATA:
        if len(body) != DATA_PAYLOAD_SIZE:
            raise DecodeError(RejectReason.BAD_PAYLOAD_LENGTH, f"DATA payload {len(body)} bytes")
        (value,) = _DATA.unpack(body)
        payload = DataPayload(value)
    elif msg_type is MessageType.DELIVERY_ACK:
        if len(body) != DELIVERY_ACK_PAYLOAD_SIZE:
            raise DecodeError(
                RejectReason.BAD_PAYLOAD_LENGTH, f"DELIVERY_ACK payload {len(body)} bytes"
            )
        origin, acked_session, acked_seq = _ACK.unpack(body)
        if origin in INVALID_NODE_IDS:
            raise DecodeError(RejectReason.BAD_NODE_ID, f"acked origin={origin}")
        if acked_session == 0:
            raise DecodeError(RejectReason.BAD_SESSION, "acked session 0 is reserved")
        payload = DeliveryAckPayload(origin, acked_session, acked_seq)
    elif msg_type is MessageType.HELLO:
        payload = _decode_hello(body)
    elif msg_type is MessageType.TOPOLOGY_REPORT:
        payload = _decode_report(body)
    elif msg_type is MessageType.PROBE:
        _check_size("PROBE", body, PROBE_PAYLOAD_SIZE)
        payload = ProbePayload()
    elif msg_type is MessageType.PROBE_REPLY:
        _check_size("PROBE_REPLY", body, PROBE_REPLY_PAYLOAD_SIZE)
        probe_session, probe_seq = _PROBE_REPLY.unpack(body)
        if probe_session == 0:
            raise DecodeError(RejectReason.BAD_SESSION, "probe session 0 is reserved")
        payload = ProbeReplyPayload(probe_session, probe_seq)
    elif msg_type is MessageType.ROUTE_UPDATE:
        payload = _decode_route_update(body)
    elif msg_type is MessageType.ROUTE_INSTALL_ACK:
        payload = _decode_route_ack(body)
    else:
        payload = _decode_failover(body)

    return Frame(src=src, dst=dst, session=session, seq=seq, ttl=ttl, payload=payload)
