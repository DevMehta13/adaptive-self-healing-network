"""The single versioned envelope that wraps every WebSocket message."""

from datetime import UTC, datetime
from typing import Any

PROTOCOL_VERSION = 1


def utc_now() -> str:
    """Server time as an ISO 8601 UTC string, e.g. 2026-10-11T09:30:00.123Z."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_envelope(
    seq: int,
    event_type: str,
    data: dict[str, Any],
    event_id: int | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Wrap one event. `event_id` is present only for state-changing events."""
    envelope: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "seq": seq,
        "ts": ts if ts is not None else utc_now(),
        "type": event_type,
        "data": data,
    }
    if event_id is not None:
        envelope["event_id"] = event_id
    return envelope
