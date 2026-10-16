"""Route calculation and installation (Phase 4a).

The controller turns the topology view into one route for the flow source -> destination
(A -> C; acknowledgements flood, so no reverse data route is needed) and installs it.

Rules, all of them testable from outside:

* **Graph.** Built only from `TopologyView.routable_links()`: links that are up (two-way, both
  reports fresh) *and* measured at both ends. An unmeasured link is left out; it is never given a
  default or zero cost. A link's cost is the larger of its two ends' RTT measurements.
* **Dijkstra**, minimum sum of costs. Ties are broken deterministically: fewer hops, then the
  lexicographically smaller list of node ids (so B, id 2, before D, id 4).
* **When to recalculate.** When the set of eligible links changes, or a node on the route
  restarted. Not when only RTT values change (no flapping on measurement noise). A link that
  *returns* is held down for `hold_down_ms` before routes may use it.
* **Installation.** Relays first; the source is told only after every relay has acknowledged, so
  the source never sends toward a relay that does not yet know the next hop. Each update is
  retried at most `install_max_attempts` times. A route is `active` only when every node that
  forwards on it has acknowledged. States: computed -> installing -> active, or
  failed (a node refused) / unconfirmed (acknowledgements missing) / superseded (replaced).
* **No path.** The controller reports `no_available_route` and tells the source explicitly with a
  ROUTE_UPDATE whose next hop is none (bounded retries as above).
* **Backups (Phase 4b, `backup_next_hops`).** Each forwarding node on the route also gets a
  backup next hop that is *loop-free and node-protecting*: the backup neighbor's own shortest
  path to the destination passes through neither this node nor the primary next hop (see
  `protecting_backups`). The backup relay's entry is installed and acknowledged, in the same
  route version, before the primary relays and the source; the route is active only when all of
  it is confirmed.
* **Local failover reports.** A node that switched to its backup says so (LOCAL_FAILOVER_REPORT).
  The controller records a *local failover* recovery event, stops using the abandoned link until
  it has been routable for the hold-down again, and installs the route that results: it never
  re-installs the primary the node just abandoned.
* **Versions.** Every update carries (epoch, version). Nodes ignore anything older than what they
  hold. The epoch identifies this controller run; a restarted controller must use a larger one.

Nothing here imports the simulator: the controller sees the network only through a `Gateway`.
"""

import heapq
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from iot_backend.controller.topology import TopologyView
from iot_backend.protocol import NODE_IDS, NODE_NAMES
from iot_backend.transports import (
    Gateway,
    GatewayEvent,
    LocalFailoverEvent,
    RouteInstallAckEvent,
    RouteUpdateCommand,
)

# Plan kinds and states
KIND_ROUTE = "route"
KIND_NO_ROUTE = "no_route"
COMPUTED = "computed"
INSTALLING = "installing"
ACTIVE = "active"
FAILED = "failed"  # a node refused the update
UNCONFIRMED = "unconfirmed"  # acknowledgements did not arrive within the bounded retries
SUPERSEDED = "superseded"

_HALF = 2**31
_U32 = 2**32


def _newer(a: int, b: int) -> bool:
    return 0 < (a - b) % _U32 < _HALF


def _name(node: int) -> str:
    return NODE_NAMES.get(node, str(node))


@dataclass(frozen=True)
class PathResult:
    path: tuple[int, ...]  # node ids from source to destination
    cost_ms: int


def shortest_path(
    links: Iterable[tuple[int, int, int]], source: int, destination: int
) -> PathResult | None:
    """Dijkstra over undirected links `(a, b, cost_ms)`; None if there is no path.

    Labels are ordered by (total cost, hop count, node-id list). Extending two paths by the same
    suffix keeps their order, so settling the smallest label first is still correct and the
    answer never depends on dictionary or insertion order.
    """
    adjacency: dict[int, list[tuple[int, int]]] = {}
    for a, b, cost in links:
        if cost < 0:
            raise ValueError("link costs must be non-negative")
        adjacency.setdefault(a, []).append((b, cost))
        adjacency.setdefault(b, []).append((a, cost))
    heap: list[tuple[int, int, tuple[int, ...]]] = [(0, 0, (source,))]
    settled: set[int] = set()
    while heap:
        cost, hops, path = heapq.heappop(heap)
        node = path[-1]
        if node in settled:
            continue
        settled.add(node)
        if node == destination:
            return PathResult(path, cost)
        for neighbor, weight in adjacency.get(node, ()):
            if neighbor not in settled:
                heapq.heappush(heap, (cost + weight, hops + 1, path + (neighbor,)))
    return None


@dataclass(frozen=True)
class BackupPlan:
    """Backups for a route: who falls back to whom, and which extra nodes need an entry."""

    backups: dict[int, int]  # forwarding node on the route -> its backup next hop
    entries: dict[int, int]  # node off the route that must forward: node -> next hop

    @property
    def signature(self) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
        return tuple(sorted(self.backups.items())), tuple(sorted(self.entries.items()))


def protecting_backups(links: Iterable[tuple[int, int, int]], path: tuple[int, ...]) -> BackupPlan:
    """Choose a loop-free, node-protecting backup next hop for each node on `path`.

    For node N on the path with primary next hop P, a neighbor n of N (n is not P) is a valid
    backup only if n's own shortest path to the destination exists and passes through neither N
    nor P:

    * not through N, or the backup would send the frame straight back (a loop);
    * not through P, or the backup would depend on the very relay whose failure it is meant to
      cover (it is "loop-free" but not "node-protecting").

    Among valid candidates the cheapest (cost to n plus n's cost to the destination, then fewer
    hops, then lower id) wins. Nodes off the route on a backup path get an entry too, and a
    candidate is dropped if it would give such a node (or a node already on the primary path)
    two different next hops, so one entry per node is always consistent. No valid backup means
    none: the relay B in the diamond has none, because its only other neighbor, A, reaches C
    through B itself.
    """
    links = list(links)
    adjacency: dict[int, dict[int, int]] = {}
    for a, b, cost in links:
        adjacency.setdefault(a, {})[b] = cost
        adjacency.setdefault(b, {})[a] = cost
    destination = path[-1]
    on_path = {node: path[i + 1] for i, node in enumerate(path[:-1])}
    backups: dict[int, int] = {}
    entries: dict[int, int] = {}
    for index, node in enumerate(path[:-1]):
        primary = path[index + 1]
        best: tuple[tuple[int, int, int], int, dict[int, int]] | None = None
        for neighbor in sorted(adjacency.get(node, {})):
            if neighbor == primary:
                continue
            route = shortest_path(links, neighbor, destination)
            if route is None or node in route.path or primary in route.path:
                continue
            needed = {
                hop: route.path[i + 1] for i, hop in enumerate(route.path[:-1])
            }  # entries along the backup path
            conflict = any(
                (hop in on_path and on_path[hop] != nxt) or (hop in entries and entries[hop] != nxt)
                for hop, nxt in needed.items()
            )
            if conflict:
                continue
            key = (adjacency[node][neighbor] + route.cost_ms, len(route.path), neighbor)
            if best is None or key < best[0]:
                best = (key, neighbor, needed)
        if best is not None:
            backups[node] = best[1]
            for hop, nxt in best[2].items():
                if hop not in on_path:
                    entries[hop] = nxt
    return BackupPlan(backups, entries)


@dataclass(frozen=True)
class RoutingConfig:
    source: str = "A"
    destination: str = "C"
    epoch: int = 1  # must grow across controller restarts (hardware: start time in seconds)
    hold_down_ms: int = 5_000  # a returning link must stay routable this long before use
    install_timeout_ms: int = 1_000  # wait this long for an update's acknowledgement
    install_max_attempts: int = 3  # transmissions of one update, including the first
    replan_delay_ms: int = 3_000  # after a failed installation, try again after this
    max_failed_plans: int = 3  # consecutive failed installations before waiting for a change
    no_route_grace_ms: int = 5_000  # at start, "no path" is "still discovering" for this long
    # Before the first route, wait until no new usable link has appeared for this long. Links are
    # measured one by one at startup; installing the first path found would often be replaced
    # within milliseconds. Later changes (a failure) are acted on immediately.
    initial_settle_ms: int = 1_000
    # Phase 4b: compute and install backup next hops. Off by default, so Phase 4a scenarios
    # keep their exact behaviour; nodes use backups only if they also run local failover.
    backup_next_hops: bool = False
    # Phase 5b: the heartbeat to node C (0 = none) and how long silence from C means the
    # gateway link is lost. These are between the laptop and C, never on the radio.
    heartbeat_interval_ms: int = 0
    link_timeout_ms: int = 3_000
    max_events: int = 1_000

    def __post_init__(self) -> None:
        if not 1 <= self.epoch < _U32:
            raise ValueError("epoch must be 1..2^32-1")
        if self.install_max_attempts < 1 or self.max_failed_plans < 0:
            raise ValueError("attempt limits must be positive")
        if min(self.hold_down_ms, self.install_timeout_ms, self.replan_delay_ms) < 0:
            raise ValueError("times must be non-negative")


@dataclass(frozen=True)
class RouteEvent:
    """A route-related event. Ids are per controller; Phase 5 merges streams and renumbers."""

    event_id: int
    time_ms: int
    # route_computed, route_installing, route_active, route_install_failed, no_available_route,
    # no_route_confirmed, link_held_down, epoch_advanced
    type: str
    data: dict[str, Any]


@dataclass
class _Plan:
    version: int
    kind: str
    path: tuple[int, ...]  # empty for a no-route plan
    cost_ms: int | None
    sessions: dict[int, int]  # session of each path node when the plan was computed
    computed_ms: int
    state: str = COMPUTED
    trigger: str = ""
    backups: dict[int, int] = field(default_factory=dict)  # node -> backup next hop
    entries: dict[int, int] = field(default_factory=dict)  # off-route backup-path nodes
    phases: list[list[int]] = field(default_factory=list)
    phase_index: int = 0
    outstanding: dict[int, int] = field(default_factory=dict)  # node -> transmissions so far
    confirmed: list[int] = field(default_factory=list)


@dataclass
class ControllerStats:
    updates_sent: int = 0  # first transmissions
    retries: int = 0  # retransmissions of an unacknowledged update
    acks_ignored: int = 0  # acknowledgements that matched no outstanding update
    plans: int = 0
    failovers_seen: int = 0  # distinct local failover reports acted on
    failover_reports_repeated: int = 0  # repeats of a report already handled
    # Wall-clock time spent in Dijkstra on this laptop, in microseconds. Not simulated time.
    compute_us: deque[int] = field(default_factory=lambda: deque(maxlen=1_000))


class RouteController:
    def __init__(
        self,
        view: TopologyView,
        gateway: Gateway,
        config: RoutingConfig | None = None,
    ) -> None:
        self._view = view
        self._gateway = gateway
        self._config = config or RoutingConfig()
        self._source = NODE_IDS[self._config.source]
        self._destination = NODE_IDS[self._config.destination]
        self._epoch = self._config.epoch
        self._version = 0
        self._started_ms = gateway.now_ms()
        self._plan: _Plan | None = None
        self._grace_timer_at: int | None = None
        self._settle_timer_at: int | None = None
        self._last_eligible: frozenset[tuple[int, int]] | None = None
        self._last_change_ms = self._started_ms
        self._basis: tuple[frozenset[tuple[int, int]], tuple[tuple[str, int], ...]] | None = None
        self._last_cost: dict[tuple[int, int], int] = {}  # last measured cost of a routable link
        self._since: dict[tuple[int, int], int] = {}
        self._eligible_at: dict[tuple[int, int], int] = {}
        self._seen: set[tuple[int, int]] = set()
        self._failed_streak = 0
        self._forced_trigger: str | None = None
        self._handled_failovers: deque[tuple[int, int, int, int, int | None]] = deque(maxlen=32)
        self._busy = False
        self._again = False
        self._scheduled = False
        self._stopped = False
        self._events: deque[RouteEvent] = deque(maxlen=self._config.max_events)
        self._next_event_id = 1
        self._handlers: list[Callable[[RouteEvent], None]] = []
        self.stats = ControllerStats()

        gateway.subscribe(self._on_gateway_event)
        view.subscribe(self._on_view_event)
        view.subscribe_updates(self._reconcile)
        self._reconcile()

    # ---- public ----

    @property
    def events(self) -> list[RouteEvent]:
        return list(self._events)

    @property
    def last_event_id(self) -> int:
        return self._next_event_id - 1

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def version(self) -> int:
        return self._version

    def subscribe(self, handler: Callable[[RouteEvent], None]) -> None:
        self._handlers.append(handler)

    def stop(self) -> None:
        """The controller process ends: it sends and reacts to nothing from now on."""
        self._stopped = True

    def route_state(self) -> dict[str, Any]:
        """The current plan, in the shape proposed for the dashboard snapshot."""
        plan = self._plan
        flow = f"{self._config.source}->{self._config.destination}"
        if plan is None:
            return {
                "flow": flow,
                "epoch": self._epoch,
                "state": "waiting",
                "kind": None,
                "version": None,
            }
        return {
            "flow": flow,
            "epoch": self._epoch,
            "version": plan.version,
            "kind": plan.kind,
            "state": plan.state,
            "path": [_name(n) for n in plan.path],
            "cost_ms": plan.cost_ms,
            "confirmed_by": [_name(n) for n in plan.confirmed],
            "waiting_for": [_name(n) for n in sorted(plan.outstanding)],
            "backups": {_name(n): _name(b) for n, b in sorted(plan.backups.items())},
            "backup_entries": {_name(n): _name(h) for n, h in sorted(plan.entries.items())},
        }

    # ---- inputs ----

    def _on_gateway_event(self, event: GatewayEvent) -> None:
        if self._stopped:
            return
        if isinstance(event, RouteInstallAckEvent):
            self._on_ack(event)
        elif isinstance(event, LocalFailoverEvent):
            self._on_failover(event)

    def _on_view_event(self, _event: object) -> None:
        # Fired while the view is mid-evaluation: read it back only after it has finished.
        if self._stopped or self._scheduled:
            return
        self._scheduled = True
        self._gateway.call_later(0, self._run_scheduled)

    def _run_scheduled(self) -> None:
        self._scheduled = False
        self._reconcile()

    # ---- deciding ----

    def _reconcile(self) -> None:
        if self._stopped:
            return
        if self._busy:
            self._again = True
            return
        self._busy = True
        try:
            while True:
                self._again = False
                self._reconcile_once()
                if not self._again:
                    break
        finally:
            self._busy = False

    def _reconcile_once(self) -> None:
        now = self._gateway.now_ms()
        routable = {
            (min(NODE_IDS[a], NODE_IDS[b]), max(NODE_IDS[a], NODE_IDS[b])): cost
            for a, b, cost in self._view.routable_links()
        }
        # Fail-static on silence: a link whose information has merely *expired* (the controller
        # stopped hearing reports, for instance while detached) is not known to be down, so it
        # keeps its last known cost and is not treated as having left. Only a link the view
        # says is down or one-way leaves the graph.
        for a, b in self._view.stale_links():
            key = (min(NODE_IDS[a], NODE_IDS[b]), max(NODE_IDS[a], NODE_IDS[b]))
            if key in self._since and key in self._last_cost and key not in routable:
                routable[key] = self._last_cost[key]
        self._last_cost.update(routable)
        self._update_hold_down(routable, now)
        eligible = {key: cost for key, cost in routable.items() if now >= self._eligible_at[key]}
        if frozenset(eligible) != self._last_eligible:
            self._last_eligible = frozenset(eligible)
            self._last_change_ms = now
        if self._plan is None and eligible:
            wait = self._config.initial_settle_ms - (now - self._last_change_ms)
            if wait > 0:
                due = now + wait
                if self._settle_timer_at != due:
                    self._settle_timer_at = due
                    self._gateway.call_later(wait, self._reconcile)
                return
        sessions = self._view.node_sessions()
        basis = (frozenset(eligible), tuple(sorted(sessions.items())))
        if basis == self._basis:
            return
        topology_changed = self._basis is None or basis[0] != self._basis[0]
        self._basis = basis
        if topology_changed and self._forced_trigger is None:
            self._failed_streak = 0
        self._decide(eligible, sessions, now)

    def _update_hold_down(self, routable: dict[tuple[int, int], int], now: int) -> None:
        for key in routable:
            if key in self._since:
                continue
            self._since[key] = now
            returning = key in self._seen
            self._seen.add(key)
            hold = self._config.hold_down_ms if returning else 0
            self._eligible_at[key] = now + hold
            if returning and hold > 0:
                self._emit(
                    "link_held_down",
                    {
                        "link": f"{_name(key[0])}-{_name(key[1])}",
                        "hold_down_ms": hold,
                        "eligible_at_ms": now + hold,
                    },
                )
                self._gateway.call_later(hold, self._reconcile)
        for key in [k for k in self._since if k not in routable]:
            del self._since[key]  # it left; if it comes back it is a returning link
            del self._eligible_at[key]

    def _decide(
        self, eligible: dict[tuple[int, int], int], sessions: dict[str, int], now: int
    ) -> None:
        links = [(a, b, cost) for (a, b), cost in sorted(eligible.items())]
        started = time.perf_counter_ns()
        result = shortest_path(links, self._source, self._destination)
        self.stats.compute_us.append((time.perf_counter_ns() - started) // 1_000)

        trigger = self._forced_trigger
        self._forced_trigger = None
        plan = self._plan
        if trigger is None:
            trigger = "initial" if plan is None else "topology_changed"
        if result is None:
            self._no_path(links, now, trigger)
            return
        extra = (
            protecting_backups(links, result.path)
            if self._config.backup_next_hops
            else BackupPlan({}, {})
        )
        involved = set(result.path) | set(extra.entries)
        node_sessions = {n: sessions.get(_name(n), 0) for n in involved}
        if (
            plan is not None
            and plan.kind == KIND_ROUTE
            and plan.path == result.path
            and (tuple(sorted(plan.backups.items())), tuple(sorted(plan.entries.items())))
            == extra.signature
            and plan.state in (INSTALLING, ACTIVE)
        ):
            if plan.sessions == node_sessions:
                return  # the same route, backups included, is already installing or in use
            trigger = "node_restarted"
        self._start_plan(KIND_ROUTE, result, node_sessions, links, now, trigger, extra)

    def _no_path(self, links: list[tuple[int, int, int]], now: int, trigger: str) -> None:
        plan = self._plan
        if plan is not None and plan.kind == KIND_NO_ROUTE and plan.state in (INSTALLING, ACTIVE):
            return  # already declared and being (or done being) announced
        grace_left = self._started_ms + self._config.no_route_grace_ms - now
        if plan is None and grace_left > 0:
            # Right after start, silence means "still discovering", not "no route".
            self._basis = None
            due = now + grace_left + 1
            if self._grace_timer_at != due:
                self._grace_timer_at = due
                self._gateway.call_later(grace_left + 1, self._reconcile)
            return
        self._start_plan(KIND_NO_ROUTE, None, {}, links, now, trigger)

    # ---- plans ----

    def _start_plan(
        self,
        kind: str,
        result: PathResult | None,
        sessions: dict[int, int],
        links: list[tuple[int, int, int]],
        now: int,
        trigger: str,
        extra: BackupPlan | None = None,
    ) -> None:
        if self._plan is not None and self._plan.state in (COMPUTED, INSTALLING):
            self._plan.state = SUPERSEDED
        self._version += 1
        self.stats.plans += 1
        path = result.path if result else ()
        plan = _Plan(
            version=self._version,
            kind=kind,
            path=path,
            cost_ms=result.cost_ms if result else None,
            sessions=sessions,
            computed_ms=now,
            trigger=trigger,
        )
        if extra is not None:
            plan.backups, plan.entries = dict(extra.backups), dict(extra.entries)
        self._plan = plan
        flow = f"{self._config.source}->{self._config.destination}"
        graph = [{"link": f"{_name(a)}-{_name(b)}", "cost_ms": c} for a, b, c in links]
        held = [
            {"link": f"{_name(a)}-{_name(b)}", "eligible_in_ms": at - now}
            for (a, b), at in sorted(self._eligible_at.items())
            if at > now
        ]
        if kind == KIND_ROUTE:
            # Order of installation: backup relays (nodes off the route that a backup points
            # at), then the primary relays (which hold backups of their own), then the source.
            # Nothing is ever told to fall back onto a node that has no entry yet.
            plan.phases = [p for p in (sorted(plan.entries), list(path[1:-1]), [path[0]]) if p]
            self._emit(
                "route_computed",
                {
                    "flow": flow,
                    "epoch": self._epoch,
                    "version": plan.version,
                    "path": [_name(n) for n in path],
                    "cost_ms": plan.cost_ms,
                    "graph": graph,
                    "held_down": held,
                    "trigger": trigger,
                    "backups": {_name(n): _name(b) for n, b in sorted(plan.backups.items())},
                    "backup_entries": {_name(n): _name(h) for n, h in sorted(plan.entries.items())},
                },
            )
        else:
            plan.phases = [[self._source]]
            self._emit(
                "no_available_route",
                {
                    "flow": flow,
                    "epoch": self._epoch,
                    "version": plan.version,
                    "reason": "no usable measured path",
                    "graph": graph,
                    "held_down": held,
                    "trigger": trigger,
                    "source_notified": False,
                },
            )
        self._begin_phase(plan)

    def _begin_phase(self, plan: _Plan) -> None:
        plan.state = INSTALLING
        nodes = plan.phases[plan.phase_index]
        if plan.phase_index == len(plan.phases) - 1:
            phase = "source"
        elif plan.kind == KIND_ROUTE and nodes and nodes[0] in plan.entries:
            phase = "backup_relays"
        else:
            phase = "relays"
        self._emit(
            "route_installing",
            {
                "version": plan.version,
                "epoch": self._epoch,
                "kind": plan.kind,
                "phase": phase,
                "nodes": [_name(n) for n in nodes],
            },
        )
        plan.outstanding = {n: 0 for n in nodes}
        for node in nodes:
            self._send_update(plan, node)

    def _send_update(self, plan: _Plan, node: int) -> None:
        plan.outstanding[node] += 1
        attempt = plan.outstanding[node]
        if attempt == 1:
            self.stats.updates_sent += 1
        else:
            self.stats.retries += 1
        backup: int | None = None
        if plan.kind == KIND_NO_ROUTE:
            next_hop = None
        elif node in plan.entries:
            next_hop = plan.entries[node]  # a backup relay: it forwards, with no backup of its own
        else:
            next_hop = plan.path[plan.path.index(node) + 1]
            backup = plan.backups.get(node)
        self._gateway.send_route_update(
            RouteUpdateCommand(
                node=node,
                epoch=self._epoch,
                version=plan.version,
                destination=self._destination,
                next_hop=next_hop,
                backup_next_hop=backup,
            )
        )
        self._gateway.call_later(
            self._config.install_timeout_ms, lambda: self._on_timeout(plan, node, attempt)
        )

    def _on_timeout(self, plan: _Plan, node: int, attempt: int) -> None:
        if (
            self._stopped
            or self._plan is not plan
            or plan.state != INSTALLING
            or plan.outstanding.get(node) != attempt
        ):
            return  # acknowledged, superseded or already failed
        if attempt < self._config.install_max_attempts:
            self._send_update(plan, node)
            return
        self._fail(plan, UNCONFIRMED, "no acknowledgement", sorted(plan.outstanding))

    def _on_ack(self, ack: RouteInstallAckEvent) -> None:
        plan = self._plan
        if ack.destination != self._destination:
            return
        if ack.status == "stale":
            self._on_stale(ack)
            return
        if (
            plan is None
            or plan.state != INSTALLING
            or ack.epoch != self._epoch
            or ack.version != plan.version
            or ack.origin not in plan.outstanding
        ):
            self.stats.acks_ignored += 1  # late, duplicate, or for a replaced plan
            return
        if ack.status == "rejected":
            self._fail(plan, FAILED, "rejected by node", [ack.origin])
            return
        del plan.outstanding[ack.origin]
        plan.confirmed.append(ack.origin)
        if plan.outstanding:
            return
        plan.phase_index += 1
        if plan.phase_index < len(plan.phases):
            self._begin_phase(plan)  # relays are confirmed; now tell the source
            return
        plan.state = ACTIVE
        self._failed_streak = 0
        now = self._gateway.now_ms()
        if plan.kind == KIND_ROUTE:
            self._emit(
                "route_active",
                {
                    "version": plan.version,
                    "epoch": self._epoch,
                    "path": [_name(n) for n in plan.path],
                    "cost_ms": plan.cost_ms,
                    "confirmed_by": [_name(n) for n in plan.confirmed],
                    "distribution_ms": now - plan.computed_ms,
                    "trigger": plan.trigger,
                    "backups": {_name(n): _name(b) for n, b in sorted(plan.backups.items())},
                },
            )
        else:
            self._emit(
                "no_route_confirmed",
                {
                    "version": plan.version,
                    "epoch": self._epoch,
                    "confirmed_by": [_name(n) for n in plan.confirmed],
                    "distribution_ms": now - plan.computed_ms,
                },
            )

    def _on_stale(self, ack: RouteInstallAckEvent) -> None:
        """A node refused an update because it holds a newer pair (carried in the ACK)."""
        plan = self._plan
        if _newer(ack.epoch, self._epoch):
            # Another controller run (or a clock set backwards) left nodes ahead of us.
            previous = self._epoch
            self._epoch = (ack.epoch + 1) % _U32 or 1
            self._version = 0
            self._emit(
                "epoch_advanced",
                {"old_epoch": previous, "held_epoch": ack.epoch, "epoch": self._epoch},
            )
            if plan is not None and plan.state in (COMPUTED, INSTALLING):
                plan.state = SUPERSEDED
            self._basis = None
            self._forced_trigger = "epoch_advanced"
            self._failed_streak = 0
            self._reconcile()
            return
        if ack.epoch == self._epoch and plan is not None and ack.version > plan.version:
            # Our version counter is behind what the node holds: catch up and plan again.
            self._version = max(self._version, ack.version)
            if plan.state == INSTALLING:
                self._fail(plan, FAILED, "node holds a newer version", [ack.origin])
            return
        self.stats.acks_ignored += 1  # about an older update; the current one is unaffected

    def _on_failover(self, report: LocalFailoverEvent) -> None:
        """A node switched to its backup (or found none) on its own.

        Recorded as a *local failover*, which is a different recovery from a controller reroute.
        The link the node abandoned is treated as having left the graph: it comes back only
        after it has been routable for the hold-down, so the controller cannot send the node
        back onto the primary it just gave up. Then the route is calculated again, which
        normally confirms the backup path as the new primary route (with a new backup if one
        exists) or installs something better.
        """
        key = (
            report.origin,
            report.epoch,
            report.version,
            report.from_next_hop,
            report.to_next_hop,
        )
        if key in self._handled_failovers:
            self.stats.failover_reports_repeated += 1
            return  # a repeat of a report already acted on
        self._handled_failovers.append(key)
        self.stats.failovers_seen += 1
        plan = self._plan
        current = plan is not None and (report.epoch, report.version) == (
            self._epoch,
            plan.version,
        )
        link = (min(report.origin, report.from_next_hop), max(report.origin, report.from_next_hop))
        self._emit(
            "local_failover",
            {
                "node": _name(report.origin),
                "from": _name(report.from_next_hop),
                "to": None if report.to_next_hop is None else _name(report.to_next_hop),
                "reason": report.reason,
                "epoch": report.epoch,
                "version": report.version,
                "link": f"{_name(link[0])}-{_name(link[1])}",
                "matches_current_route": current,
                "outcome": "no_route" if report.to_next_hop is None else "backup_in_use",
                # when the report reached C (estimated from its age if C held it)
                "received_ms": report.received_ms,
                "replayed": report.replayed,
            },
        )
        # treat the abandoned link as gone: if it returns (or is still reported up) it must be
        # routable for the hold-down before routes use it again
        self._since.pop(link, None)
        self._eligible_at.pop(link, None)
        self._seen.add(link)
        self._basis = None
        self._forced_trigger = "local_failover_report"
        self._reconcile()

    def _fail(self, plan: _Plan, state: str, reason: str, nodes: list[int]) -> None:
        plan.state = state
        self._emit(
            "route_install_failed",
            {
                "version": plan.version,
                "epoch": self._epoch,
                "kind": plan.kind,
                "state": state,
                "reason": reason,
                "nodes": [_name(n) for n in nodes],
                "attempts": self._config.install_max_attempts,
            },
        )
        if self._failed_streak >= self._config.max_failed_plans:
            return  # give up until the topology changes: bounded, never an endless loop
        self._failed_streak += 1
        self._gateway.call_later(self._config.replan_delay_ms, lambda: self._replan(plan))

    def _replan(self, plan: _Plan) -> None:
        if self._stopped or self._plan is not plan or plan.state not in (FAILED, UNCONFIRMED):
            return
        self._basis = None
        self._forced_trigger = "retry_after_failure"
        self._reconcile()

    # ---- events ----

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        event = RouteEvent(self._next_event_id, self._gateway.now_ms(), type_, data)
        self._next_event_id += 1
        self._events.append(event)
        for handler in self._handlers:
            handler(event)
