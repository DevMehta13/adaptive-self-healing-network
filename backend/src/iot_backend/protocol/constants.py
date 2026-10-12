"""Constants of the node protocol. The normative description is docs/PROTOCOL.md."""

import struct
from enum import IntEnum, StrEnum

PROTOCOL_VERSION = 1

# version, type, src, dst, session, seq, ttl, payload_len -- little-endian, no padding.
HEADER_STRUCT = struct.Struct("<BBBBIIBB")
HEADER_SIZE = HEADER_STRUCT.size  # 14

MAX_FRAME_SIZE = 250  # ESP-NOW v1 limit for the whole frame
MAX_PAYLOAD_SIZE = MAX_FRAME_SIZE - HEADER_SIZE  # 236

DEFAULT_TTL = 8

U8_MAX = 0xFF
U32_MAX = 0xFFFFFFFF
I32_MIN = -(2**31)
I32_MAX = 2**31 - 1

# Node ids: 0 is invalid, 255 is reserved (broadcast, not used yet).
NODE_A, NODE_B, NODE_C, NODE_D = 1, 2, 3, 4
NODE_NAMES = {NODE_A: "A", NODE_B: "B", NODE_C: "C", NODE_D: "D"}
NODE_IDS = {name: node_id for node_id, name in NODE_NAMES.items()}
INVALID_NODE_IDS = frozenset({0, 255})


class MessageType(IntEnum):
    DATA = 0x01
    DELIVERY_ACK = 0x02
    HELLO = 0x10
    TOPOLOGY_REPORT = 0x11
    ROUTE_UPDATE = 0x12
    ROUTE_INSTALL_ACK = 0x13
    PROBE = 0x14
    PROBE_REPLY = 0x15
    LOCAL_FAILOVER_REPORT = 0x16  # defined in Phase 4a, sent from Phase 4b
    TELEMETRY = 0x17  # Phase 5a: a node's own counters, for the dashboard


# Every code above has a payload definition. A code added to MessageType before its payload is
# defined stays out of this set and is rejected as `reserved_type`.
IMPLEMENTED_TYPES = frozenset(MessageType)

DATA_PAYLOAD_SIZE = 4  # int32 value
DELIVERY_ACK_PAYLOAD_SIZE = 9  # origin u8, session u32, seq u32

# HELLO payload: count u8, then count x (node u8, session u32).
# TOPOLOGY_REPORT payload: count u8, then count x (neighbor u8, state u8, session u32, age u16).
MAX_HELLO_NEIGHBORS = 8
MAX_REPORT_ENTRIES = 8
HELLO_ENTRY_SIZE = 5
REPORT_ENTRY_SIZE = 8
AGE_MAX_MS = 0xFFFF  # "last heard" ages saturate here

# Optional TOPOLOGY_REPORT trailer (Phase 4a): count u8, then count x (neighbor u8, rtt_ms u16).
# It is present only when at least one neighbor has a measurement, so reports without
# measurements keep their Phase 3 bytes exactly.
RTT_ENTRY_SIZE = 3
RTT_MAX_MS = 0xFFFE  # 0xFFFF is never sent; it is the "unmeasured" value, not a cost
NO_NODE = 0  # "none" in a next-hop field (0 is not a valid node id)

# PROBE has no payload. PROBE_REPLY echoes the probe's own session and seq (u32, u32).
PROBE_PAYLOAD_SIZE = 0
PROBE_REPLY_PAYLOAD_SIZE = 8
# ROUTE_UPDATE: epoch u32, version u32, destination u8, next_hop u8, backup_next_hop u8.
ROUTE_UPDATE_PAYLOAD_SIZE = 11
# ROUTE_INSTALL_ACK: epoch u32, version u32, destination u8, status u8.
ROUTE_INSTALL_ACK_PAYLOAD_SIZE = 10
# LOCAL_FAILOVER_REPORT: epoch u32, version u32, destination u8, from u8, to u8, reason u8.
LOCAL_FAILOVER_REPORT_PAYLOAD_SIZE = 12
# TELEMETRY: created, acknowledged, lost, no_route, delivered, forwarded, dropped (u32
# each), failovers u16, send_failures u16. Counters since the node's boot, saturating.
TELEMETRY_PAYLOAD_SIZE = 32


class NeighborCode(IntEnum):
    """A neighbor's state as carried in TOPOLOGY_REPORT. 0 is invalid; unknown is absence."""

    ONE_WAY = 1  # we hear its HELLOs, but it does not report hearing ours
    TWO_WAY = 2  # we hear it and it reports hearing us
    LOST = 3  # heard before, nothing within the timeout


class RouteAckStatus(IntEnum):
    """Outcome carried in ROUTE_INSTALL_ACK."""

    INSTALLED = 1  # the update is now (or already was) the node's entry for the destination
    STALE = 2  # the node holds a newer version; epoch/version then carry the node's held values
    REJECTED = 3  # the node cannot use it (next hop is not a permitted neighbor, table full)


class FailoverReason(IntEnum):
    """Why a node switched to its backup (LOCAL_FAILOVER_REPORT; sent from Phase 4b)."""

    NEIGHBOR_LOST = 1  # T1: the next hop is no longer a two-way neighbor (lost or one-way)
    SEND_FAILURES = 2  # T2: K consecutive link-layer delivery failures to the next hop


class RejectReason(StrEnum):
    """Why decode() refused a frame. The strings appear in tests/protocol-examples/."""

    OVERSIZE = "oversize"
    TRUNCATED = "truncated"
    BAD_VERSION = "bad_version"
    UNKNOWN_TYPE = "unknown_type"
    RESERVED_TYPE = "reserved_type"
    BAD_NODE_ID = "bad_node_id"
    BAD_SESSION = "bad_session"
    LENGTH_MISMATCH = "length_mismatch"
    BAD_PAYLOAD_LENGTH = "bad_payload_length"
    BAD_VALUE = "bad_value"
