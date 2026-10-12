"""Virtual nodes. They talk only in encoded bytes, as the real boards will.

Every node runs the same logic; what differs is configuration:

* a **permitted-neighbor list** (the logical topology): the node refuses to send to, and
  rejects frames received from, any other node, even though all boards can physically hear
  each other;
* a **routing table** (destination -> next hop) used for DATA: configured by hand (Phase 2
  manual mode) and/or installed by the controller with ROUTE_UPDATE (Phase 4a);
* optionally a **reading destination**: the node then creates numbered readings and retries
  them end to end until acknowledged or out of attempts;
* optionally a **gateway sink**: the node is C and hands topology reports to the controller.

Two kinds of traffic (docs/ARCHITECTURE.md):

* **Data** (DATA) follows the routing tables hop by hop.
* **Management** (TOPOLOGY_REPORT, ROUTE_UPDATE, ROUTE_INSTALL_ACK, and DELIVERY_ACK unless
  `ack_delivery="routed"`) is flooded over permitted links, once per node, independent of the
  routing tables. ROUTE_UPDATE is only accepted when it was created by the gateway node.
* **HELLO** and **PROBE / PROBE_REPLY** are local: one hop to a permitted neighbor, never
  forwarded. A probe's round trip is timed on the prober's own clock.
"""

import random
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from iot_backend.protocol import (
    DEFAULT_TTL,
    NODE_NAMES,
    DataPayload,
    DecodeError,
    DeliveryAckPayload,
    Frame,
    HelloNeighbor,
    HelloPayload,
    MessageType,
    NeighborCode,
    PacketIdentity,
    ProbePayload,
    ProbeReplyPayload,
    ReportEntry,
    RouteAckStatus,
    RouteInstallAckPayload,
    RouteUpdatePayload,
    TopologyReportPayload,
    decode,
    encode,
)
from iot_backend.protocol.constants import (
    AGE_MAX_MS,
    MAX_HELLO_NEIGHBORS,
    MAX_REPORT_ENTRIES,
    RTT_MAX_MS,
)
from iot_backend.simulator.dedup import DuplicateFilter, FloodCache, Verdict
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.trace import (
    ACK_IGNORED,
    ACK_RECEIVED,
    ACK_TIMEOUT,
    DUPLICATE_RECEIVED,
    FRAME_DROPPED,
    FRAME_FORWARDED,
    FRAME_RECEIVED,
    FRAME_REJECTED,
    FRAME_SENT,
    NEIGHBOR_CHANGED,
    NEIGHBOR_RESTARTED,
    NODE_BOOTED,
    NODE_STOPPED,
    READING_DELIVERED,
    READING_LOST,
    ROUTE_INSTALLED,
    ROUTE_UPDATE_IGNORED,
    ROUTE_UPDATE_SENT,
    Trace,
)
from iot_backend.transports import RouteUpdateCommand

# transmit(from_id, to_id, data, identity, msg): put encoded bytes on the air towards `to_id`.
# `msg` is the message-type name; only the simulated link uses it (to pick frames to drop).
TransmitFunction = Callable[[int, int, bytes, PacketIdentity, str], None]
GatewaySink = Callable[[Frame], None]

# Epochs, versions and sequence numbers are compared with serial-number arithmetic.
_HALF = 2**31


def _newer(a: int, b: int) -> bool:
    """True if `a` is ahead of `b` modulo 2^32 (wrap-safe)."""
    return 0 < (a - b) % 2**32 < _HALF


_FLOODED_ALWAYS = frozenset(
    {
        MessageType.TOPOLOGY_REPORT,
        MessageType.ROUTE_UPDATE,
        MessageType.ROUTE_INSTALL_ACK,
        MessageType.LOCAL_FAILOVER_REPORT,
    }
)

ACK_FLOODING = "flooding"
ACK_ROUTED = "routed"


@dataclass(frozen=True)
class NodeSettings:
    ttl: int = DEFAULT_TTL
    queue_capacity: int = 16  # frames waiting to be transmitted, including the one in service
    service_ms: int = 5  # time the radio needs per frame
    reading_interval_ms: int = 2000
    ack_timeout_ms: int = 1000  # per attempt
    max_attempts: int = 3  # transmissions per reading, including the first
    max_pending: int = 32  # readings awaiting acknowledgement
    dedup_window: int = 64
    dedup_sessions: int = 8
    max_receipts: int = 1024
    # Phase 3: discovery and management traffic
    discovery: bool = True  # HELLOs, neighbor table and topology reports
    ack_delivery: str = ACK_FLOODING  # "flooding" (Phase 3) or "routed" (Phase 2 behaviour)
    hello_interval_ms: int = 1000
    hello_jitter_ms: int = 100  # each interval is hello_interval_ms +/- up to this much
    neighbor_timeout_ms: int = 3000  # no HELLO for this long -> lost
    report_interval_ms: int = 5000  # periodic topology report; changes are sent promptly
    report_coalesce_ms: int = 50  # wait this long to batch several changes into one report
    flood_cache_size: int = 64  # management frame identities remembered per node
    gateway_id: int = 3
    # Phase 4a: probes (link round-trip time) and installed routes
    probe_interval_ms: int = 0  # 0 = probing off
    probe_jitter_ms: int = 100
    rtt_window: int = 5  # most recent samples kept per neighbor
    rtt_min_samples: int = 3  # fewer fresh samples than this = "unmeasured"
    rtt_max_age_ms: int = 10_000  # older samples are forgotten
    max_routes: int = 8  # installed route entries (one per destination)


@dataclass
class NodeCounters:
    boots: int = 0
    created: int = 0  # readings created
    attempts: int = 0  # DATA transmissions attempted for those readings (first tries + retries)
    sent: int = 0  # frames this node put on the air (created or forwarded)
    forwarded: int = 0  # frames relayed for someone else
    delivered_unique: int = 0
    duplicates: int = 0
    acknowledged: int = 0
    lost: int = 0
    hellos: int = 0  # HELLO frames created
    reports: int = 0  # TOPOLOGY_REPORT frames created
    probes: int = 0  # PROBE frames created
    dropped_no_route: int = 0  # readings dropped at creation because no route existed


@dataclass
class _Pending:
    frame: Frame
    attempts: int = 0
    first_attempt_ms: int = 0


@dataclass
class _Outgoing:
    frame: Frame
    next_hop: int
    previous_hop: int | None  # None when this node created the frame
    attempt: int | None


@dataclass
class _Neighbor:
    session: int
    last_heard_ms: int
    state: NeighborCode


@dataclass
class _Installed:
    """A controller-installed route entry. `next_hop` None means "told: no route"."""

    epoch: int
    version: int
    next_hop: int | None
    backup_next_hop: int | None  # stored, never used in Phase 4a


@dataclass
class _Probe:
    """The one outstanding probe to a neighbor."""

    seq: int
    sent_ms: int


class Node:
    def __init__(
        self,
        node_id: int,
        engine: Engine,
        trace: Trace,
        transmit: TransmitFunction,
        seed: int,
        *,
        permitted: set[int],
        routes: dict[int, int],
        settings: NodeSettings,
        reading_dst: int | None = None,
        initial_seq: int = 1,
    ) -> None:
        if len(permitted) > MAX_HELLO_NEIGHBORS:
            raise ValueError(f"a node may have at most {MAX_HELLO_NEIGHBORS} neighbors")
        self.id = node_id
        self.name = NODE_NAMES.get(node_id, str(node_id))
        self._engine = engine
        self._trace = trace
        self._transmit_bytes = transmit
        self._rng = random.Random(f"{seed}:node:{self.name}")
        # A separate generator for HELLO timing, so enabling discovery does not change the
        # session ids or reading values a node would otherwise draw.
        self._jitter_rng = random.Random(f"{seed}:jitter:{self.name}")
        self._probe_rng = random.Random(f"{seed}:probe:{self.name}")
        self.permitted = set(permitted)
        self.routes = dict(routes)  # configuration: survives a stop/start
        self._settings = settings
        self._reading_dst = reading_dst
        self._initial_seq = initial_seq
        self.gateway_sink: GatewaySink | None = None

        self.running = False
        self._epoch = 0  # incremented on boot and stop so timers from an earlier life do nothing
        self._session = 0
        self._next_seq = initial_seq
        self._pending: dict[PacketIdentity, _Pending] = {}
        self._queue: deque[_Outgoing] = deque()
        self._busy = False
        self._dedup = DuplicateFilter(settings.dedup_window, settings.dedup_sessions)
        self._flood = FloodCache(settings.flood_cache_size)
        self._neighbors: dict[int, _Neighbor] = {}
        self._report_pending = False
        self._installed: dict[int, _Installed] = {}  # RAM: lost on stop or boot
        self._probes: dict[int, _Probe] = {}
        self._rtt: dict[int, deque[tuple[int, int]]] = {}  # neighbor -> (time, rtt) samples
        self._rtt_reported: dict[int, bool] = {}  # last "has a measurement" per neighbor
        # (identity, value, time) of accepted readings; bounded, oldest dropped.
        self.receipts: deque[tuple[PacketIdentity, int, int]] = deque(maxlen=settings.max_receipts)

        self.counters = NodeCounters()
        self.drops: Counter[str] = Counter()  # frames discarded or rejected here, by reason

    # ---- life cycle ----

    @property
    def session(self) -> int:
        return self._session

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    @property
    def flood_cache_evictions(self) -> int:
        return self._flood.evictions

    def boot(self) -> None:
        """Start the node with a new random session and empty RAM."""
        previous = self._session
        while True:
            session = self._rng.randrange(1, 2**32)
            if session != previous:
                break
        self._epoch += 1
        self.running = True
        self._session = session
        self._next_seq = self._initial_seq
        self._pending.clear()
        self._queue.clear()
        self._busy = False
        self._dedup = DuplicateFilter(self._settings.dedup_window, self._settings.dedup_sessions)
        self._flood = FloodCache(self._settings.flood_cache_size, self._flood.evictions)
        self._neighbors.clear()
        self._report_pending = False
        self._installed.clear()
        self._probes.clear()
        self._rtt.clear()
        self._rtt_reported.clear()
        self.counters.boots += 1
        self._trace.record(
            self._engine.now_ms,
            self.name,
            NODE_BOOTED,
            None,
            session=f"{session:08x}",
            boot=self.counters.boots,
        )
        epoch = self._epoch
        if self._reading_dst is not None:
            self._engine.schedule(
                self._settings.reading_interval_ms, lambda: self._create_reading(epoch)
            )
        if self._settings.discovery:
            # Start at a random point in the interval so nodes that boot together do not
            # transmit their HELLOs at the same instant forever.
            first_hello = self._jitter_rng.randrange(self._settings.hello_interval_ms)
            self._engine.schedule(first_hello, lambda: self._hello_tick(epoch))
            first_report = self._settings.hello_interval_ms + self._jitter_rng.randrange(100)
            self._engine.schedule(first_report, lambda: self._report_tick(epoch))
        if self._settings.probe_interval_ms > 0:
            first_probe = self._probe_rng.randrange(self._settings.probe_interval_ms)
            self._engine.schedule(first_probe, lambda: self._probe_tick(epoch))

    def stop(self) -> None:
        """Power off: RAM is lost, incoming frames are ignored, pending timers do nothing."""
        if not self.running:
            return
        self._epoch += 1
        self.running = False
        self._pending.clear()  # unresolved: neither acknowledged nor lost
        self._queue.clear()
        self._busy = False
        self._neighbors.clear()
        self._installed.clear()
        self._probes.clear()
        self._rtt.clear()
        self._rtt_reported.clear()
        self._trace.record(self._engine.now_ms, self.name, NODE_STOPPED)

    # ---- sending ----

    def _new_frame(self, dst: int, payload: object, ttl: int | None = None) -> Frame:
        frame = Frame(
            self.id,
            dst,
            self._session,
            self._next_seq,
            self._settings.ttl if ttl is None else ttl,
            payload,  # type: ignore[arg-type]
        )
        self._next_seq = (self._next_seq + 1) % 2**32  # sequence numbers wrap
        return frame

    def _drop(self, reason: str, identity: PacketIdentity | None, **info: object) -> None:
        self.drops[reason] += 1
        self._trace.record(
            self._engine.now_ms, self.name, FRAME_DROPPED, identity, reason=reason, **info
        )

    def _reject(self, reason: str, identity: PacketIdentity | None, **info: object) -> None:
        self.drops[reason] += 1
        self._trace.record(
            self._engine.now_ms, self.name, FRAME_REJECTED, identity, reason=reason, **info
        )

    def _name(self, node_id: int) -> str:
        return NODE_NAMES.get(node_id, str(node_id))

    def _lookup(self, dst: int) -> int | None:
        """Next hop for `dst`: a controller-installed entry wins over a configured one."""
        installed = self._installed.get(dst)
        if installed is not None:
            return installed.next_hop  # None = the controller told us there is no route
        return self.routes.get(dst)

    def route_state(self, dst: int) -> tuple[int, int, int | None] | None:
        """(epoch, version, next_hop) of an installed entry, for tests and the dashboard."""
        held = self._installed.get(dst)
        return None if held is None else (held.epoch, held.version, held.next_hop)

    def _route(self, frame: Frame, previous_hop: int | None, attempt: int | None) -> None:
        """Data path: pick the next hop from the routing table and queue the frame."""
        next_hop = self._lookup(frame.dst)
        if next_hop is None:
            self._drop("no_route", frame.identity, dst=self._name(frame.dst))
            return
        self._enqueue(frame, next_hop, previous_hop, attempt)

    def _enqueue(
        self, frame: Frame, next_hop: int, previous_hop: int | None, attempt: int | None
    ) -> None:
        if next_hop not in self.permitted:
            # Enforced here, not by the absence of a link: the radio could reach it.
            self._drop("not_permitted_neighbor", frame.identity, to=self._name(next_hop))
            return
        if len(self._queue) >= self._settings.queue_capacity:
            self._drop("queue_full", frame.identity, to=self._name(next_hop))
            return
        self._queue.append(_Outgoing(frame, next_hop, previous_hop, attempt))
        if not self._busy:
            self._start_service()

    def _start_service(self) -> None:
        self._busy = True
        epoch = self._epoch
        self._engine.schedule(self._settings.service_ms, lambda: self._finish_service(epoch))

    def _finish_service(self, epoch: int) -> None:
        if epoch != self._epoch or not self._queue:
            return
        item = self._queue.popleft()
        data = encode(item.frame)
        self.counters.sent += 1
        if item.previous_hop is None:
            info: dict[str, object] = {
                "msg": item.frame.msg_type.name,
                "to": self._name(item.next_hop),
                "size": len(data),
            }
            if item.attempt is not None:
                info["attempt"] = item.attempt
            self._trace.record(
                self._engine.now_ms, self.name, FRAME_SENT, item.frame.identity, **info
            )
        else:
            self.counters.forwarded += 1
            self._trace.record(
                self._engine.now_ms,
                self.name,
                FRAME_FORWARDED,
                item.frame.identity,
                msg=item.frame.msg_type.name,
                sender=self._name(item.previous_hop),
                to=self._name(item.next_hop),
                ttl=item.frame.ttl,
            )
        self._transmit_bytes(
            self.id, item.next_hop, data, item.frame.identity, item.frame.msg_type.name
        )
        if self._queue:
            self._start_service()
        else:
            self._busy = False

    # ---- receiving ----

    def receive(self, data: bytes, from_node: int) -> None:
        """A frame's bytes arrived at this node's radio from neighbor `from_node`.

        The sender is the previous hop, as reported by the radio (ESP-NOW gives the sender's
        MAC). It is not necessarily the frame's origin (`src`).
        """
        sender = self._name(from_node)
        if not self.running:
            self._drop("node_stopped", None, sender=sender)
            return
        try:
            frame = decode(data)
        except DecodeError as error:
            self._trace.record(
                self._engine.now_ms, self.name, FRAME_RECEIVED, None, sender=sender, size=len(data)
            )
            self._reject(error.reason.value, None, sender=sender, size=len(data))
            return
        self._trace.record(
            self._engine.now_ms,
            self.name,
            FRAME_RECEIVED,
            frame.identity,
            sender=sender,
            msg=frame.msg_type.name,
            ttl=frame.ttl,
        )
        if from_node not in self.permitted:
            self._reject("not_permitted_neighbor", frame.identity, sender=sender)
            return
        if frame.msg_type is MessageType.HELLO:
            self._on_hello(frame, from_node)
        elif frame.msg_type in (MessageType.PROBE, MessageType.PROBE_REPLY):
            self._on_probe(frame, from_node)
        elif self._is_flooded(frame):
            self._on_flooded(frame, from_node)
        elif frame.dst != self.id:
            self._forward(frame, from_node)
        elif frame.msg_type is MessageType.DATA:
            self._accept_data(frame)
        else:
            self._accept_ack(frame)

    def _is_flooded(self, frame: Frame) -> bool:
        if frame.msg_type in _FLOODED_ALWAYS:
            return True
        return (
            frame.msg_type is MessageType.DELIVERY_ACK
            and self._settings.ack_delivery == ACK_FLOODING
        )

    def _forward(self, frame: Frame, from_node: int) -> None:
        """Relay a routed frame addressed to another node.

        TTL is checked first: the forwarder decrements it and the frame is dropped if the
        result would be 0, i.e. a frame arriving with TTL 1 (or 0) is not forwarded. Only then
        is the route looked up. The destination never checks TTL.
        """
        if frame.ttl <= 1:
            self._drop("ttl_expired", frame.identity, ttl=frame.ttl)
            return
        self._route(replace(frame, ttl=frame.ttl - 1), from_node, None)

    # ---- management flooding ----

    def _send_management(self, frame: Frame) -> None:
        """Originate a management frame: flood it to every permitted neighbor.

        The frame's identity is remembered so that a copy coming back is dropped as our own.
        """
        self._flood.seen(frame.identity)
        if frame.dst == self.id:
            self._consume_management(frame)
            return
        for neighbor in sorted(self.permitted):
            self._enqueue(frame, neighbor, None, None)

    def _on_flooded(self, frame: Frame, from_node: int) -> None:
        """Bounded flooding: each node handles a given management frame at most once."""
        if frame.src == self.id:
            self._drop("own_frame", frame.identity, sender=self._name(from_node))
            return
        if self._flood.seen(frame.identity):
            self._drop("flood_duplicate", frame.identity, sender=self._name(from_node))
            return
        if frame.dst == self.id:
            self._consume_management(frame)
            return
        if frame.ttl <= 1:
            self._drop("ttl_expired", frame.identity, ttl=frame.ttl)
            return
        forwarded = replace(frame, ttl=frame.ttl - 1)
        for neighbor in sorted(self.permitted):
            if neighbor != from_node:  # not back to where it came from
                self._enqueue(forwarded, neighbor, from_node, None)

    def _consume_management(self, frame: Frame) -> None:
        kind = frame.msg_type
        if kind in (MessageType.TOPOLOGY_REPORT, MessageType.ROUTE_INSTALL_ACK):
            if self.gateway_sink is not None:
                self.gateway_sink(frame)
            else:
                self._reject("unexpected_type", frame.identity, msg=kind.name)
        elif kind is MessageType.ROUTE_UPDATE:
            self._on_route_update(frame)
        elif kind is MessageType.DELIVERY_ACK:
            self._accept_ack(frame)
        else:  # LOCAL_FAILOVER_REPORT: defined, but nothing handles it before Phase 4b
            self._reject("unexpected_type", frame.identity, msg=kind.name)

    # ---- neighbor discovery (HELLO) ----

    def _hello_tick(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        heard = tuple(
            HelloNeighbor(node, neighbor.session)
            for node, neighbor in sorted(self._neighbors.items())
            if neighbor.state is not NeighborCode.LOST
        )[:MAX_HELLO_NEIGHBORS]
        for neighbor_id in sorted(self.permitted):
            hello = self._new_frame(neighbor_id, HelloPayload(heard), ttl=1)
            self.counters.hellos += 1
            self._enqueue(hello, neighbor_id, None, None)
        jitter = self._settings.hello_jitter_ms
        delay = self._settings.hello_interval_ms + self._jitter_rng.randint(-jitter, jitter)
        self._engine.schedule(max(1, delay), lambda: self._hello_tick(epoch))

    def _on_hello(self, frame: Frame, from_node: int) -> None:
        """Hearing a HELLO proves only that frames from this neighbor reach us.

        The two-way state also needs the neighbor's HELLO to list *us* in our current
        session, which proves our frames reach it.
        """
        if frame.dst != self.id or frame.src != from_node:
            self._reject("hello_not_local", frame.identity)
            return
        if not self._settings.discovery:
            return
        assert isinstance(frame.payload, HelloPayload)
        now = self._engine.now_ms
        heard_me = any(
            n.node == self.id and n.session == self._session for n in frame.payload.neighbors
        )
        new_state = NeighborCode.TWO_WAY if heard_me else NeighborCode.ONE_WAY
        neighbor = self._neighbors.get(frame.src)
        changed = False
        old_state = "unknown"
        if neighbor is None:
            neighbor = self._neighbors[frame.src] = _Neighbor(frame.session, now, new_state)
            changed = True
        else:
            old_state = neighbor.state.name.lower()
            if neighbor.session != frame.session:
                self._trace.record(
                    now,
                    self.name,
                    NEIGHBOR_RESTARTED,
                    None,
                    neighbor=self._name(frame.src),
                    old_session=f"{neighbor.session:08x}",
                    new_session=f"{frame.session:08x}",
                )
                neighbor.session = frame.session
                changed = True
            if neighbor.state is not new_state:
                changed = True
            neighbor.state = new_state
        neighbor.last_heard_ms = now
        if changed:
            self._trace.record(
                now,
                self.name,
                NEIGHBOR_CHANGED,
                None,
                neighbor=self._name(frame.src),
                old=old_state,
                new=new_state.name.lower(),
            )
            self._request_report()
        epoch, source, heard_at = self._epoch, frame.src, now
        self._engine.schedule(
            self._settings.neighbor_timeout_ms,
            lambda: self._neighbor_timeout(epoch, source, heard_at),
        )

    def _neighbor_timeout(self, epoch: int, neighbor_id: int, heard_at: int) -> None:
        neighbor = self._neighbors.get(neighbor_id)
        if epoch != self._epoch or neighbor is None or neighbor.last_heard_ms != heard_at:
            return  # a newer HELLO arrived (or we were restarted)
        if neighbor.state is NeighborCode.LOST:
            return
        old = neighbor.state.name.lower()
        neighbor.state = NeighborCode.LOST
        self._trace.record(
            self._engine.now_ms,
            self.name,
            NEIGHBOR_CHANGED,
            None,
            neighbor=self._name(neighbor_id),
            old=old,
            new="lost",
            silent_ms=self._engine.now_ms - heard_at,
        )
        self._request_report()

    # ---- link probes (round-trip time on this node's own clock) ----

    def _probe_tick(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        now = self._engine.now_ms
        for neighbor_id in sorted(self.permitted):
            probe = self._new_frame(neighbor_id, ProbePayload(), ttl=1)
            # One outstanding probe per neighbor: a new one replaces an unanswered one, so a
            # silent neighbor costs one small record, not a growing list.
            self._probes[neighbor_id] = _Probe(probe.seq, now)
            self.counters.probes += 1
            self._enqueue(probe, neighbor_id, None, None)
        if self._refresh_measurement_flags():  # a measurement aged out: tell the controller
            self._request_report()
        jitter = self._settings.probe_jitter_ms
        delay = self._settings.probe_interval_ms + self._probe_rng.randint(-jitter, jitter)
        self._engine.schedule(max(1, delay), lambda: self._probe_tick(epoch))

    def _on_probe(self, frame: Frame, from_node: int) -> None:
        if frame.dst != self.id or frame.src != from_node:
            self._reject("probe_not_local", frame.identity)
            return
        payload = frame.payload
        if isinstance(payload, ProbePayload):
            reply = self._new_frame(frame.src, ProbeReplyPayload(frame.session, frame.seq), ttl=1)
            self._enqueue(reply, from_node, None, None)
            return
        assert isinstance(payload, ProbeReplyPayload)
        outstanding = self._probes.get(from_node)
        if (
            outstanding is None
            or payload.probe_session != self._session
            or payload.probe_seq != outstanding.seq
        ):
            self._drop("probe_unmatched", frame.identity, sender=self._name(from_node))
            return
        del self._probes[from_node]
        now = self._engine.now_ms
        rtt = min(now - outstanding.sent_ms, RTT_MAX_MS)  # this node's clock, both ends
        window = self._rtt.setdefault(from_node, deque(maxlen=self._settings.rtt_window))
        window.append((now, rtt))
        if self._refresh_measurement_flags():
            self._request_report()

    def _fresh_samples(self, neighbor_id: int) -> list[int]:
        horizon = self._engine.now_ms - self._settings.rtt_max_age_ms
        return [rtt for at, rtt in self._rtt.get(neighbor_id, ()) if at >= horizon]

    def _measured_rtt(self, neighbor_id: int) -> int | None:
        """Median of the recent fresh samples, or None when there are too few.

        None is "unmeasured" and is reported as such; it is never turned into a number.
        """
        samples = sorted(self._fresh_samples(neighbor_id))
        if len(samples) < self._settings.rtt_min_samples:
            return None
        return samples[len(samples) // 2]

    def _refresh_measurement_flags(self) -> bool:
        """Note which neighbors are measured; True if that changed (a report is then due).

        Individual RTT values do not trigger reports, only gaining or losing a measurement.
        """
        changed = False
        for neighbor_id in self.permitted:
            has = self._measured_rtt(neighbor_id) is not None
            if self._rtt_reported.get(neighbor_id, False) != has:
                self._rtt_reported[neighbor_id] = has
                changed = True
        return changed

    # ---- route installation (ROUTE_UPDATE / ROUTE_INSTALL_ACK) ----

    def originate_route_update(self, command: RouteUpdateCommand) -> None:
        """Gateway node only: send the controller's instruction into the network.

        C creates the frame (its own `src`, session and sequence), so nodes can check that a
        ROUTE_UPDATE really came from the gateway. It travels by management flooding, not along
        the data route it is replacing.
        """
        if not self.running:
            return
        frame = self._new_frame(
            command.node,
            RouteUpdatePayload(
                command.epoch,
                command.version,
                command.destination,
                command.next_hop,
                command.backup_next_hop,
            ),
        )
        self._trace.record(
            self._engine.now_ms,
            self.name,
            ROUTE_UPDATE_SENT,
            frame.identity,
            target=self._name(command.node),
            destination=self._name(command.destination),
            next_hop=None if command.next_hop is None else self._name(command.next_hop),
            epoch=command.epoch,
            version=command.version,
        )
        self._send_management(frame)

    def _on_route_update(self, frame: Frame) -> None:
        """Install a controller instruction if it is newer than what this node holds.

        Order of the checks: it must come from the gateway; the next hop must be a permitted
        neighbor; then (epoch, version) must be newer than the held pair. An update that equals
        the held one is acknowledged again (its ACK may have been lost) without re-installing;
        an older one is refused and the ACK carries the held pair so the controller can see it.
        """
        assert isinstance(frame.payload, RouteUpdatePayload)
        update = frame.payload
        if frame.src != self._settings.gateway_id:
            self._reject("not_from_gateway", frame.identity, src=self._name(frame.src))
            return
        held = self._installed.get(update.destination)
        status = RouteAckStatus.INSTALLED
        report_epoch, report_version = update.epoch, update.version
        if held is not None and (held.epoch, held.version) == (update.epoch, update.version):
            self._trace.record(
                self._engine.now_ms,
                self.name,
                ROUTE_UPDATE_IGNORED,
                frame.identity,
                reason="duplicate",
                epoch=update.epoch,
                version=update.version,
            )
        elif held is not None and not self._is_newer(update.epoch, update.version, held):
            status = RouteAckStatus.STALE
            report_epoch, report_version = held.epoch, held.version
            self._trace.record(
                self._engine.now_ms,
                self.name,
                ROUTE_UPDATE_IGNORED,
                frame.identity,
                reason="stale",
                epoch=update.epoch,
                version=update.version,
                held_epoch=held.epoch,
                held_version=held.version,
            )
        elif update.next_hop is not None and update.next_hop not in self.permitted:
            status = RouteAckStatus.REJECTED
            self._trace.record(
                self._engine.now_ms,
                self.name,
                ROUTE_UPDATE_IGNORED,
                frame.identity,
                reason="next_hop_not_permitted",
                epoch=update.epoch,
                version=update.version,
            )
        elif held is None and len(self._installed) >= self._settings.max_routes:
            status = RouteAckStatus.REJECTED
            self._trace.record(
                self._engine.now_ms,
                self.name,
                ROUTE_UPDATE_IGNORED,
                frame.identity,
                reason="route_table_full",
                epoch=update.epoch,
                version=update.version,
            )
        else:
            self._installed[update.destination] = _Installed(
                update.epoch, update.version, update.next_hop, update.backup_next_hop
            )
            self._trace.record(
                self._engine.now_ms,
                self.name,
                ROUTE_INSTALLED,
                frame.identity,
                destination=self._name(update.destination),
                next_hop=None if update.next_hop is None else self._name(update.next_hop),
                epoch=update.epoch,
                version=update.version,
            )
        ack = self._new_frame(
            self._settings.gateway_id,
            RouteInstallAckPayload(report_epoch, report_version, update.destination, status),
        )
        self._send_management(ack)

    @staticmethod
    def _is_newer(epoch: int, version: int, held: _Installed) -> bool:
        """A newer epoch wins outright; within one epoch the higher version wins."""
        if epoch != held.epoch:
            return _newer(epoch, held.epoch)
        return _newer(version, held.version)

    # ---- topology reports ----

    def _report_tick(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        self._send_report()
        self._engine.schedule(self._settings.report_interval_ms, lambda: self._report_tick(epoch))

    def _request_report(self) -> None:
        """Ask for a report soon; several changes within the coalesce time share one."""
        if self._report_pending or not self.running:
            return
        self._report_pending = True
        epoch = self._epoch
        self._engine.schedule(self._settings.report_coalesce_ms, lambda: self._flush_report(epoch))

    def _flush_report(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        self._report_pending = False
        self._send_report()

    def _send_report(self) -> None:
        now = self._engine.now_ms
        entries = tuple(
            ReportEntry(
                node,
                neighbor.state,
                neighbor.session,
                min(now - neighbor.last_heard_ms, AGE_MAX_MS),
                self._measured_rtt(node),
            )
            for node, neighbor in sorted(self._neighbors.items())
        )[:MAX_REPORT_ENTRIES]
        report = self._new_frame(self._settings.gateway_id, TopologyReportPayload(entries))
        self.counters.reports += 1
        self._send_management(report)

    # ---- destination behaviour ----

    def _accept_data(self, frame: Frame) -> None:
        assert isinstance(frame.payload, DataPayload)
        identity = frame.identity
        verdict = self._dedup.check(frame.src, frame.session, frame.seq)
        if verdict is Verdict.TOO_OLD:
            self._reject("stale_sequence", identity)
            return
        if verdict is Verdict.NEW:
            self.counters.delivered_unique += 1
            self.receipts.append((identity, frame.payload.value, self._engine.now_ms))
            self._trace.record(
                self._engine.now_ms,
                self.name,
                READING_DELIVERED,
                identity,
                value=frame.payload.value,
            )
        else:
            # Counted once already. Acknowledge again: the earlier ACK may have been lost, and
            # without a fresh one the source would keep retrying.
            self.counters.duplicates += 1
            self._trace.record(
                self._engine.now_ms, self.name, DUPLICATE_RECEIVED, identity, ack="resent"
            )
        ack = self._new_frame(
            frame.src, DeliveryAckPayload(identity.src, identity.session, identity.seq)
        )
        if self._settings.ack_delivery == ACK_FLOODING:
            self._send_management(ack)
        else:
            self._route(ack, None, None)

    # ---- source behaviour ----

    def _create_reading(self, epoch: int) -> None:
        if epoch != self._epoch or self._reading_dst is None:
            return
        self.counters.created += 1
        if self._lookup(self._reading_dst) is None:
            # No route (never installed, or the controller said there is none): drop now rather
            # than queue or retry. Counted, and nothing grows.
            self.counters.dropped_no_route += 1
            self.drops["no_route"] += 1
            told = self._reading_dst in self._installed
            self._trace.record(
                self._engine.now_ms,
                self.name,
                FRAME_DROPPED,
                None,
                reason="no_route",
                dst=self._name(self._reading_dst),
                reading="not_sent",
                told_by_controller=told,
            )
            self._engine.schedule(
                self._settings.reading_interval_ms, lambda: self._create_reading(epoch)
            )
            return
        value = 2000 + self._rng.randint(-50, 50)
        frame = self._new_frame(self._reading_dst, DataPayload(value))
        if len(self._pending) >= self._settings.max_pending:
            oldest = next(iter(self._pending))
            del self._pending[oldest]
            self.counters.lost += 1
            self._trace.record(
                self._engine.now_ms, self.name, READING_LOST, oldest, reason="pending_overflow"
            )
        self._pending[frame.identity] = _Pending(frame, first_attempt_ms=self._engine.now_ms)
        self._attempt(frame.identity, epoch)
        self._engine.schedule(
            self._settings.reading_interval_ms, lambda: self._create_reading(epoch)
        )

    def _attempt(self, identity: PacketIdentity, epoch: int) -> None:
        """Send (or re-send) a pending reading. A retry reuses the very same frame."""
        pending = self._pending[identity]
        pending.attempts += 1
        attempt = pending.attempts
        self.counters.attempts += 1
        self._route(pending.frame, None, attempt)
        self._engine.schedule(
            self._settings.ack_timeout_ms, lambda: self._timed_out(epoch, identity, attempt)
        )

    def _timed_out(self, epoch: int, identity: PacketIdentity, attempt: int) -> None:
        pending = self._pending.get(identity)
        if epoch != self._epoch or pending is None or pending.attempts != attempt:
            return  # stopped since, already acknowledged, or superseded by a newer attempt
        self._trace.record(self._engine.now_ms, self.name, ACK_TIMEOUT, identity, attempt=attempt)
        if attempt < self._settings.max_attempts:
            self._attempt(identity, epoch)
            return
        del self._pending[identity]
        self.counters.lost += 1
        self._trace.record(self._engine.now_ms, self.name, READING_LOST, identity, attempts=attempt)

    def _accept_ack(self, frame: Frame) -> None:
        if frame.msg_type is not MessageType.DELIVERY_ACK:
            self._reject("unexpected_type", frame.identity, msg=frame.msg_type.name)
            return
        assert isinstance(frame.payload, DeliveryAckPayload)
        acked = frame.payload.acked
        pending = self._pending.pop(acked, None)
        if pending is None:
            # Late (after the last attempt), already acknowledged by an earlier copy, or from a
            # previous session of this node.
            self._trace.record(
                self._engine.now_ms,
                self.name,
                ACK_IGNORED,
                acked,
                reason="not_pending",
                acked_by=frame.src,
            )
            return
        self.counters.acknowledged += 1
        self._trace.record(
            self._engine.now_ms,
            self.name,
            ACK_RECEIVED,
            acked,
            # From the FIRST attempt to the ACK: time to confirmed delivery, not an RTT.
            delivery_time_ms=self._engine.now_ms - pending.first_attempt_ms,
            attempts=pending.attempts,
            acked_by=frame.src,
        )
