"""The laptop's side of the link to node C, shared by every way of reaching C.

`LaptopGateway` implements the `Gateway` interface the controller is written against. It is fed
wire messages (`transports/wire.py`) by whatever carries them: directly in the simulator, or as
serial lines by `SerialGateway`. Everything that matters to the controller happens here, once,
so the simulated and the serial paths cannot drift apart.

Two jobs:

* **Turn C's messages into controller events.** C hands radio frames up as raw bytes with the
  link-layer sender; they are decoded here with the protocol codec.
* **Reliable uplink.** C numbers each frame it hands up (`n`, per boot of C). The laptop keeps
  `ack`, the highest number up to which it has everything, and sends it back (in every
  heartbeat, and after every few frames). That is a cumulative acknowledgement, as in TCP: C
  may forget what is acknowledged. A number above `ack` that is missing is a gap: the laptop
  asks C to resend from `ack + 1`. If C no longer holds those numbers it says so, and the loss
  is counted instead of silent. A message that arrives twice is counted once.

Memory is bounded: the out-of-order set holds at most `max_out_of_order` numbers.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from iot_backend.protocol import (
    DEFAULT_TTL,
    HEADER_SIZE,
    NODE_C,
    DataPayload,
    FailoverReason,
    Frame,
    LocalFailoverReportPayload,
    MessageType,
    NeighborCode,
    RouteAckStatus,
    RouteInstallAckPayload,
    RouteUpdatePayload,
    TelemetryPayload,
    TopologyReportPayload,
    decode,
    encode,
)
from iot_backend.transports.gateway import (
    GatewayEvent,
    GatewayHandler,
    GatewayStatusEvent,
    Heartbeat,
    LocalFailoverEvent,
    NeighborObservation,
    ReadingEvent,
    ReplayEndEvent,
    ReplayStartEvent,
    RouteInstallAckEvent,
    RouteUpdateCommand,
    TelemetryEvent,
    TopologyReportEvent,
    UplinkLossEvent,
)
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


class Clock(Protocol):
    def now_ms(self) -> int: ...

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None: ...


@dataclass(frozen=True)
class UplinkConfig:
    data_ttl: int = DEFAULT_TTL  # the TTL every node gives DATA; hops are counted from it
    ack_every: int = 8  # send an acknowledgement after this many new frames (and on heartbeats)
    resend_timeout_ms: int = 1_000  # ask again if a requested gap is still open after this
    max_out_of_order: int = 256  # numbers above `ack` remembered; C's window is far smaller


@dataclass
class UplinkStats:
    frames: int = 0  # numbered frames accepted (each counted once)
    duplicates: int = 0  # numbered frames received again and dropped
    acks_sent: int = 0  # acknowledgement-only lines
    resend_requests: int = 0
    resend_repeats: int = 0  # of those, asked again because the first answer did not come
    lost_gone: int = 0  # numbers C said it no longer held (and we never had)
    lost_readings: int = 0  # of those, readings
    lost_unknown: int = 0  # of those, of a kind C could not say
    lost_restart: int = 0  # numbers known to be missing when C rebooted (a lower bound)
    superseded: int = 0  # numbers C replaced by a newer report from the same node (not lost)
    c_restarts: int = 0
    out_of_window: int = 0  # frames numbered impossibly far ahead (refused)
    statuses: int = 0
    hellos: int = 0


def payload_bytes(command: RouteUpdateCommand) -> bytes:
    """The ROUTE_UPDATE payload alone. C wraps it in its own header (src, session, seq)."""
    payload = RouteUpdatePayload(
        command.epoch,
        command.version,
        command.destination,
        command.next_hop,
        command.backup_next_hop,
    )
    # Any valid header will do: only the bytes after it are sent.
    return encode(Frame(NODE_C, command.node, 1, 0, DEFAULT_TTL, payload))[HEADER_SIZE:]


def frame_event(
    frame: Frame, up: FrameUp, received_ms: int, data_ttl: int = DEFAULT_TTL
) -> GatewayEvent | None:
    """The controller event for one radio frame C handed up, or None if it is not one."""
    p = frame.payload
    common: dict[str, Any] = {
        "received_ms": received_ms,
        "origin": frame.src,
        "session": frame.session,
        "seq": frame.seq,
        "age_ms": up.age_ms,
        "replayed": up.replayed,
    }
    if isinstance(p, DataPayload):
        return ReadingEvent(
            **common,
            value=p.value,
            via=up.via,
            ttl=frame.ttl,
            hops=data_ttl - frame.ttl + 1,
            duplicate=up.duplicate,
        )
    if isinstance(p, TopologyReportPayload):
        return TopologyReportEvent(
            **common,
            neighbors=tuple(
                NeighborObservation(
                    neighbor=e.neighbor,
                    state=NeighborCode(e.state).name.lower(),
                    session=e.session,
                    last_heard_ms_ago=e.age_ms,
                    rtt_ms=e.rtt_ms,
                    probes_ok=e.probes_ok,
                    probes_total=e.probes_total,
                )
                for e in p.entries
            ),
        )
    if isinstance(p, RouteInstallAckPayload):
        return RouteInstallAckEvent(
            **common,
            epoch=p.epoch,
            version=p.version,
            destination=p.destination,
            status=RouteAckStatus(p.status).name.lower(),
        )
    if isinstance(p, LocalFailoverReportPayload):
        return LocalFailoverEvent(
            **common,
            epoch=p.epoch,
            version=p.version,
            destination=p.destination,
            from_next_hop=p.from_next_hop,
            to_next_hop=p.to_next_hop,
            reason=FailoverReason(p.reason).name.lower(),
        )
    if isinstance(p, TelemetryPayload):
        return TelemetryEvent(
            **common,
            created=p.created,
            acknowledged=p.acknowledged,
            lost=p.lost,
            no_route=p.no_route,
            delivered=p.delivered,
            forwarded=p.forwarded,
            dropped=p.dropped,
            failovers=p.failovers,
            send_failures=p.send_failures,
        )
    return None


@dataclass
class _Pending:
    from_n: int
    upto: int  # the request is answered once `ack` reaches this
    at_ms: int


class LaptopGateway:
    def __init__(
        self,
        clock: Clock,
        send_down: Callable[[DownMessage], None],
        config: UplinkConfig | None = None,
    ) -> None:
        self._clock = clock
        self._send_down = send_down
        self.config = config or UplinkConfig()
        self._handlers: list[GatewayHandler] = []
        self.stats = UplinkStats()
        self.commands_sent = 0  # route updates handed to C
        self.heartbeats_sent = 0
        # reliable-uplink state for the current boot of C
        self.boot = 0  # 0: not known yet
        self.ack = 0
        self.last_known = 0  # highest number C is known to have assigned
        self._above: set[int] = set()  # received numbers above `ack`
        self._early: dict[int, set[int]] = {}  # frames received before C's boot was known
        self._pending: _Pending | None = None
        self._since_ack = 0
        self.last_status: StatusUp | None = None

    # ---- the Gateway interface (what the controller uses) ----

    def subscribe(self, handler: GatewayHandler) -> None:
        self._handlers.append(handler)

    def now_ms(self) -> int:
        return self._clock.now_ms()

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self._clock.call_later(delay_ms, callback)

    def send_route_update(self, command: RouteUpdateCommand) -> None:
        self.commands_sent += 1
        self._send_down(
            TransmitDown(command.node, MessageType.ROUTE_UPDATE, payload_bytes(command))
        )

    def send_heartbeat(self, heartbeat: Heartbeat) -> None:
        del heartbeat  # its time is the laptop's own; C needs only the acknowledgement
        self.heartbeats_sent += 1
        self._since_ack = 0
        self._send_down(HeartbeatDown(self.boot, self.ack))

    # ---- what the carrier hands us ----

    def receive(self, message: UpMessage) -> None:
        now = self._clock.now_ms()
        if isinstance(message, FrameUp):
            self._on_frame(message, now)
        elif isinstance(message, StatusUp):
            self._on_status(message, now)
        elif isinstance(message, HelloUp):
            self.stats.hellos += 1
            if message.boot != self.boot:
                self._new_boot(message.boot, now, low=1)
        elif isinstance(message, GoneUp):
            self._on_gone(message, now)
        elif isinstance(message, ReplayStartUp):
            self._note_boot(message.boot, now)
            self._emit(
                ReplayStartEvent(
                    received_ms=now,
                    entries=message.entries,
                    held=message.held,
                    dropped_overflow=message.dropped_overflow,
                    dropped_readings=message.dropped_readings,
                    coalesced=message.coalesced,
                    outage_ms=message.outage_ms,
                )
            )
        elif isinstance(message, ReplayEndUp):
            self._note_boot(message.boot, now)
            self._emit(
                ReplayEndEvent(
                    received_ms=now,
                    replayed=message.replayed,
                    dropped_overflow=message.dropped_overflow,
                    dropped_readings=message.dropped_readings,
                    coalesced=message.coalesced,
                )
            )

    def describe(self) -> dict[str, Any]:
        """The laptop's own view of the uplink, for the dashboard."""
        s = self.stats
        return {
            "c_boot": self.boot or None,
            "acknowledged_up_to": self.ack,
            "highest_known": self.last_known,
            "missing_now": max(0, self.last_known - self.ack - len(self._above)),
            "frames": s.frames,
            "duplicates_dropped": s.duplicates,
            "resend_requests": s.resend_requests,
            "resend_repeats": s.resend_repeats,
            "lost": {
                "gone_at_c": s.lost_gone,
                "of_which_readings": s.lost_readings,
                "of_unknown_kind": s.lost_unknown,
                "c_restarted_at_least": s.lost_restart,
            },
            "superseded": s.superseded,
            "c_restarts": s.c_restarts,
            "out_of_window": s.out_of_window,
        }

    # ---- internals ----

    def _emit(self, event: GatewayEvent) -> None:
        for handler in list(self._handlers):
            handler(event)

    def _note_boot(self, boot: int, now: int) -> None:
        """A marker from a boot of C we have not seen means C restarted."""
        if self.boot != 0 and boot != self.boot:
            self._new_boot(boot, now, low=1)

    def _new_boot(self, boot: int, now: int, low: int) -> None:
        """C's boot changed (or became known). Whatever C held in its previous life is gone."""
        if self.boot != 0:
            missing = max(0, self.last_known - self.ack - len(self._above))
            self.stats.c_restarts += 1
            self.stats.lost_restart += missing
            self._emit(
                UplinkLossEvent(
                    received_ms=now,
                    reason="c_restarted",
                    boot=self.boot,
                    # the numbers known to be missing; 0..0 when none were
                    first=self.ack + 1 if missing else 0,
                    last=self.last_known if missing else 0,
                    count=missing,
                )
            )
        early = self._early.pop(boot, set())
        self._early.clear()
        self.boot = boot
        self.ack = low - 1
        self.last_known = self.ack
        self._above = {n for n in early if n > self.ack}
        self._advance()
        self._pending = None
        self._since_ack = 0

    def _advance(self) -> None:
        while self.ack + 1 in self._above:
            self.ack += 1
            self._above.discard(self.ack)
        self.last_known = max(self.last_known, self.ack, max(self._above, default=0))

    def _on_status(self, status: StatusUp, now: int) -> None:
        self.stats.statuses += 1
        if status.boot != self.boot:
            self._new_boot(status.boot, now, low=status.low)
        # If C's lowest held number is above what we have, some numbers are missing: that is a
        # gap like any other. Asking for it makes C say exactly what is gone (never skipped).
        self.last_known = max(self.last_known, status.last)
        self.last_status = status
        self._emit(
            GatewayStatusEvent(
                received_ms=now,
                controller_reachable=status.controller_reachable,
                held=status.held,
                capacity=status.capacity,
                dropped_overflow=status.dropped_overflow,
                dropped_readings=status.dropped_readings,
                coalesced=status.coalesced,
                replayed=status.replayed,
                outages=status.outages,
            )
        )
        self._check_gap(now)

    def _on_frame(self, up: FrameUp, now: int) -> None:
        if self.boot == 0:
            # C's boot is not known yet (its hello and status were missed): take the frame,
            # and remember its number until the next status says where numbering stands.
            if up.boot not in self._early and len(self._early) >= 4:
                self._early.clear()  # bounded: only the latest few boots are worth remembering
            seen = self._early.setdefault(up.boot, set())
            if up.n in seen or len(seen) >= self.config.max_out_of_order:
                self.stats.duplicates += up.n in seen
                return
            seen.add(up.n)
            self._deliver(up, now)
            return
        if up.boot != self.boot:
            self._new_boot(up.boot, now, low=1)
        if up.n <= self.ack or up.n in self._above:
            self.stats.duplicates += 1
            return
        if up.n - self.ack > self.config.max_out_of_order:
            self.stats.out_of_window += 1  # C never runs this far ahead of our acknowledgement
            return
        self._above.add(up.n)
        self._advance()
        self._deliver(up, now)
        self._since_ack += 1
        if self._since_ack >= self.config.ack_every:
            self._since_ack = 0
            self.stats.acks_sent += 1
            self._send_down(AckDown(self.boot, self.ack))
        self._check_gap(now)

    def _deliver(self, up: FrameUp, now: int) -> None:
        self.stats.frames += 1
        frame = decode(up.frame)  # already validated when the line was parsed
        event = frame_event(frame, up, now - up.age_ms, self.config.data_ttl)
        if event is not None:
            self._emit(event)

    def _on_gone(self, gone: GoneUp, now: int) -> None:
        if self.boot == 0 or gone.boot != self.boot:
            return  # about numbers of a boot we are not tracking
        missing = [
            n for n in range(max(gone.first, self.ack + 1), gone.last + 1) if n not in self._above
        ]
        self._above.update(missing)
        self._advance()
        # "S": superseded by a newer report from the same node, which we have or will get.
        superseded = [n for n in missing if gone.kinds[n - gone.first] == "S"]
        self.stats.superseded += len(superseded)
        missing = [n for n in missing if n not in superseded]
        if missing:
            kinds = [gone.kinds[n - gone.first] for n in missing]
            readings, unknown = kinds.count("R"), kinds.count("?")
            self.stats.lost_gone += len(missing)
            self.stats.lost_readings += readings
            self.stats.lost_unknown += unknown
            self._emit(
                UplinkLossEvent(
                    now,
                    "gone_at_c",
                    self.boot,
                    missing[0],
                    missing[-1],
                    len(missing),
                    readings=readings,
                    unknown=unknown,
                )
            )
        self._check_gap(now)

    def _check_gap(self, now: int) -> None:
        """Ask for missing numbers: at most one request outstanding, repeated after a timeout."""
        pending = self._pending
        if pending is not None and self.ack >= pending.upto:
            self._pending = pending = None
        if self.boot == 0 or self.ack >= self.last_known:
            return
        if pending is not None and now - pending.at_ms < self.config.resend_timeout_ms:
            return
        if pending is not None:
            self.stats.resend_repeats += 1
        self.stats.resend_requests += 1
        self._pending = _Pending(self.ack + 1, self.last_known, now)
        self._send_down(ResendDown(self.boot, self.ack + 1))


@dataclass
class EngineClock:
    """The simulator's virtual clock seen through the `Clock` interface."""

    engine: Any

    def now_ms(self) -> int:
        return self.engine.now_ms

    def call_later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self.engine.schedule(delay_ms, callback)
