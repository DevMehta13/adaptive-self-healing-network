"""Virtual nodes. They talk only in encoded bytes, as the real boards will.

Phase 1 behaviour: A creates numbered readings and tracks whether each one is
acknowledged; B validates DATA, records the receipt, and answers with DELIVERY_ACK.
No retries, forwarding or duplicate suppression yet (Phase 2).
"""

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from iot_backend.protocol import (
    DEFAULT_TTL,
    DataPayload,
    DecodeError,
    DeliveryAckPayload,
    Frame,
    MessageType,
    PacketIdentity,
    decode,
    encode,
)
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.trace import (
    ACK_IGNORED,
    ACK_RECEIVED,
    ACK_TIMEOUT,
    FRAME_RECEIVED,
    FRAME_REJECTED,
    FRAME_SENT,
    NODE_BOOTED,
    READING_DELIVERED,
    Trace,
)

# send(from_id, to_id, data, identity): hand encoded bytes to the link towards `to_id`.
SendFunction = Callable[[int, int, bytes, PacketIdentity], None]


@dataclass
class NodeCounters:
    boots: int = 0
    sent: int = 0
    delivered: int = 0
    acknowledged: int = 0
    unacknowledged: int = 0
    rejected: int = 0


class _Node:
    def __init__(
        self,
        node_id: int,
        name: str,
        engine: Engine,
        trace: Trace,
        send: SendFunction,
        seed: int,
    ) -> None:
        self.id = node_id
        self.name = name
        self._engine = engine
        self._trace = trace
        self._send = send
        self._rng = random.Random(f"{seed}:node:{name}")
        self._epoch = 0  # incremented per boot so timers from an old boot do nothing
        self._session = 0
        self._next_seq = 1
        self.counters = NodeCounters()

    def boot(self) -> None:
        """Start (or restart) the node: new random session, sequence numbers start again."""
        previous = self._session
        while True:
            session = self._rng.randrange(1, 2**32)
            if session != previous:
                break
        self._epoch += 1
        self._session = session
        self._next_seq = 1
        self.counters.boots += 1
        self._on_boot()
        self._trace.record(
            self._engine.now_ms,
            self.name,
            NODE_BOOTED,
            None,
            session=f"{session:08x}",
            boot=self.counters.boots,
        )

    def _on_boot(self) -> None:
        """Hook: reset per-boot state."""

    def _new_frame(self, dst: int, ttl: int, payload: DataPayload | DeliveryAckPayload) -> Frame:
        frame = Frame(self.id, dst, self._session, self._next_seq, ttl, payload)
        self._next_seq += 1
        return frame

    def _transmit(self, frame: Frame, **info: object) -> None:
        data = encode(frame)
        self.counters.sent += 1
        self._trace.record(
            self._engine.now_ms,
            self.name,
            FRAME_SENT,
            frame.identity,
            msg=frame.msg_type.name,
            dst=frame.dst,
            size=len(data),
            **info,
        )
        self._send(self.id, frame.dst, data, frame.identity)

    def receive(self, data: bytes) -> None:
        """A frame's bytes arrived at this node's radio."""
        now = self._engine.now_ms
        self._trace.record(now, self.name, FRAME_RECEIVED, None, size=len(data))
        try:
            frame = decode(data)
        except DecodeError as error:
            self._reject(None, error.reason.value, size=len(data))
            return
        if frame.dst != self.id:
            self._reject(frame.identity, "wrong_destination", dst=frame.dst)
            return
        self._handle(frame)

    def _reject(self, identity: PacketIdentity | None, reason: str, **info: object) -> None:
        self.counters.rejected += 1
        self._trace.record(
            self._engine.now_ms, self.name, FRAME_REJECTED, identity, reason=reason, **info
        )

    def _handle(self, frame: Frame) -> None:
        raise NotImplementedError


class SourceNode(_Node):
    """Node A: creates a numbered reading every interval and waits for its acknowledgement."""

    def __init__(
        self,
        node_id: int,
        name: str,
        engine: Engine,
        trace: Trace,
        send: SendFunction,
        seed: int,
        *,
        dst: int,
        reading_interval_ms: int,
        ack_timeout_ms: int,
        ttl: int = DEFAULT_TTL,
        max_pending: int = 32,
    ) -> None:
        super().__init__(node_id, name, engine, trace, send, seed)
        self._dst = dst
        self._interval_ms = reading_interval_ms
        self._ack_timeout_ms = ack_timeout_ms
        self._ttl = ttl
        self._max_pending = max_pending
        # identity -> time sent. Insertion-ordered and capped by max_pending.
        self._pending: dict[PacketIdentity, int] = {}
        self.last_discarded_pending = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _on_boot(self) -> None:
        # RAM is lost on restart: readings still waiting for an ACK are forgotten.
        self.last_discarded_pending = len(self._pending)
        self._pending.clear()
        epoch = self._epoch
        self._engine.schedule(self._interval_ms, lambda: self._create_reading(epoch))

    def _create_reading(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        value = 2000 + self._rng.randint(-50, 50)
        frame = self._new_frame(self._dst, self._ttl, DataPayload(value))
        if len(self._pending) >= self._max_pending:
            oldest = next(iter(self._pending))
            del self._pending[oldest]
            self.counters.unacknowledged += 1
            self._trace.record(
                self._engine.now_ms, self.name, ACK_TIMEOUT, oldest, reason="pending_overflow"
            )
        self._pending[frame.identity] = self._engine.now_ms
        self._transmit(frame, value=value)
        identity = frame.identity
        self._engine.schedule(self._ack_timeout_ms, lambda: self._ack_timed_out(epoch, identity))
        self._engine.schedule(self._interval_ms, lambda: self._create_reading(epoch))

    def _ack_timed_out(self, epoch: int, identity: PacketIdentity) -> None:
        if epoch != self._epoch or identity not in self._pending:
            return
        del self._pending[identity]
        self.counters.unacknowledged += 1
        self._trace.record(
            self._engine.now_ms, self.name, ACK_TIMEOUT, identity, reason="no_ack_in_time"
        )

    def _handle(self, frame: Frame) -> None:
        if frame.msg_type is not MessageType.DELIVERY_ACK:
            self._reject(frame.identity, "unexpected_type", msg=frame.msg_type.name)
            return
        assert isinstance(frame.payload, DeliveryAckPayload)
        acked = frame.payload.acked
        sent_at = self._pending.pop(acked, None)
        if sent_at is None:
            # Late (after the timeout), unknown, or from a previous session of this node.
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
            rtt_ms=self._engine.now_ms - sent_at,
            acked_by=frame.src,
        )


class ReceiverNode(_Node):
    """Node B: accepts valid DATA addressed to it, records the receipt, sends DELIVERY_ACK."""

    def __init__(
        self,
        node_id: int,
        name: str,
        engine: Engine,
        trace: Trace,
        send: SendFunction,
        seed: int,
        *,
        max_receipts: int = 1024,
    ) -> None:
        super().__init__(node_id, name, engine, trace, send, seed)
        # (identity, value, time) of every accepted reading; bounded, oldest dropped.
        self.receipts: deque[tuple[PacketIdentity, int, int]] = deque(maxlen=max_receipts)

    def _handle(self, frame: Frame) -> None:
        if frame.msg_type is not MessageType.DATA:
            self._reject(frame.identity, "unexpected_type", msg=frame.msg_type.name)
            return
        assert isinstance(frame.payload, DataPayload)
        self.receipts.append((frame.identity, frame.payload.value, self._engine.now_ms))
        self.counters.delivered += 1
        self._trace.record(
            self._engine.now_ms,
            self.name,
            READING_DELIVERED,
            frame.identity,
            value=frame.payload.value,
        )
        acked = frame.identity
        ack = self._new_frame(
            frame.src, DEFAULT_TTL, DeliveryAckPayload(acked.src, acked.session, acked.seq)
        )
        self._transmit(ack, acks=acked.seq)
