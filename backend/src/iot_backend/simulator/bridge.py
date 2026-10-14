"""Node C's side of the laptop link: heartbeat watching, a bounded buffer, and replay.

This models firmware that will run on C (not the controller). It sits between the radio side of
C and the serial line to the laptop:

* The laptop sends a heartbeat every second. If none arrives for `controller_timeout_ms`
  (default 3 s), C treats the controller as **unreachable**. C keeps working on the radio exactly
  as before (it still forwards, acknowledges readings and answers HELLOs): this is
  *fail-static*. Only the stream going up to the laptop changes.
* While unreachable, what C would have sent up is stored in a **bounded ring buffer**.
* When heartbeats resume, C announces a replay, hands the buffer up in order between explicit
  start and end markers, and then goes back to passing things straight through.

What goes into the buffer, and what is dropped first
----------------------------------------------------
Capacity is fixed (default 50 entries). Not all entries are equal:

* **Readings** are the data the project exists to deliver, so they are preferred.
* **Topology reports, route acknowledgements and telemetry** are *coalesced*: a newer one from
  the same origin supersedes the older, so each origin occupies one slot however long the outage.
* **Local-failover reports** are always kept (one slot per distinct failover; the node repeats
  them and the repeats collapse).
* Entries C already sent up before it noticed the outage are kept for a short while (back to the
  second-to-last heartbeat) because the laptop may not have been listening; they are replayed and
  the laptop counts each reading once by its identity. They are the first thing evicted.

When the buffer is full the least valuable, oldest entry is evicted and counted: already-sent
entries, then reports/acks/telemetry, then readings, then failover reports.

Ages, not timestamps: C has no wall clock. Each entry remembers when it arrived on C's clock and is
replayed with `age_ms = now - arrived`. The laptop subtracts that from its own clock.

RAM on an ESP8266 (estimate, not measured): an entry is a reading (origin 1, session 4, seq 4,
value 4, via 1, ttl 1, flags 1, arrival 4: about 20 bytes) or a report (up to 8 neighbor
observations of 8 bytes plus a header: about 80 bytes). 50 entries at the 80-byte worst case is
4 KB; a buffer that holds 40 readings and 10 coalesced slots is about 1.5 KB. The buffer is
RAM-only: it is lost if C loses power, and C is powered through the same USB cable that
connects it to the laptop, so unplugging that cable is not a "controller loss" test.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from iot_backend.simulator.engine import Engine
from iot_backend.transports import (
    GatewayStatusEvent,
    LocalFailoverEvent,
    ReadingEvent,
    ReplayEndEvent,
    ReplayStartEvent,
    RouteInstallAckEvent,
    TelemetryEvent,
    TopologyReportEvent,
)

READING, REPORT, ACK, TELEMETRY, FAILOVER = "reading", "report", "ack", "telemetry", "failover"

# Order in which entries are given up when the buffer is full (lowest rank first).
_EVICT_ORDER = {"sent": 0, ACK: 1, TELEMETRY: 1, REPORT: 1, READING: 2, FAILOVER: 3}


@dataclass(frozen=True)
class BridgeConfig:
    controller_timeout_ms: int = 0  # 0 = no heartbeat watching: everything passes straight up
    buffer_capacity: int = 50
    replay_spacing_ms: int = 20  # time per replayed entry (the serial line's throughput)


@dataclass
class _Entry:
    kind: str
    key: tuple[Any, ...]
    event: Any
    arrived_ms: int  # C's own clock
    sent: bool  # already handed up while C believed the controller was reachable


@dataclass
class BridgeCounters:
    outages: int = 0
    buffered: int = 0  # entries put in the buffer while unreachable
    dropped_overflow: int = 0  # held entries lost because the buffer was full
    coalesced: int = 0  # held entries superseded by a newer one
    replayed: int = 0  # entries handed up in replays
    replays: int = 0


def _rank(entry: "_Entry") -> int:
    """How much an entry is worth keeping: lower is given up first."""
    return _EVICT_ORDER["sent" if entry.sent else entry.kind]


def _classify(event: Any) -> tuple[str, tuple[Any, ...]]:
    if isinstance(event, ReadingEvent):
        return READING, (READING, event.origin, event.session, event.seq)
    if isinstance(event, TopologyReportEvent):
        return REPORT, (REPORT, event.origin)
    if isinstance(event, RouteInstallAckEvent):
        return ACK, (ACK, event.origin)
    if isinstance(event, TelemetryEvent):
        return TELEMETRY, (TELEMETRY, event.origin)
    if isinstance(event, LocalFailoverEvent):
        return FAILOVER, (
            FAILOVER,
            event.origin,
            event.epoch,
            event.version,
            event.from_next_hop,
            event.to_next_hop,
        )
    raise TypeError(f"not an up event: {type(event).__name__}")


class CBridge:
    def __init__(self, engine: Engine, config: BridgeConfig, emit: Callable[[Any], None]) -> None:
        self._engine = engine
        self.config = config
        self._emit = emit  # hands one message to the serial line (age is on the event)
        self._buffer: deque[_Entry] = deque()
        self.counters = BridgeCounters()
        self.reachable = config.controller_timeout_ms == 0  # unknown until the first heartbeat
        self._last_hb: int | None = None
        self._prev_hb: int | None = None
        self._outage_started: int | None = None
        self._outage_dropped = 0
        self._outage_coalesced = 0
        self._replaying = False
        self._epoch = 0  # invalidates timers when a replay is superseded

    # ---- introspection (the simulator's tests and CLI; the controller never sees this) ----

    @property
    def held(self) -> int:
        return sum(1 for e in self._buffer if not e.sent)

    @property
    def size(self) -> int:
        return len(self._buffer)

    @property
    def replaying(self) -> bool:
        return self._replaying

    def slots(self) -> list[tuple[str, tuple[Any, ...], bool]]:
        return [(e.kind, e.key, e.sent) for e in self._buffer]

    # ---- from the radio side ----

    def up(self, event: Any) -> None:
        """Something C received that the controller should see."""
        if self.config.controller_timeout_ms == 0:
            self._emit(event)  # no heartbeat watching: the Phase 5a behaviour
            return
        kind, key = _classify(event)
        now = self._engine.now_ms
        live = self.reachable and not self._replaying
        entry = _Entry(kind, key, event, now, sent=live)
        self._store(entry)
        if live:
            self._emit(event)
        elif not self.reachable:
            self.counters.buffered += 1

    def _store(self, entry: _Entry) -> None:
        for existing in self._buffer:
            if existing.key == entry.key:
                if entry.kind == FAILOVER and not existing.sent:
                    # A node repeats a failover report; keep the first copy, which carries the
                    # time the failover really happened (its age), and drop the repeat.
                    self.counters.coalesced += 1
                    self._outage_coalesced += 1
                    return
                self._buffer.remove(existing)
                if not existing.sent:
                    self.counters.coalesced += 1
                    self._outage_coalesced += 1
                break
        while len(self._buffer) >= max(1, self.config.buffer_capacity):
            victim = min(
                enumerate(self._buffer),
                key=lambda item: (
                    _rank(item[1]),
                    item[0],  # oldest first within a rank
                ),
            )[1]
            if _rank(entry) < _rank(victim):
                # The newcomer is worth less than everything already held (a report arriving
                # at a buffer full of readings): it is the one given up.
                if not entry.sent:
                    self.counters.dropped_overflow += 1
                    self._outage_dropped += 1
                return
            self._buffer.remove(victim)
            if not victim.sent:
                self.counters.dropped_overflow += 1
                self._outage_dropped += 1
        self._buffer.append(entry)

    # ---- from the laptop side ----

    def heartbeat(self) -> None:
        if self.config.controller_timeout_ms == 0:
            # Not watching heartbeats, but still answering them so the laptop can see C.
            self._emit(
                GatewayStatusEvent(
                    received_ms=self._engine.now_ms,
                    controller_reachable=True,
                    held=0,
                    capacity=0,
                    dropped_overflow=0,
                    coalesced=0,
                    replayed=0,
                    outages=0,
                )
            )
            return
        now = self._engine.now_ms
        self._prev_hb, self._last_hb = self._last_hb, now
        # Entries older than the second-to-last heartbeat were read by the laptop in order.
        if self._prev_hb is not None:
            for entry in [e for e in self._buffer if e.sent and e.arrived_ms < self._prev_hb]:
                self._buffer.remove(entry)
        was_reachable = self.reachable
        self.reachable = True
        stamp = now
        self._engine.schedule(self.config.controller_timeout_ms, lambda: self._check_timeout(stamp))
        self._emit(
            GatewayStatusEvent(
                received_ms=now,
                controller_reachable=True,
                held=self.held,
                capacity=self.config.buffer_capacity,
                dropped_overflow=self.counters.dropped_overflow,
                coalesced=self.counters.coalesced,
                replayed=self.counters.replayed,
                outages=self.counters.outages,
            )
        )
        if not was_reachable and self._buffer and not self._replaying:
            self._start_replay()

    def _check_timeout(self, stamp: int) -> None:
        if self._last_hb != stamp or not self.reachable:
            return  # a newer heartbeat arrived
        self.reachable = False
        self._epoch += 1  # a replay in progress stops; its remaining entries stay held
        if self._replaying:
            self._replaying = False
        self.counters.outages += 1
        self._outage_started = self._engine.now_ms - self.config.controller_timeout_ms
        self._outage_dropped = 0
        self._outage_coalesced = 0

    # ---- replay ----

    def _start_replay(self) -> None:
        now = self._engine.now_ms
        self._replaying = True
        self.counters.replays += 1
        epoch = self._epoch
        started = self._outage_started if self._outage_started is not None else now
        self._emit(
            ReplayStartEvent(
                received_ms=now,
                entries=len(self._buffer),
                held=self.held,
                dropped_overflow=self._outage_dropped,
                coalesced=self._outage_coalesced,
                outage_ms=max(0, now - started),
            )
        )
        replayed_before = self.counters.replayed
        self._engine.schedule(
            self.config.replay_spacing_ms, lambda: self._replay_step(epoch, replayed_before)
        )

    def _replay_step(self, epoch: int, replayed_before: int) -> None:
        if epoch != self._epoch or not self._replaying:
            return
        if not self._buffer:
            self._replaying = False
            self._emit(
                ReplayEndEvent(
                    received_ms=self._engine.now_ms,
                    replayed=self.counters.replayed - replayed_before,
                    dropped_overflow=self._outage_dropped,
                    coalesced=self._outage_coalesced,
                )
            )
            return
        entry = self._buffer.popleft()
        age = max(0, self._engine.now_ms - entry.arrived_ms)
        self.counters.replayed += 1
        self._emit(replace(entry.event, age_ms=age, replayed=True))
        self._engine.schedule(
            self.config.replay_spacing_ms, lambda: self._replay_step(epoch, replayed_before)
        )
