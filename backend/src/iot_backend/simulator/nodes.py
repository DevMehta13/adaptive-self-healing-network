"""Virtual nodes. They talk only in encoded bytes, as the real boards will.

Every node runs the same logic; what differs is configuration:

* a **permitted-neighbor list** (the logical topology): the node refuses to send to, and
  rejects frames received from, any other node, even though all boards can physically hear
  each other;
* a **routing table** (destination -> next hop), set by hand in Phase 2;
* optionally a **reading destination**: the node then creates numbered readings and retries
  them end to end until acknowledged or out of attempts.

A node with a frame whose destination is another node forwards it. A node that is the
destination accepts DATA (with bounded duplicate detection) and answers with DELIVERY_ACK.
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
    MessageType,
    PacketIdentity,
    decode,
    encode,
)
from iot_backend.simulator.dedup import DuplicateFilter, Verdict
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
    NODE_BOOTED,
    NODE_STOPPED,
    READING_DELIVERED,
    READING_LOST,
    Trace,
)

# transmit(from_id, to_id, data, identity): put encoded bytes on the air towards `to_id`.
TransmitFunction = Callable[[int, int, bytes, PacketIdentity], None]


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
        self.id = node_id
        self.name = NODE_NAMES.get(node_id, str(node_id))
        self._engine = engine
        self._trace = trace
        self._transmit_bytes = transmit
        self._rng = random.Random(f"{seed}:node:{self.name}")
        self.permitted = set(permitted)
        self.routes = dict(routes)  # configuration: survives a stop/start
        self._settings = settings
        self._reading_dst = reading_dst
        self._initial_seq = initial_seq

        self.running = False
        self._epoch = 0  # incremented on boot and stop so timers from an earlier life do nothing
        self._session = 0
        self._next_seq = initial_seq
        self._pending: dict[PacketIdentity, _Pending] = {}
        self._queue: deque[_Outgoing] = deque()
        self._busy = False
        self._dedup = DuplicateFilter(settings.dedup_window, settings.dedup_sessions)
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
        self.counters.boots += 1
        self._trace.record(
            self._engine.now_ms,
            self.name,
            NODE_BOOTED,
            None,
            session=f"{session:08x}",
            boot=self.counters.boots,
        )
        if self._reading_dst is not None:
            epoch = self._epoch
            self._engine.schedule(
                self._settings.reading_interval_ms, lambda: self._create_reading(epoch)
            )

    def stop(self) -> None:
        """Power off: RAM is lost, incoming frames are ignored, pending timers do nothing."""
        if not self.running:
            return
        self._epoch += 1
        self.running = False
        self._pending.clear()  # unresolved: neither acknowledged nor lost
        self._queue.clear()
        self._busy = False
        self._trace.record(self._engine.now_ms, self.name, NODE_STOPPED)

    # ---- sending ----

    def _new_frame(self, dst: int, payload: DataPayload | DeliveryAckPayload) -> Frame:
        frame = Frame(self.id, dst, self._session, self._next_seq, self._settings.ttl, payload)
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

    def _route(self, frame: Frame, previous_hop: int | None, attempt: int | None) -> None:
        """Pick the next hop from the routing table and queue the frame for transmission."""
        next_hop = self.routes.get(frame.dst)
        if next_hop is None:
            self._drop("no_route", frame.identity, dst=self._name(frame.dst))
            return
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
        self._transmit_bytes(self.id, item.next_hop, data, item.frame.identity)
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
        if frame.dst != self.id:
            self._forward(frame, from_node)
        elif frame.msg_type is MessageType.DATA:
            self._accept_data(frame)
        else:
            self._accept_ack(frame)

    def _forward(self, frame: Frame, from_node: int) -> None:
        """Relay a frame addressed to another node.

        TTL is checked first: the forwarder decrements it and the frame is dropped if the
        result would be 0, i.e. a frame arriving with TTL 1 (or 0) is not forwarded. Only then
        is the route looked up. The destination never checks TTL.
        """
        if frame.ttl <= 1:
            self._drop("ttl_expired", frame.identity, ttl=frame.ttl)
            return
        self._route(replace(frame, ttl=frame.ttl - 1), from_node, None)

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
        self._route(ack, None, None)

    # ---- source behaviour ----

    def _create_reading(self, epoch: int) -> None:
        if epoch != self._epoch or self._reading_dst is None:
            return
        self.counters.created += 1
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
            delivery_time_ms=self._engine.now_ms - pending.first_attempt_ms,
            attempts=pending.attempts,
            acked_by=frame.src,
        )
