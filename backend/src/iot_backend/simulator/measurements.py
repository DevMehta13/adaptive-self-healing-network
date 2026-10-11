"""Failure-detection measurements taken in simulation.

The reference time is the *scripted* stop or start in the scenario, which is exact in
simulation. On real boards the reference would be an instrumented cut and reporting delays
would add to the figure, so these numbers are simulated values, not hardware results.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


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
