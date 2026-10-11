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
    # Reserved for later phases; not accepted by the Phase 1 decoder.
    HELLO = 0x10
    TOPOLOGY_REPORT = 0x11
    ROUTE_UPDATE = 0x12
    ROUTE_INSTALL_ACK = 0x13
    PROBE = 0x14
    PROBE_REPLY = 0x15
    LOCAL_FAILOVER_REPORT = 0x16


IMPLEMENTED_TYPES = frozenset({MessageType.DATA, MessageType.DELIVERY_ACK})

DATA_PAYLOAD_SIZE = 4  # int32 value
DELIVERY_ACK_PAYLOAD_SIZE = 9  # origin u8, session u32, seq u32


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
