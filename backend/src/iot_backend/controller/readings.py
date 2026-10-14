"""Counting readings once, however many times they arrive.

A reading can reach the controller more than once: C replays entries it had already sent up
(because the laptop may not have been listening), and A's own retries can reach C twice (C flags
those). The ledger keys every reading by its identity `(origin, session, seq)` and counts it once.
Memory is bounded: a window of recent identities per session, and a few sessions.
"""

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from iot_backend.transports import Gateway, GatewayEvent, ReadingEvent


@dataclass(frozen=True)
class ReadingRecord:
    """One reading, counted once."""

    origin: int
    session: int
    seq: int
    value: int
    via: int
    hops: int
    ttl: int
    received_ms: int  # laptop clock, estimated from C's age when replayed
    replayed: bool
    delay_ms: int  # how long C held it (0 for a live reading)


class ReadingLedger:
    def __init__(self, gateway: Gateway, max_sessions: int = 8, window: int = 1024) -> None:
        self._max_sessions = max_sessions
        self._window = window
        self._seen: OrderedDict[tuple[int, int], tuple[set[int], deque[int]]] = OrderedDict()
        self._handlers: list[Callable[[ReadingRecord], None]] = []
        self.unique = 0
        self.unique_replayed = 0  # of those, first seen in a replay (delivered late)
        self.repeats_from_c = 0  # C flagged them: A's retry reached C again
        self.repeats_seen_again = 0  # arrived again at the laptop (replay of a sent entry)
        gateway.subscribe(self._on_event)

    def subscribe(self, handler: Callable[[ReadingRecord], None]) -> None:
        """Call `handler` once per *new* reading, in arrival order."""
        self._handlers.append(handler)

    def _first_time(self, origin: int, session: int, seq: int) -> bool:
        key = (origin, session)
        if key in self._seen:
            self._seen.move_to_end(key)
        else:
            self._seen[key] = (set(), deque())
            while len(self._seen) > self._max_sessions:
                self._seen.popitem(last=False)
        seen, order = self._seen[key]
        if seq in seen:
            return False
        seen.add(seq)
        order.append(seq)
        while len(order) > self._window:
            seen.discard(order.popleft())
        return True

    def _on_event(self, event: GatewayEvent) -> None:
        if not isinstance(event, ReadingEvent):
            return
        if event.duplicate:
            self.repeats_from_c += 1
            return
        if not self._first_time(event.origin, event.session, event.seq):
            self.repeats_seen_again += 1
            return
        self.unique += 1
        if event.replayed:
            self.unique_replayed += 1
        record = ReadingRecord(
            origin=event.origin,
            session=event.session,
            seq=event.seq,
            value=event.value,
            via=event.via,
            hops=event.hops,
            ttl=event.ttl,
            received_ms=event.received_ms,
            replayed=event.replayed,
            delay_ms=event.age_ms,
        )
        for handler in self._handlers:
            handler(record)
