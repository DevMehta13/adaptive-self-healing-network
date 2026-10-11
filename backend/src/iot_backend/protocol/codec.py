"""Binary encoding and validated decoding of protocol frames."""

import struct

from iot_backend.protocol.constants import (
    DATA_PAYLOAD_SIZE,
    DELIVERY_ACK_PAYLOAD_SIZE,
    HEADER_SIZE,
    HEADER_STRUCT,
    I32_MAX,
    I32_MIN,
    IMPLEMENTED_TYPES,
    INVALID_NODE_IDS,
    MAX_FRAME_SIZE,
    PROTOCOL_VERSION,
    U8_MAX,
    U32_MAX,
    MessageType,
    RejectReason,
)
from iot_backend.protocol.messages import DataPayload, DeliveryAckPayload, Frame, Payload

_DATA = struct.Struct("<i")
_ACK = struct.Struct("<BII")


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
    if len(data) > MAX_FRAME_SIZE:  # cannot happen with Phase 1 payloads; guards future ones
        raise EncodeError(f"frame is {len(data)} bytes, limit is {MAX_FRAME_SIZE}")
    return data


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
    else:
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

    return Frame(src=src, dst=dst, session=session, seq=seq, ttl=ttl, payload=payload)
