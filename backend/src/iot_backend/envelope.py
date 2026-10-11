"""The single versioned envelope that wraps every WebSocket message."""

from datetime import UTC, datetime
from typing import Any

PROTOCOL_VERSION = 1


def utc_now() -> str:
    """Server time as an ISO 8601 UTC string, e.g. 2026-10-11T09:30:00.123Z."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_envelope(seq: int, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "seq": seq, "ts": utc_now(), "type": event_type, "data": data}
