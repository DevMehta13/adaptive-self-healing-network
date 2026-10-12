"""Controller-side topology view, built only from gateway events.

It never sees node or simulator state: it takes a `Gateway` and nothing else. From topology
reports it keeps nodes and links, each with a freshness-aware state, and emits an event
whenever something changes. Nothing here calculates routes: `routable_links()` only hands the
route calculation (controller/routing.py) the links it may use and what each costs.

Rules (all times are the gateway clock):

* A report is **fresh** for `report_expiry_ms` after it arrives (default 15 s = 3 report
  intervals). Information from an expired report is never presented as current.
* A node is **lost contact** when *every* other node that has a fresh report mentioning it
  lists it as `lost`, and each of those reports arrived *after* the node's own latest report.
  Requiring agreement keeps a one-way link failure, which only one neighbor sees, from marking
  a node that is still heard elsewhere. The observers are recorded as the source. It is never
  described as "powered off": silence cannot prove that.
* A node is **reachable** when its own report is fresh and no such newer claim exists,
  **stale** when its latest report has expired and nothing newer exists, **unknown** when
  nothing has ever been heard about it.
* A link is **up** only when both ends report each other as two-way and both reports are
  fresh. It is **one_way** when only one direction is confirmed, **down** when neither
  direction is heard or an endpoint is lost contact, and **stale** when an end's report has
  expired. Only **up** links are usable.
* A link is **routable** (Phase 4a) when it is up *and* both ends report a measured round-trip
  time. Its cost is the larger of the two measurements: both ends measure the same round trip on
  their own clocks, and taking the larger one never makes a link look better than either end saw
  it. A link with a missing measurement is not routable, and is never given a default or zero
  cost. Nothing here subtracts timestamps from different boards.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from iot_backend.protocol import NODE_IDS, NODE_NAMES
from iot_backend.transports.gateway import (
    Gateway,
    GatewayEvent,
    NeighborObservation,
    TopologyReportEvent,
)

REACHABLE = "reachable"
LOST_CONTACT = "lost_contact"
STALE = "stale"
UNKNOWN = "unknown"

LINK_UP = "up"
LINK_ONE_WAY = "one_way"
LINK_DOWN = "down"
LINK_STALE = "stale"

_HALF = 2**31


def _newer(a: int, b: int) -> bool:
    """True if sequence number `a` is ahead of `b` (wrap-safe)."""
    return 0 < (a - b) % 2**32 < _HALF


def _name(node: int) -> str:
    return NODE_NAMES.get(node, str(node))


@dataclass(frozen=True)
class TopologyConfig:
    report_expiry_ms: int = 15_000
    max_events: int = 1_000


@dataclass(frozen=True)
class TopologyEvent:
    """A change in the controller's view. `event_id` increases by one per event."""

    event_id: int
    time_ms: int
    # node_seen, node_lost_contact, node_regained_contact, node_restarted, node_stale,
    # link_state_changed
    type: str
    data: dict[str, Any]


@dataclass
class _Report:
    received_ms: int
    session: int
    seq: int
    entries: dict[int, NeighborObservation]


@dataclass
class _NodeState:
    status: str = UNKNOWN
    since_ms: int = 0
    session: int | None = None  # latest own session seen
    observed_by: tuple[int, ...] = ()
    silent_for_ms: int = 0


@dataclass
class _LinkState:
    state: str
    directions: dict[str, bool] = field(default_factory=dict)
    since_ms: int = 0


class TopologyView:
    def __init__(self, gateway: Gateway, config: TopologyConfig | None = None) -> None:
        self._gateway = gateway
        self._config = config or TopologyConfig()
        self._reports: dict[int, _Report] = {}
        self._nodes: dict[int, _NodeState] = {}
        self._links: dict[tuple[int, int], _LinkState] = {}
        self._events: deque[TopologyEvent] = deque(maxlen=self._config.max_events)
        self._handlers: list[Callable[[TopologyEvent], None]] = []
        self._update_handlers: list[Callable[[], None]] = []
        self._next_event_id = 1
        self._timer_at: int | None = None
        gateway.subscribe(self._on_gateway_event)

    # ---- public ----

    @property
    def events(self) -> list[TopologyEvent]:
        return list(self._events)

    @property
    def last_event_id(self) -> int:
        return self._next_event_id - 1

    def subscribe(self, handler: Callable[[TopologyEvent], None]) -> None:
        """Call `handler` with every topology event (the stream the dashboard will use)."""
        self._handlers.append(handler)

    def subscribe_updates(self, handler: Callable[[], None]) -> None:
        """Call `handler` after each report has been absorbed and the view is consistent.

        Events are emitted *while* the view is being re-evaluated, so a handler that wants to
        read the view back (the route calculation) uses this instead.
        """
        self._update_handlers.append(handler)

    def node_status(self, name: str) -> str:
        return self._nodes[_id(name)].status if _id(name) in self._nodes else UNKNOWN

    def link_state(self, a: str, b: str) -> str | None:
        key = tuple(sorted((_id(a), _id(b))))
        state = self._links.get(key)  # type: ignore[arg-type]
        return state.state if state else None

    def node_sessions(self) -> dict[str, int]:
        """Latest own session of each node that has reported. A change means it restarted."""
        self._evaluate()
        return {
            _name(n): s.session for n, s in sorted(self._nodes.items()) if s.session is not None
        }

    def routable_links(self) -> list[tuple[str, str, int]]:
        """(a, b, cost_ms) for every up link whose two ends both report a measured RTT."""
        self._evaluate()
        result = []
        for (a, b), state in sorted(self._links.items()):
            if state.state != LINK_UP:
                continue
            cost = self._link_cost(a, b)
            if cost is not None:
                result.append((_name(a), _name(b), cost))
        return result

    def unmeasured_links(self) -> list[tuple[str, str]]:
        """Up links that are not routable because an end has no measurement."""
        self._evaluate()
        return [
            (_name(a), _name(b))
            for (a, b), state in sorted(self._links.items())
            if state.state == LINK_UP and self._link_cost(a, b) is None
        ]

    def _rtt_from(self, reporter: int, neighbor: int) -> int | None:
        report = self._reports.get(reporter)
        entry = None if report is None else report.entries.get(neighbor)
        return None if entry is None else entry.rtt_ms

    def _link_cost(self, a: int, b: int) -> int | None:
        rtt_a, rtt_b = self._rtt_from(a, b), self._rtt_from(b, a)
        if rtt_a is None or rtt_b is None:
            return None
        return max(rtt_a, rtt_b)

    def usable_links(self) -> list[tuple[str, str]]:
        """Links confirmed in both directions by fresh reports. Nothing else is usable."""
        self._evaluate()
        return sorted(
            (_name(a), _name(b)) for (a, b), s in self._links.items() if s.state == LINK_UP
        )

    def snapshot(self) -> dict[str, Any]:
        """Current view in the shape proposed for GET /api/snapshot (docs/DASHBOARD_API.md)."""
        self._evaluate()
        now = self._gateway.now_ms()
        nodes = []
        for node_id, state in sorted(self._nodes.items()):
            report = self._reports.get(node_id)
            entry: dict[str, Any] = {
                "id": _name(node_id),
                "status": state.status,
                "since_ms": state.since_ms,
                "session": state.session,
                "last_report_age_ms": None if report is None else now - report.received_ms,
            }
            if state.status == LOST_CONTACT:
                entry["source"] = {
                    "kind": "neighbor_reports",
                    "observed_by": [_name(o) for o in state.observed_by],
                    "silent_for_ms": state.silent_for_ms,
                }
            nodes.append(entry)
        links = [
            {
                "a": _name(a),
                "b": _name(b),
                "state": s.state,
                "usable": s.state == LINK_UP,
                "directions": dict(s.directions),
                "since_ms": s.since_ms,
                # Each end's own measurement of the round trip (None = unmeasured) and the
                # cost the route calculation would use (None = not routable).
                "rtt_ms": {_name(a): self._rtt_from(a, b), _name(b): self._rtt_from(b, a)},
                "cost_ms": self._link_cost(a, b) if s.state == LINK_UP else None,
            }
            for (a, b), s in sorted(self._links.items())
        ]
        return {
            "nodes": nodes,
            "links": links,
            "last_event_id": self.last_event_id,
            "report_expiry_ms": self._config.report_expiry_ms,
        }

    # ---- input ----

    def _on_gateway_event(self, event: GatewayEvent) -> None:
        if not isinstance(event, TopologyReportEvent):
            return
        previous = self._reports.get(event.origin)
        if previous and previous.session == event.session and not _newer(event.seq, previous.seq):
            return  # an older copy of a report we already have
        self._reports[event.origin] = _Report(
            received_ms=event.received_ms,
            session=event.session,
            seq=event.seq,
            entries={o.neighbor: o for o in event.neighbors},
        )
        self._evaluate()
        for handler in list(self._update_handlers):
            handler()

    def _on_timer(self) -> None:
        self._timer_at = None
        self._evaluate()

    # ---- evaluation ----

    def _fresh(self, report: _Report, now: int) -> bool:
        return now - report.received_ms <= self._config.report_expiry_ms

    def _evaluate(self) -> None:
        now = self._gateway.now_ms()
        fresh = {o: r for o, r in self._reports.items() if self._fresh(r, now)}
        known: set[int] = set(self._reports)
        for report in self._reports.values():
            known |= set(report.entries)

        # ---- nodes ----
        new_status: dict[int, tuple[str, tuple[int, ...], int]] = {}
        for node in known:
            own = self._reports.get(node)
            observers = [(o, r) for o, r in fresh.items() if o != node and node in r.entries]
            claims = [(o, r) for o, r in observers if r.entries[node].state == "lost"]
            # Every neighbor that currently has a fresh report must agree, and each of those
            # reports must be newer than the node's own latest report. One neighbor alone
            # (say, a one-way link failure) is not enough while others still hear the node.
            unanimous = (
                bool(claims)
                and len(claims) == len(observers)
                and (own is None or min(r.received_ms for _, r in claims) > own.received_ms)
            )
            if unanimous:
                new_status[node] = (
                    LOST_CONTACT,
                    tuple(sorted(o for o, _ in claims)),
                    max(r.entries[node].last_heard_ms_ago for _, r in claims),
                )
            elif own is not None and node in fresh:
                new_status[node] = (REACHABLE, (), 0)
            elif own is not None:
                new_status[node] = (STALE, (), 0)
            else:
                new_status[node] = (UNKNOWN, (), 0)

        for node in sorted(known):
            status, observers, silent = new_status[node]
            state = self._nodes.setdefault(node, _NodeState(since_ms=now))
            own = self._reports.get(node)
            old_status = state.status
            old_session = state.session
            if own is not None:
                state.session = own.session
            state.observed_by, state.silent_for_ms = observers, silent
            if (
                status == old_status == REACHABLE
                and old_session is not None
                and own is not None
                and own.session != old_session
            ):
                # Rebooted so quickly that nobody declared it lost: its RAM, including any
                # installed routes, is gone even though it never looked unreachable.
                self._emit(
                    "node_restarted",
                    {"node": _name(node), "old_session": old_session, "session": own.session},
                )
            if status == old_status:
                continue
            state.status, state.since_ms = status, now
            if status == LOST_CONTACT:
                self._emit(
                    "node_lost_contact",
                    {
                        "node": _name(node),
                        "observed_by": [_name(o) for o in observers],
                        "silent_for_ms": silent,
                        "basis": "fresh neighbor reports list it as lost (no HELLO heard)",
                    },
                )
            elif status == REACHABLE and old_status == LOST_CONTACT:
                self._emit(
                    "node_regained_contact",
                    {
                        "node": _name(node),
                        "session": state.session,
                        "restarted": old_session is not None and old_session != state.session,
                    },
                )
            elif status == REACHABLE:
                self._emit("node_seen", {"node": _name(node), "session": state.session})
            elif status == STALE:
                self._emit("node_stale", {"node": _name(node), "reason": "reports expired"})

        # ---- links ----
        pairs: set[tuple[int, int]] = set()
        for origin, report in self._reports.items():
            for neighbor in report.entries:
                pairs.add((min(origin, neighbor), max(origin, neighbor)))
        for pair in sorted(pairs):
            a, b = pair
            state_name, directions = self._link(a, b, fresh, new_status)
            previous = self._links.get(pair)
            if previous is not None and previous.state == state_name:
                previous.directions = directions  # same state: refresh silently
                continue
            self._links[pair] = _LinkState(state_name, directions, now)
            self._emit(
                "link_state_changed",
                {
                    "link": f"{_name(a)}-{_name(b)}",
                    "old": previous.state if previous else None,
                    "new": state_name,
                    "usable": state_name == LINK_UP,
                    "directions": directions,
                },
            )

        self._arm_timer(fresh, now)

    def _link(
        self,
        a: int,
        b: int,
        fresh: dict[int, _Report],
        status: dict[int, tuple[str, tuple[int, ...], int]],
    ) -> tuple[str, dict[str, bool]]:
        def observation(x: int, y: int) -> str | None:
            """What x says about y, or None if x's report is missing, expired or x is lost."""
            if x not in fresh or status[x][0] == LOST_CONTACT:
                return None
            entry = fresh[x].entries.get(y)
            return entry.state if entry else "absent"

        sa, sb = observation(a, b), observation(b, a)
        hears_ab = sa in ("one_way", "two_way")  # a hears b: frames b -> a work
        hears_ba = sb in ("one_way", "two_way")  # b hears a: frames a -> b work
        directions = {f"{_name(b)}->{_name(a)}": hears_ab, f"{_name(a)}->{_name(b)}": hears_ba}
        if status[a][0] == LOST_CONTACT or status[b][0] == LOST_CONTACT:
            return LINK_DOWN, {k: False for k in directions}
        if sa is None or sb is None:
            return LINK_STALE, directions
        if sa == "two_way" and sb == "two_way":
            return LINK_UP, directions
        if hears_ab or hears_ba:
            return LINK_ONE_WAY, directions
        return LINK_DOWN, directions

    def _arm_timer(self, fresh: dict[int, _Report], now: int) -> None:
        if not fresh:
            return
        due = min(r.received_ms for r in fresh.values()) + self._config.report_expiry_ms + 1
        if due > now and due != self._timer_at:
            self._timer_at = due
            self._gateway.call_later(due - now, self._on_timer)

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        event = TopologyEvent(self._next_event_id, self._gateway.now_ms(), type_, data)
        self._next_event_id += 1
        self._events.append(event)
        for handler in self._handlers:
            handler(event)


def _id(name: str) -> int:
    return NODE_IDS[name]
