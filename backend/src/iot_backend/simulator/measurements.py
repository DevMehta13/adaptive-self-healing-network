"""Failure-detection and recovery measurements taken in simulation.

The reference time is the *scripted* stop or start in the scenario, which is exact in
simulation. On real boards the reference would be an instrumented cut and reporting delays
would add to the figure, so these numbers are simulated values, not hardware results.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from iot_backend.simulator.trace import (
    FRAME_SENT,
    LOCAL_FAILOVER,
    READING_DELIVERED,
    ROUTE_INSTALLED,
    observed_paths,
)


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
    # Phase 4b: when did the source table start pointing at the replacement relay, by what
    # means, and when did it first actually send a reading that way?
    switch_ms: int | None = None
    switch_by: str | None = None  # "local_failover" or "controller_route"
    first_send_ms: int | None = None
    local_failover_trigger: str | None = None  # "T1_neighbor_state" or "T2_send_failures"

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
    def switch_after_ms(self) -> int | None:
        return self._since(self.switch_ms, self.failure_ms)

    @property
    def first_send_after_ms(self) -> int | None:
        return self._since(self.first_send_ms, self.failure_ms)

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

    # the relay in use before the failure, and the replacement the controller settled on
    before = [e for e in routes if e.type == "route_active" and e.time_ms <= failure_ms]
    old_relay = before[-1].data["path"][1] if before and len(before[-1].data["path"]) > 2 else None
    after = [e for e in routes if e.type == "route_computed" and e.time_ms >= failure_ms]
    computed = next((e for e in after if e.data["path"][1:2] != [old_relay]), None)
    if computed is None and after:
        computed = after[0]
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
    source = new_path[0] if new_path else "A"

    # when the source table switched to the replacement relay, and by what means
    switch_ms = switch_by = trigger = None
    if relay is not None:
        for e in sorted(trace, key=lambda x: x.time_ms):
            if e.time_ms < failure_ms or e.node != source:
                continue
            if e.event == LOCAL_FAILOVER and e.info.get("to") == relay:
                switch_ms, switch_by, trigger = e.time_ms, "local_failover", e.info["trigger"]
                break
            if e.event == ROUTE_INSTALLED and e.info.get("next_hop") == relay:
                switch_ms, switch_by = e.time_ms, "controller_route"
                break
    first_send = None
    if relay is not None:
        first_send = next(
            (
                e.time_ms
                for e in trace
                if e.event == FRAME_SENT
                and e.node == source
                and e.info.get("msg") == "DATA"
                and e.info.get("to") == relay
                and e.time_ms >= failure_ms
            ),
            None,
        )

    delivered = [e for e in trace if e.event == READING_DELIVERED and e.node == destination]
    delivered_ids = {e.identity for e in delivered}
    # Readings still arriving over the old relay just after the failure are not recovery.
    # If the replacement relay is the old one (a link failure that kept the same path shape),
    # only receipts after the replacement route existed count.
    cutoff = failure_ms
    if relay == old_relay and computed is not None:
        cutoff = computed.time_ms
    first_delivery = None
    if computed is not None and relay is not None:
        for e in delivered:
            if e.time_ms < cutoff:
                continue
            paths = observed_paths(trace, e.identity)
            if any(len(p) >= 3 and p[1] == relay and p[-1] == destination for p in paths):
                first_delivery = e
                break
    before_ms = [e.time_ms for e in delivered if e.time_ms <= failure_ms]
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
        last_delivery_before_ms=max(before_ms) if before_ms else None,
        readings_lost=lost,
        switch_ms=switch_ms,
        switch_by=switch_by,
        first_send_ms=first_send,
        local_failover_trigger=trigger,
    )
