"""Failure-detection and recovery measurements taken in simulation.

The reference time is the *scripted* stop or start in the scenario, which is exact in
simulation. On real boards the reference would be an instrumented cut and reporting delays
would add to the figure, so these numbers are simulated values, not hardware results.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from iot_backend.simulator.trace import FRAME_SENT, READING_DELIVERED, observed_paths


@dataclass(frozen=True)
class Detection:
    kind: str  # "loss" (stop -> lost contact declared) or "return" (start -> regained contact)
    node: str
    reference_ms: int  # the scripted stop/start time
    detected_ms: int | None  # when the controller declared it; None if it never did

    @property
    def latency_ms(self) -> int | None:
        return None if self.detected_ms is None else self.detected_ms - self.reference_ms


def detection_measurements(
    actions: Iterable[dict[str, Any]], events: Iterable[Any]
) -> list[Detection]:
    """Match each scripted stop_node / start_node to the first later controller event.

    `events` are topology events (objects with `.type`, `.time_ms` and `.data["node"]`).
    """
    events = list(events)
    wanted = {
        "stop_node": ("loss", "node_lost_contact"),
        "start_node": ("return", "node_regained_contact"),
    }
    results = []
    for action in sorted(actions, key=lambda a: a["at_ms"]):
        if action["action"] not in wanted:
            continue
        kind, event_type = wanted[action["action"]]
        reference = int(action["at_ms"])
        match = next(
            (
                e
                for e in events
                if e.type == event_type
                and e.data.get("node") == action["node"]
                and e.time_ms >= reference
            ),
            None,
        )
        results.append(
            Detection(kind, action["node"], reference, None if match is None else match.time_ms)
        )
    return results


@dataclass(frozen=True)
class Recovery:
    """One scripted failure and how the network recovered from it (simulation only).

    Every time is in simulated milliseconds. The reference is the scripted failure time, which
    is exact in simulation; on hardware it would be an instrumented cut. Route computation time
    is *not* here: it is wall-clock CPU time on the laptop (`ControllerStats.compute_us`), a
    different kind of number from these simulated-time values.
    """

    failure: str  # e.g. "stop_node B" or "link_down A->B"
    failure_ms: int
    detected_ms: int | None  # controller declared loss of contact / link no longer up
    computed_ms: int | None  # a replacement route was computed
    active_ms: int | None  # ... and confirmed by every node on it
    version: int | None
    new_path: tuple[str, ...] | None
    first_delivery_ms: int | None  # first reading accepted at C over the new path
    last_delivery_before_ms: int | None  # last reading accepted at C at or before the failure
    readings_lost: int  # readings first sent in [failure, first delivery] never accepted at C

    @staticmethod
    def _since(value: int | None, reference: int) -> int | None:
        return None if value is None else value - reference

    @property
    def detection_ms(self) -> int | None:
        return self._since(self.detected_ms, self.failure_ms)

    @property
    def route_active_after_ms(self) -> int | None:
        return self._since(self.active_ms, self.failure_ms)

    @property
    def distribution_ms(self) -> int | None:
        """From the route being computed to every node confirming it (not computation time)."""
        if self.computed_ms is None or self.active_ms is None:
            return None
        return self.active_ms - self.computed_ms

    @property
    def service_recovery_ms(self) -> int | None:
        return self._since(self.first_delivery_ms, self.failure_ms)

    @property
    def delivery_gap_ms(self) -> int | None:
        if self.first_delivery_ms is None or self.last_delivery_before_ms is None:
            return None
        return self.first_delivery_ms - self.last_delivery_before_ms


def recovery_measurement(
    actions: Iterable[dict[str, Any]],
    trace_events: Iterable[Any],
    route_events: Iterable[Any],
    topology_events: Iterable[Any],
    destination: str = "C",
) -> Recovery | None:
    """Measure recovery from the first scripted failure (a stopped node or a downed link).

    Delivery evidence comes from the trace (received-frame paths), not from the controller.
    """
    failures = [
        a
        for a in actions
        if a["action"] == "stop_node" or (a["action"] == "link_down" and a.get("down"))
    ]
    if not failures:
        return None
    failure = min(failures, key=lambda a: a["at_ms"])
    failure_ms = int(failure["at_ms"])
    trace = list(trace_events)
    routes = list(route_events)
    topology = list(topology_events)

    if failure["action"] == "stop_node":
        label = f"stop_node {failure['node']}"

        def is_detection(e: Any) -> bool:
            return e.type == "node_lost_contact" and e.data.get("node") == failure["node"]

    else:
        label = f"link_down {failure['link']}"
        ends = sorted(failure["link"].replace("->", "-").upper().split("-"))
        link = f"{ends[0]}-{ends[1]}"

        def is_detection(e: Any) -> bool:
            return (
                e.type == "link_state_changed"
                and e.data.get("link") == link
                and e.data.get("new") != "up"
            )

    detected = next((e for e in topology if e.time_ms >= failure_ms and is_detection(e)), None)
    computed = next(
        (e for e in routes if e.type == "route_computed" and e.time_ms >= failure_ms), None
    )
    active = None
    if computed is not None:
        active = next(
            (
                e
                for e in routes
                if e.type == "route_active" and e.data["version"] == computed.data["version"]
            ),
            None,
        )
    new_path = tuple(computed.data["path"]) if computed is not None else None
    relay = new_path[1] if new_path and len(new_path) > 2 else None

    delivered = [e for e in trace if e.event == READING_DELIVERED and e.node == destination]
    delivered_ids = {e.identity for e in delivered}
    first_delivery = None
    if computed is not None and active is not None and relay is not None:
        for e in delivered:
            # Only receipts after the replacement route existed count as service on the new
            # path; readings still arriving over the old path just after the failure do not.
            if e.time_ms < computed.time_ms:
                continue
            paths = observed_paths(trace, e.identity)
            if any(len(p) >= 3 and p[1] == relay and p[-1] == destination for p in paths):
                first_delivery = e
                break
    before = [e.time_ms for e in delivered if e.time_ms <= failure_ms]
    end = first_delivery.time_ms if first_delivery is not None else None
    first_sent: dict[Any, int] = {}
    for e in trace:
        if e.event == FRAME_SENT and e.info.get("msg") == "DATA":
            first_sent.setdefault(e.identity, e.time_ms)
    lost = sum(
        1
        for identity, at in first_sent.items()
        if at >= failure_ms and (end is None or at <= end) and identity not in delivered_ids
    )
    return Recovery(
        failure=label,
        failure_ms=failure_ms,
        detected_ms=None if detected is None else detected.time_ms,
        computed_ms=None if computed is None else computed.time_ms,
        active_ms=None if active is None else active.time_ms,
        version=None if computed is None else computed.data["version"],
        new_path=new_path,
        first_delivery_ms=end,
        last_delivery_before_ms=max(before) if before else None,
        readings_lost=lost,
    )
