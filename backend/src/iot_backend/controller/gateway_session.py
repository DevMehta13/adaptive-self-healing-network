"""The laptop's side of the link to node C: heartbeats down, liveness and replay seen from above.

Two jobs, both between the laptop and C and never on the radio:

* **Heartbeat.** Every `heartbeat_interval_ms` the controller tells C "I am alive". C uses the
  absence of heartbeats to learn the controller is unreachable and to start holding what it would
  have sent up. The heartbeat is also how the buffer gets drained: the first one after an outage
  makes C replay.
* **Link monitoring.** C answers each heartbeat with a status message. If *nothing* arrives from
  C for `link_timeout_ms`, the laptop declares the gateway link lost "since" the last time it
  heard anything, and the network view it has is stale. This is the controller's own knowledge,
  on its own clock: it cannot see inside C.

States: `connecting` (nothing heard yet), `connected`, `lost`, `replaying` (C is handing up what
it held, between its start and end markers).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from iot_backend.transports import (
    Gateway,
    GatewayEvent,
    GatewayStatusEvent,
    Heartbeat,
    ReplayEndEvent,
    ReplayStartEvent,
)

CONNECTING, CONNECTED, LOST, REPLAYING = "connecting", "connected", "lost", "replaying"


@dataclass(frozen=True)
class GatewayLinkEvent:
    """A change in what the laptop knows about its link to C."""

    time_ms: int  # the laptop's clock
    kind: str  # link_lost, link_restored, replay_started, replay_finished
    data: dict[str, Any] = field(default_factory=dict)


class GatewaySession:
    def __init__(
        self,
        gateway: Gateway,
        heartbeat_interval_ms: int = 1_000,
        link_timeout_ms: int = 3_000,
    ) -> None:
        self._gateway = gateway
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.link_timeout_ms = link_timeout_ms
        self.state = CONNECTING
        self.since_ms = gateway.now_ms()
        self.last_heard_ms: int | None = None
        self.last_status: GatewayStatusEvent | None = None
        self.last_replay: dict[str, Any] | None = None
        self.heartbeats_sent = 0
        self._stopped = False
        self._handlers: list[Callable[[GatewayLinkEvent], None]] = []
        self.active = heartbeat_interval_ms > 0
        if self.active:
            gateway.subscribe(self._on_event)
            self._beat()
            self._watch()

    # ---- public ----

    def subscribe(self, handler: Callable[[GatewayLinkEvent], None]) -> None:
        self._handlers.append(handler)

    def stop(self) -> None:
        """The controller process ends: no more heartbeats, and C will notice."""
        self._stopped = True

    def describe(self) -> dict[str, Any]:
        status = self.last_status
        return {
            "state": self.state,
            "since_ms": self.since_ms,
            "last_heard_ms": self.last_heard_ms,
            "heartbeat_interval_ms": self.heartbeat_interval_ms,
            "link_timeout_ms": self.link_timeout_ms,
            # C's own report as of its last status message: during an outage this is old news
            "c_status_as_of_ms": None if status is None else status.received_ms,
            "c_reports_controller_reachable": None
            if status is None
            else status.controller_reachable,
            "c_buffer": None
            if status is None
            else {
                "held": status.held,
                "capacity": status.capacity,
                "dropped_overflow": status.dropped_overflow,
                "coalesced": status.coalesced,
                "replayed": status.replayed,
                "outages": status.outages,
            },
            "last_replay": self.last_replay,
        }

    # ---- internals ----

    def _emit(self, kind: str, **data: Any) -> None:
        event = GatewayLinkEvent(self._gateway.now_ms(), kind, data)
        for handler in self._handlers:
            handler(event)

    def _beat(self) -> None:
        if self._stopped:
            return
        self._gateway.send_heartbeat(Heartbeat(self._gateway.now_ms()))
        self.heartbeats_sent += 1
        self._gateway.call_later(self.heartbeat_interval_ms, self._beat)

    def _watch(self) -> None:
        if self._stopped:
            return
        now = self._gateway.now_ms()
        quiet_since = self.last_heard_ms if self.last_heard_ms is not None else self.since_ms
        if (
            self.state in (CONNECTED, REPLAYING, CONNECTING)
            and now - quiet_since > self.link_timeout_ms
        ):
            self.state, self.since_ms = LOST, quiet_since
            self._emit("link_lost", since_ms=quiet_since, silent_for_ms=now - quiet_since)
        self._gateway.call_later(max(1, self.heartbeat_interval_ms // 2), self._watch)

    def _on_event(self, event: GatewayEvent) -> None:
        if self._stopped:
            return
        now = self._gateway.now_ms()
        self.last_heard_ms = now
        if self.state in (LOST, CONNECTING):
            was_lost, since = self.state == LOST, self.since_ms
            self.state, self.since_ms = CONNECTED, now
            if was_lost:
                self._emit("link_restored", lost_since_ms=since, lost_for_ms=now - since)
        if isinstance(event, GatewayStatusEvent):
            self.last_status = event
        elif isinstance(event, ReplayStartEvent):
            self.state, self.since_ms = REPLAYING, now
            self.last_replay = {
                "state": "replaying",
                "started_ms": now,
                "entries": event.entries,
                "held": event.held,
                "dropped_overflow": event.dropped_overflow,
                "coalesced": event.coalesced,
                "outage_ms": event.outage_ms,
            }
            self._emit(
                "replay_started", **{k: v for k, v in self.last_replay.items() if k != "state"}
            )
        elif isinstance(event, ReplayEndEvent):
            self.state, self.since_ms = CONNECTED, now
            if self.last_replay is not None:
                self.last_replay = {
                    **self.last_replay,
                    "state": "finished",
                    "finished_ms": now,
                    "replayed": event.replayed,
                    "dropped_overflow": event.dropped_overflow,
                    "coalesced": event.coalesced,
                }
            self._emit(
                "replay_finished",
                replayed=event.replayed,
                dropped_overflow=event.dropped_overflow,
                coalesced=event.coalesced,
            )
