"""The shapes of snapshot and event data, written down once and checked by tests.

A tiny validator on purpose: every field a dashboard may rely on is listed with its type, and
`None` in a type tuple means the field may be null. Extra fields are allowed (clients must
ignore unknown fields); missing or mistyped ones are reported.
"""

from typing import Any

Str, Int, Bool, List, Dict, Num = str, int, bool, list, dict, (int, float)
NoneType = type(None)

EVENT_SCHEMAS: dict[str, dict[str, Any]] = {
    # --- the controller's view of the topology ---
    "node_seen": {"node": Str, "session": Int},
    "node_lost_contact": {
        "node": Str,
        "observed_by": List,
        "silent_for_ms": Int,
        "basis": Str,
    },
    "node_regained_contact": {"node": Str, "session": Int, "restarted": Bool},
    "node_restarted": {"node": Str, "old_session": Int, "session": Int},
    "node_stale": {"node": Str, "reason": Str},
    "link_state_changed": {
        "link": Str,
        "old": (Str, NoneType),
        "new": Str,
        "usable": Bool,
        "directions": Dict,
    },
    # --- the controller's routing ---
    "route_computed": {
        "flow": Str,
        "epoch": Int,
        "version": Int,
        "path": List,
        "cost_ms": (Int, NoneType),
        "graph": List,
        "held_down": List,
        "trigger": Str,
        "backups": Dict,
        "backup_entries": Dict,
    },
    "route_installing": {"version": Int, "epoch": Int, "kind": Str, "phase": Str, "nodes": List},
    "route_active": {
        "version": Int,
        "epoch": Int,
        "path": List,
        "cost_ms": Int,
        "confirmed_by": List,
        "distribution_ms": Int,
        "trigger": Str,
        "backups": Dict,
    },
    "route_install_failed": {
        "version": Int,
        "epoch": Int,
        "kind": Str,
        "state": Str,
        "reason": Str,
        "nodes": List,
        "attempts": Int,
    },
    "no_available_route": {
        "flow": Str,
        "epoch": Int,
        "version": Int,
        "reason": Str,
        "graph": List,
        "held_down": List,
        "trigger": Str,
        "source_notified": Bool,
    },
    "no_route_confirmed": {
        "version": Int,
        "epoch": Int,
        "confirmed_by": List,
        "distribution_ms": Int,
    },
    "link_held_down": {"link": Str, "hold_down_ms": Int, "eligible_at_ms": Int},
    "epoch_advanced": {"old_epoch": Int, "held_epoch": Int, "epoch": Int},
    "local_failover": {
        "node": Str,
        "from": Str,
        "to": (Str, NoneType),
        "reason": Str,
        "epoch": Int,
        "version": Int,
        "link": Str,
        "matches_current_route": Bool,
        "outcome": Str,
        "received_ms": Int,
        "replayed": Bool,
    },
    # --- what reached C ---
    "reading_received": {
        "origin": Str,
        "session": Int,
        "seq": Int,
        "value": Num,
        "value_hundredths": Int,
        "via": Str,
        "hops": Int,
        "ttl": Int,
        "observed_path": List,
        "received_ms": Int,
        "replayed": Bool,
        "delay_ms": Int,
    },
    "telemetry": {"node": Str, "session": Int, "counters": Dict, "received_ms": Int},
    # --- the controller's link to node C (Phase 5b) ---
    "gateway_link_lost": {"since_ms": Int, "silent_for_ms": Int},
    "gateway_link_restored": {"lost_since_ms": Int, "lost_for_ms": Int},
    "replay_started": {
        "started_ms": Int,
        "entries": Int,
        "held": Int,
        "dropped_overflow": Int,
        "coalesced": Int,
        "outage_ms": Int,
    },
    "replay_finished": {"replayed": Int, "dropped_overflow": Int, "coalesced": Int},
    # --- the reliable uplink and the serial port (Phase 6-prep) ---
    "uplink_loss": {
        "reason": Str,
        "c_boot": Int,
        "first": Int,
        "last": Int,
        "count": Int,
        "readings": Int,
        "unknown": Int,
        "lower_bound": Bool,
    },
    "serial_port": {"port": (Str, NoneType), "state": Str, "detail": Str},
    # --- the environment channel and the runtime ---
    "environment": {
        "channel": Str,
        "source": Str,
        "action": Str,
        "target": (Str, NoneType),
        "detail": Dict,
        "label": Str,
    },
    "runtime": {"change": Str, "paused": Bool, "speed": Num},
    "reset": {"run_id": Int, "scenario": Str},
}

TELEMETRY_COUNTERS = (
    "created",
    "acknowledged",
    "lost",
    "no_route",
    "delivered",
    "forwarded",
    "dropped",
    "failovers",
    "send_failures",
)

SNAPSHOT_FIELDS: dict[str, Any] = {
    "v": Int,
    "mode": Str,
    "backend": Dict,
    "server_time": Str,
    "run_id": Int,
    "scenario": (Str, NoneType),
    "label": Str,
    "last_event_id": Int,
    "clock": Dict,
    "nodes": List,
    "links": List,
    "route": Dict,
    "gateway": Dict,
    "readings": Dict,
    "counts": Dict,
    "presenter": Dict,
    "recent_events": List,
}

NODE_FIELDS: dict[str, Any] = {
    "id": Str,
    "status": Str,
    "since_ms": Int,
    "session": (Int, NoneType),
    "last_report_age_ms": (Int, NoneType),
    "telemetry": (Dict, NoneType),
}

LINK_FIELDS: dict[str, Any] = {
    "a": Str,
    "b": Str,
    "state": Str,
    "usable": Bool,
    "directions": Dict,
    "since_ms": Int,
    "rtt_ms": Dict,
    "cost_ms": (Int, NoneType),
}


def _check(where: str, data: dict[str, Any], fields: dict[str, Any]) -> list[str]:
    problems = []
    for name, kind in fields.items():
        if name not in data:
            problems.append(f"{where}: missing {name!r}")
            continue
        value = data[name]
        # bool is an int in Python; keep the two apart so "true" never passes as a count
        if kind is not Bool and isinstance(value, bool) and kind in (Int, Num):
            problems.append(f"{where}.{name}: expected a number, got a bool")
        elif not isinstance(value, kind):
            problems.append(f"{where}.{name}: unexpected type {type(value).__name__}")
    return problems


def validate_event(event_type: str, data: dict[str, Any]) -> list[str]:
    """Problems with one event's data (an empty list means it matches the contract)."""
    if event_type not in EVENT_SCHEMAS:
        return [f"unknown event type {event_type!r}"]
    problems = _check(event_type, data, {"sim_ms": Int, **EVENT_SCHEMAS[event_type]})
    if event_type == "telemetry" and isinstance(data.get("counters"), dict):
        for name in TELEMETRY_COUNTERS:
            if not isinstance(data["counters"].get(name), int):
                problems.append(f"telemetry.counters: missing integer {name!r}")
    if event_type == "reading_received" and isinstance(data.get("observed_path"), list):
        path = data["observed_path"]
        if len(path) < 2 or path[-1] != "C" or path[0] != data.get("origin"):
            problems.append("reading_received.observed_path: must run origin ... C")
    return problems


def validate_envelope(envelope: dict[str, Any]) -> list[str]:
    problems = _check(
        "envelope", envelope, {"v": Int, "seq": Int, "ts": Str, "type": Str, "data": Dict}
    )
    extra = set(envelope) - {"v", "seq", "ts", "type", "data", "event_id"}
    if extra:
        problems.append(f"envelope: unexpected keys {sorted(extra)}")
    state_changing = envelope.get("type") in EVENT_SCHEMAS
    if state_changing and not isinstance(envelope.get("event_id"), int):
        problems.append("envelope: a state-changing event needs an integer event_id")
    if not state_changing and "event_id" in envelope:
        problems.append("envelope: only state-changing events carry an event_id")
    if state_changing and isinstance(envelope.get("data"), dict):
        problems += validate_event(envelope["type"], envelope["data"])
    return problems


def validate_snapshot(snapshot: dict[str, Any]) -> list[str]:
    problems = _check("snapshot", snapshot, SNAPSHOT_FIELDS)
    for node in snapshot.get("nodes", []):
        problems += _check(f"nodes[{node.get('id')}]", node, NODE_FIELDS)
    for link in snapshot.get("links", []):
        problems += _check(f"links[{link.get('a')}-{link.get('b')}]", link, LINK_FIELDS)
    clock = snapshot.get("clock", {})
    problems += _check("clock", clock, {"sim_ms": Int, "speed": Num, "paused": Bool})
    return problems
