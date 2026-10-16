"""Node C's side of the laptop link: heartbeat watching, a bounded buffer, replay, and a
reliable uplink.

This models firmware that will run on C (not the controller). It sits between the radio side of
C and the serial line to the laptop, and speaks the wire messages of `transports/wire.py`:

* The laptop sends a heartbeat every second. If none arrives for `controller_timeout_ms`
  (default 3 s), C treats the controller as **unreachable**. C keeps working on the radio exactly
  as before (it still forwards, acknowledges readings and answers HELLOs): this is
  *fail-static*. Only the stream going up to the laptop changes.
* While unreachable, what C would have sent up is stored in a **bounded buffer**.
* When heartbeats resume, C announces a replay, hands the buffer up in order between explicit
  start and end markers, and then goes back to passing things straight through.

The reliable uplink (Phase 6-prep)
----------------------------------
Every frame C hands up gets a number `n` (1, 2, 3, ... in this boot of C) when it is first
sent, and stays in the buffer until the laptop acknowledges it. The laptop's acknowledgement is
cumulative ("I have everything up to n", in each heartbeat and after every few frames), as in
TCP. C keeps at most `window` numbered entries unacknowledged; beyond that it holds new ones
unsent, so a laptop that has stopped acknowledging cannot make C's memory grow. If the laptop
sees a gap it asks C to resend from the missing number. If C no longer holds some of those
numbers (a long outage filled the buffer and they were given up) it answers which numbers are
gone and what each was (a reading or not), so the laptop counts the loss exactly.

This closes the Phase 5b gap: an outage too short for C's timeout used to lose what C sent
during it, because C thought it had delivered it. Now nothing is forgotten until acknowledged.

What goes into the buffer, and what is dropped first
----------------------------------------------------
Capacity is fixed (default 50 entries). Entries waiting to be sent for the first time are
subject to the Phase 5b rules:

* **Readings** are the data the project exists to deliver, so they are preferred.
* **Topology reports, route acknowledgements and telemetry** are *coalesced*: a newer one from
  the same origin supersedes an older one still waiting, so each origin occupies one slot.
* **Local-failover reports** are always kept (a repeated report keeps the first copy, whose age
  tells when the failover happened).

When the buffer is full the least valuable, oldest entry is evicted and counted: numbered
entries (already sent once, awaiting acknowledgement: the laptop probably has them), then
reports/acks/telemetry, then readings, then failover reports (never). Numbered entries are
created only while the controller is reachable, so at most a window's worth can be evicted in
an outage, and C remembers their kinds until acknowledged.

Ages, not timestamps: C has no wall clock. Each entry remembers when it arrived on C's clock and is
sent with `age_ms = now - arrived`. The laptop subtracts that from its own clock.

RAM on an ESP8266 (estimate, not measured): an entry is the frame as received plus a few fields
(arrival time 4, number 4, via 1, flags 1). A reading frame is 18 bytes, so about 28 bytes; a
topology report up to 104 bytes, so about 114. 50 entries at the worst case is about 5.7 KB; a
buffer that holds 40 readings and 10 coalesced slots is about 2.3 KB. The buffer is RAM-only: it
is lost if C loses power, and C is powered through the same USB cable that connects it to the
laptop, so unplugging that cable is not a "controller loss" test.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from iot_backend.protocol import Frame, MessageType, encode
from iot_backend.simulator.engine import Engine
from iot_backend.transports.serial_format import MAX_GONE_RANGE
from iot_backend.transports.wire import (
    AckDown,
    DownMessage,
    FrameUp,
    GoneUp,
    HeartbeatDown,
    HelloUp,
    ReplayEndUp,
    ReplayStartUp,
    ResendDown,
    StatusUp,
    TransmitDown,
    UpMessage,
)

READING, REPORT, ACK, TELEMETRY, FAILOVER = "reading", "report", "ack", "telemetry", "failover"

# Order in which waiting entries are given up when the buffer is full (lowest rank first).
_EVICT_ORDER = {ACK: 1, TELEMETRY: 1, REPORT: 1, READING: 2, FAILOVER: 3}

Originate = Callable[[int, int, bytes], str | None]  # (dst, type, payload) -> refusal or None


@dataclass(frozen=True)
class BridgeConfig:
    controller_timeout_ms: int = 0  # 0 = no heartbeat watching: frames pass straight up
    buffer_capacity: int = 50
    replay_spacing_ms: int = 20  # time per message sent from the buffer (serial throughput)
    window: int = 16  # most numbered entries kept unacknowledged
    node_id: int = 3
    firmware: str = "sim-1"

    def __post_init__(self) -> None:
        if self.controller_timeout_ms and not 0 < self.window < self.buffer_capacity:
            raise ValueError("window must be at least 1 and smaller than buffer_capacity")


@dataclass
class _Entry:
    kind: str
    key: tuple[Any, ...]
    frame: bytes
    via: int
    duplicate: bool
    arrived_ms: int  # C's own clock
    n: int | None = None  # its number, once sent; None while waiting to be sent
    late: bool = False  # it had to wait (it was not handed up the moment it arrived)
    in_outage: bool = False  # stored while the controller was unreachable


@dataclass
class BridgeCounters:
    """Cumulative since C booted."""

    outages: int = 0
    buffered: int = 0  # entries stored while unreachable
    dropped_overflow: int = 0  # waiting entries lost because the buffer was full
    coalesced: int = 0  # waiting entries superseded by a newer one
    replayed: int = 0  # entries handed up late (after waiting)
    retransmitted: int = 0  # numbered entries sent again on request
    replays: int = 0
    gone: int = 0  # numbers asked for that C no longer held
    evicted_sent: int = 0  # numbered (sent, unacknowledged) entries given up when full
    refused: int = 0  # downlink commands C would not carry out


def _rank(entry: _Entry) -> int:
    """How much an entry is worth keeping: lower is given up first.

    A numbered entry was already handed up once, so the laptop probably has it: it goes first
    (failover reports excepted: they are never given up).
    """
    if entry.n is not None and entry.kind != FAILOVER:
        return 0
    return _EVICT_ORDER[entry.kind]


def _runs(numbers: list[int]) -> list[tuple[int, int]]:
    """Sorted numbers as (first, last) runs of consecutive values."""
    runs: list[tuple[int, int]] = []
    for n in numbers:
        if runs and runs[-1][1] == n - 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


def _classify(frame: Frame) -> tuple[str, tuple[Any, ...]]:
    kind = frame.msg_type
    p = frame.payload
    if kind is MessageType.DATA:
        return READING, (READING, frame.src, frame.session, frame.seq)
    if kind is MessageType.TOPOLOGY_REPORT:
        return REPORT, (REPORT, frame.src)
    if kind is MessageType.ROUTE_INSTALL_ACK:
        return ACK, (ACK, frame.src)
    if kind is MessageType.TELEMETRY:
        return TELEMETRY, (TELEMETRY, frame.src)
    if kind is MessageType.LOCAL_FAILOVER_REPORT:
        return FAILOVER, (
            FAILOVER,
            frame.src,
            p.epoch,  # type: ignore[union-attr]
            p.version,  # type: ignore[union-attr]
            p.from_next_hop,  # type: ignore[union-attr]
            p.to_next_hop,  # type: ignore[union-attr]
        )
    raise TypeError(f"C does not hand {kind.name} frames up")


class CBridge:
    def __init__(
        self,
        engine: Engine,
        config: BridgeConfig,
        emit: Callable[[UpMessage], None],
        originate: Originate | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine
        self.config = config
        self._emit = emit  # hands one message to the serial line
        self._originate = originate
        self._log = log or (lambda _text: None)
        self.running = False
        self.boot = 0
        self._life = 0  # changes at every power cycle: older timers do nothing
        self._reset()

    def _reset(self) -> None:
        self._buffer: deque[_Entry] = deque()
        self.counters = BridgeCounters()
        self.next_n = 1
        self.acked = 0
        self.reachable = self.config.controller_timeout_ms == 0  # unknown until a heartbeat
        self._last_hb: int | None = None
        self._outage_started: int | None = None  # None: no controller heard since boot yet
        self._outage_dropped = 0
        self._outage_coalesced = 0
        self._replaying = False
        self._replayed_this_time = 0
        self._resend: deque[int] = deque()
        self._evicted: dict[int, str] = {}  # number -> "R"/"M" of numbered entries given up
        self._pump_gen = 0
        self._pump_due = False

    @property
    def _watching(self) -> bool:
        return self.config.controller_timeout_ms > 0

    def attach_originator(self, originate: Originate) -> None:
        """Node C's radio side: how C sends a management frame it was asked to originate."""
        self._originate = originate

    # ---- power ----

    def boot_up(self, boot: int) -> None:
        """C powered on with session `boot`: empty RAM, numbering starts again at 1."""
        self._life += 1
        self.boot = boot
        self.running = True
        self._reset()
        self._emit(
            HelloUp(
                boot,
                self.config.node_id,
                self.config.buffer_capacity if self._watching else 0,
                self.config.window if self._watching else 0,
                self.config.firmware,
            )
        )
        self._log(f"C booted, session {boot:08x}")

    def power_off(self) -> None:
        """C lost power: everything it held is gone, and it answers nothing."""
        self._life += 1
        self.running = False
        self._reset()

    # ---- introspection (the simulator's tests and CLI; the controller never sees this) ----

    @property
    def held(self) -> int:
        """Entries waiting to be sent for the first time."""
        return sum(1 for e in self._buffer if e.n is None)

    @property
    def unacked(self) -> int:
        return sum(1 for e in self._buffer if e.n is not None)

    @property
    def size(self) -> int:
        return len(self._buffer)

    @property
    def replaying(self) -> bool:
        return self._replaying

    def slots(self) -> list[tuple[str, tuple[Any, ...], bool]]:
        """(kind, key, numbered) for every entry, oldest first."""
        return [(e.kind, e.key, e.n is not None) for e in self._buffer]

    # ---- from the radio side ----

    def from_radio(self, frame: Frame, via: int, duplicate: bool = False) -> None:
        """Something C received that the controller should see."""
        if not self.running:
            return
        kind, key = _classify(frame)
        entry = _Entry(kind, key, encode(frame), via, duplicate, self._engine.now_ms)
        if not self._watching:
            self._send(entry)  # the Phase 5a behaviour: numbered, sent, not kept
            return
        if self.reachable and self.held == 0 and self.unacked < self.config.window:
            self._buffer.append(entry)
            self._send(entry)  # live: numbered now, kept until acknowledged
            return
        entry.late = True
        entry.in_outage = not self.reachable
        if entry.in_outage:
            self.counters.buffered += 1
        self._store(entry)
        if self.reachable:
            self._schedule_pump()

    def _store(self, entry: _Entry) -> None:
        for existing in self._buffer:
            if existing.key != entry.key:
                continue
            if entry.kind == FAILOVER:
                # A node repeats a failover report; keep the first copy, which carries the
                # time the failover really happened (its age), and drop the repeat.
                self._count_coalesced()
                return
            if existing.n is None:
                self._buffer.remove(existing)
                self._count_coalesced()
                break
        while len(self._buffer) >= max(1, self.config.buffer_capacity):
            _, victim = min(enumerate(self._buffer), key=lambda item: (_rank(item[1]), item[0]))
            if _rank(entry) < _rank(victim):
                # The newcomer is worth less than everything held (a report arriving at a
                # buffer full of readings): it is the one given up.
                self._count_dropped()
                return
            self._buffer.remove(victim)
            if victim.n is None:
                self._count_dropped()
            else:
                # Sent once, never acknowledged. Remember what it was, so that if the laptop
                # asks for it C can say exactly what is gone (bounded: at most a window's worth
                # of numbers are ever unacknowledged).
                self.counters.evicted_sent += 1
                self._evicted[victim.n] = "R" if victim.kind == READING else "M"
        self._buffer.append(entry)

    def _count_dropped(self) -> None:
        self.counters.dropped_overflow += 1
        self._outage_dropped += 1

    def _count_coalesced(self) -> None:
        self.counters.coalesced += 1
        self._outage_coalesced += 1

    def _send(self, entry: _Entry, again: bool = False) -> None:
        if entry.n is None:
            entry.n = self.next_n
            self.next_n += 1
        self._emit(
            FrameUp(
                boot=self.boot,
                n=entry.n,
                age_ms=max(0, self._engine.now_ms - entry.arrived_ms),
                replayed=entry.late or again,
                via=entry.via,
                frame=entry.frame,
                duplicate=entry.duplicate,
            )
        )

    # ---- from the laptop side ----

    def receive(self, message: DownMessage) -> None:
        if not self.running:
            return  # C is off
        if isinstance(message, HeartbeatDown):
            self._on_ack(message.boot, message.ack)
            self._on_heartbeat()
        elif isinstance(message, AckDown):
            self._on_ack(message.boot, message.ack)
        elif isinstance(message, ResendDown):
            self._on_resend(message.boot, message.from_n)
        elif isinstance(message, TransmitDown):
            refusal = (
                "no radio"
                if self._originate is None
                else self._originate(message.dst, message.msg_type, message.payload)
            )
            if refusal is not None:
                self.counters.refused += 1
                self._log(f"refused tx type {message.msg_type} to {message.dst}: {refusal}")

    def _on_ack(self, boot: int, ack: int) -> None:
        if boot != self.boot or ack <= self.acked or ack >= self.next_n:
            return  # about another boot, old news, or a number never sent
        self.acked = ack
        for entry in [e for e in self._buffer if e.n is not None and e.n <= ack]:
            self._buffer.remove(entry)
        self._resend = deque(n for n in self._resend if n > ack)
        self._evicted = {n: kind for n, kind in self._evicted.items() if n > ack}
        if self.reachable and self.held:
            self._schedule_pump()

    def _status(self) -> StatusUp:
        numbered = [e.n for e in self._buffer if e.n is not None]
        return StatusUp(
            boot=self.boot,
            last=self.next_n - 1,
            low=min(numbered) if numbered else self.next_n,
            controller_reachable=True,
            held=self.held,
            unacked=len(numbered),
            capacity=self.config.buffer_capacity if self._watching else 0,
            window=self.config.window if self._watching else 0,
            dropped_overflow=self.counters.dropped_overflow,
            coalesced=self.counters.coalesced,
            replayed=self.counters.replayed,
            retransmitted=self.counters.retransmitted,
            outages=self.counters.outages,
        )

    def _on_heartbeat(self) -> None:
        if not self._watching:
            self._emit(self._status())  # not watching, but answering so the laptop can see C
            return
        now = self._engine.now_ms
        self._last_hb = now
        was_reachable = self.reachable
        self.reachable = True
        life = self._life
        self._engine.schedule(
            self.config.controller_timeout_ms, lambda: self._check_timeout(now, life)
        )
        self._emit(self._status())
        if was_reachable:
            return
        waiting = self.held
        if waiting:
            self._replaying = True
            self._replayed_this_time = 0
            self.counters.replays += 1
            started = self._outage_started if self._outage_started is not None else now
            self._emit(
                ReplayStartUp(
                    boot=self.boot,
                    entries=waiting,
                    held=sum(1 for e in self._buffer if e.n is None and e.in_outage),
                    dropped_overflow=self._outage_dropped,
                    coalesced=self._outage_coalesced,
                    outage_ms=max(0, now - started),
                )
            )
            self._log(f"controller back: replaying {waiting} entries")
        self._schedule_pump()

    def _check_timeout(self, stamp: int, life: int) -> None:
        if life != self._life or self._last_hb != stamp or not self.reachable:
            return  # powered off since, or a newer heartbeat arrived
        self.reachable = False
        self._pump_gen += 1  # a replay in progress stops; its remaining entries stay held
        self._pump_due = False
        self._replaying = False
        self._resend.clear()
        self.counters.outages += 1
        self._outage_started = self._engine.now_ms - self.config.controller_timeout_ms
        self._outage_dropped = 0
        self._outage_coalesced = 0
        self._log("no heartbeat: controller unreachable, holding what goes up")

    def _on_resend(self, boot: int, from_n: int) -> None:
        if boot != self.boot or from_n >= self.next_n:
            return
        numbered = sorted(e.n for e in self._buffer if e.n is not None and e.n >= from_n)
        held = set(numbered)
        # Numbers from `from_n` up to the newest one that is not held anymore are gone.
        gone = [n for n in range(from_n, self.next_n) if n not in held]
        if gone:
            self.counters.gone += len(gone)
            for first, last in _runs(gone):
                for start in range(first, last + 1, MAX_GONE_RANGE):
                    end = min(last, start + MAX_GONE_RANGE - 1)
                    kinds = "".join(self._evicted.get(n, "?") for n in range(start, end + 1))
                    self._emit(GoneUp(self.boot, start, end, kinds))
        queued = set(self._resend)
        self._resend.extend(n for n in numbered if n not in queued)
        if self.reachable:
            self._schedule_pump()

    # ---- sending from the buffer, one message per `replay_spacing_ms` ----

    def _schedule_pump(self) -> None:
        if self._pump_due:
            return
        self._pump_due = True
        gen, life = self._pump_gen, self._life
        self._engine.schedule(self.config.replay_spacing_ms, lambda: self._pump(gen, life))

    def _pump(self, gen: int, life: int) -> None:
        if gen != self._pump_gen or life != self._life:
            return
        self._pump_due = False
        if not self.reachable:
            return
        while self._resend:
            n = self._resend.popleft()
            entry = next((e for e in self._buffer if e.n == n), None)
            if entry is not None:
                self.counters.retransmitted += 1
                self._send(entry, again=True)
                self._schedule_pump()
                return
        waiting = next((e for e in self._buffer if e.n is None), None)
        if waiting is not None:
            if self.unacked >= self.config.window:
                return  # wait for an acknowledgement; it will restart the pump
            self.counters.replayed += 1
            self._replayed_this_time += 1
            self._send(waiting)
            self._schedule_pump()
            return
        if self._replaying:
            self._replaying = False
            self._emit(
                ReplayEndUp(
                    boot=self.boot,
                    replayed=self._replayed_this_time,
                    dropped_overflow=self._outage_dropped,
                    coalesced=self._outage_coalesced,
                )
            )
            self._log("replay finished")
