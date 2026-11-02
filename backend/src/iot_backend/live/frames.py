"""Frame records: the radio frames node C handed up, as the packets view shows them.

Every frame C hands to the laptop (and every one the protocol codec refuses) becomes one record:
C's uplink number, the time it reached C, the neighbor that handed it over, the exact bytes, the
decoded 14-byte header and a short summary of the payload. Decoding is the protocol codec's: the
record only names what `decode` already returned, so there is one decoder and not two.

Scope, stated honestly: only frames C hands up are here (DATA it accepts, new or repeated, and the
reports, acknowledgements and telemetry addressed to C). HELLO, PROBE, DELIVERY_ACK, frames the
controller originates and every hop between other boards never reach the laptop. Version, type and
length are decoded from the bytes, not measured. In simulation the bytes come from the
simulator's codec, not from a radio.

Records are not state-changing events: they have no `event_id` and never enter the event history.
They live in a bounded `FrameLog` (recent records plus cumulative counts) and are announced on the
WebSocket in batches (api/ws.py).
"""

from collections import deque
from typing import Any

from iot_backend.protocol import (
    HEADER_SIZE,
    NODE_NAMES,
    PROTOCOL_VERSION,
    DataPayload,
    FailoverReason,
    Frame,
    LocalFailoverReportPayload,
    MessageType,
    NeighborCode,
    RouteAckStatus,
    RouteInstallAckPayload,
    TelemetryPayload,
    TopologyReportPayload,
)
from iot_backend.transports.laptop_gateway import FrameObserved

FRAME_LOG_SIZE = 200  # recent records kept for the snapshot
HANDED_UP_TYPES = (
    MessageType.DATA,
    MessageType.TOPOLOGY_REPORT,
    MessageType.ROUTE_INSTALL_ACK,
    MessageType.LOCAL_FAILOVER_REPORT,
    MessageType.TELEMETRY,
)
TELEMETRY_SHORT = (
    ("created", "created"),
    ("acknowledged", "acknowledged"),
    ("lost", "lost"),
    ("no_route", "no route"),
    ("delivered", "delivered"),
    ("forwarded", "forwarded"),
    ("dropped", "dropped"),
    ("failovers", "failovers"),
    ("send_failures", "send failures"),
)


def _name(node: int) -> str:
    return NODE_NAMES.get(node, str(node))


def _hop(node: int | None) -> str | None:
    return None if node is None else _name(node)


def _describe_payload(frame: Frame) -> tuple[dict[str, Any], str]:
    """The payload as plain data, and a one-line summary of it."""
    p = frame.payload
    if isinstance(p, DataPayload):
        return (
            {"value_hundredths": p.value, "value": p.value / 100},
            f"reading {p.value / 100:.2f} ({p.value} hundredths)",
        )
    if isinstance(p, TopologyReportPayload):
        neighbors = [
            {
                "node": _name(e.neighbor),
                "state": NeighborCode(e.state).name.lower(),
                "session": e.session,
                "last_heard_ms_ago": e.age_ms,
                "rtt_ms": e.rtt_ms,
                "probes_ok": e.probes_ok,
                "probes_total": e.probes_total,
            }
            for e in p.entries
        ]
        listing = ", ".join(f"{n['node']} {n['state']}" for n in neighbors) or "no neighbors"
        return {"neighbors": neighbors}, f"{len(neighbors)} neighbors: {listing}"
    if isinstance(p, RouteInstallAckPayload):
        status = RouteAckStatus(p.status).name.lower()
        return (
            {
                "epoch": p.epoch,
                "version": p.version,
                "destination": _name(p.destination),
                "status": status,
            },
            f"route version {p.version} for {_name(p.destination)}: {status}",
        )
    if isinstance(p, LocalFailoverReportPayload):
        reason = FailoverReason(p.reason).name.lower()
        return (
            {
                "epoch": p.epoch,
                "version": p.version,
                "destination": _name(p.destination),
                "from": _name(p.from_next_hop),
                "to": _hop(p.to_next_hop),
                "reason": reason,
            },
            f"failover {_name(p.from_next_hop)} -> {_hop(p.to_next_hop) or 'no backup'} ({reason})",
        )
    if isinstance(p, TelemetryPayload):
        counters = {key: getattr(p, key) for key, _ in TELEMETRY_SHORT}
        shown = [f"{label} {counters[key]}" for key, label in TELEMETRY_SHORT if counters[key]]
        return {"counters": counters}, ", ".join(shown) or "all counters 0"
    raise ValueError(f"{frame.msg_type.name} is never handed up")  # pragma: no cover


def frame_record(observed: FrameObserved, record_id: int) -> dict[str, Any]:
    """The record for one frame C handed up (or one the codec refused).

    `id` counts records for the whole backend run and never restarts; a client uses it to drop
    records a snapshot already contains. `received_ms` is on the controller's clock; for a late
    frame it is the estimate "now minus the age C reports", as for readings, and may be negative
    in hardware mode when C held the frame for longer than the backend has been running.
    """
    up, frame = observed.up, observed.frame
    record: dict[str, Any] = {
        "id": record_id,
        "c_boot": up.boot,
        "n": up.n,
        "received_ms": observed.received_ms,
        "delay_ms": up.age_ms,
        "replayed": up.replayed,
        "via": _name(up.via),
        "dup": None,
        "hex": up.frame.hex(),
        "size": len(up.frame),
        "header": None,
        "payload": None,
        "rejected": None,
    }
    if frame is None:
        record["summary"] = f"rejected: {observed.rejected}"
        record["rejected"] = {"reason": observed.rejected}
        return record
    payload, summary = _describe_payload(frame)
    record["dup"] = up.duplicate if frame.msg_type is MessageType.DATA else None
    record["header"] = {
        # decode() accepts only this version and requires payload_len to equal the bytes after
        # the header, so both are what the bytes say, as checked by the codec
        "version": PROTOCOL_VERSION,
        "type": int(frame.msg_type),
        "type_name": frame.msg_type.name,
        "src": _name(frame.src),
        "dst": _name(frame.dst),
        "session": frame.session,
        "seq": frame.seq,
        "ttl": frame.ttl,
        "payload_len": len(up.frame) - HEADER_SIZE,
    }
    record["payload"] = payload
    record["summary"] = summary
    return record


class FrameLog:
    """The most recent records and cumulative counts. Memory is bounded by `capacity`."""

    def __init__(self, capacity: int = FRAME_LOG_SIZE) -> None:
        self.capacity = capacity
        self._recent: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.total = 0
        self.by_type = {t.name: 0 for t in HANDED_UP_TYPES}
        self.duplicates = 0
        self.replayed = 0
        self.rejected = 0
        self.rejected_by_reason: dict[str, int] = {}

    def add(self, record: dict[str, Any]) -> None:
        self._recent.append(record)
        self.total += 1
        if record["replayed"]:
            self.replayed += 1
        if record["rejected"] is not None:
            self.rejected += 1
            reason = record["rejected"]["reason"]
            self.rejected_by_reason[reason] = self.rejected_by_reason.get(reason, 0) + 1
            return
        self.by_type[record["header"]["type_name"]] += 1
        if record["dup"]:
            self.duplicates += 1

    def describe(self, last_id: int, source: str) -> dict[str, Any]:
        """The snapshot's `frames` section."""
        return {
            "last_id": last_id,
            "capacity": self.capacity,
            "source": source,
            "counts": {
                "total": self.total,
                "by_type": dict(self.by_type),
                "duplicates": self.duplicates,
                "replayed": self.replayed,
                "rejected": self.rejected,
                "rejected_by_reason": dict(self.rejected_by_reason),
            },
            "recent": list(self._recent),
        }
