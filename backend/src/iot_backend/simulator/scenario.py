"""The Phase 2 scenario: four virtual nodes A, B, C, D that can all physically hear each other,
restricted to the diamond A-B, B-C, A-D, D-C by each node's own permitted-neighbor list.

`Simulation` is the public face. Its nodes, links and counters are private: anything
that later needs to learn about the network (the controller) must do so through a
gateway interface fed by events, never by reading these objects.
"""

import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from iot_backend.protocol import DEFAULT_TTL, NODE_IDS, NODE_NAMES, PacketIdentity
from iot_backend.simulator.bridge import BridgeConfig, CBridge
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.gateway import SimulatedGateway
from iot_backend.simulator.link import Link, LinkConfig
from iot_backend.simulator.nodes import ACK_FLOODING, ACK_ROUTED, Node, NodeSettings
from iot_backend.simulator.serial_device import SerialCDevice
from iot_backend.simulator.trace import (
    FRAME_DROPPED,
    GATEWAY_ATTACHED,
    GATEWAY_DETACHED,
    Trace,
)
from iot_backend.transports.laptop_gateway import EngineClock, LaptopGateway, UplinkConfig
from iot_backend.transports.serial_gateway import SerialGateway

NODE_LETTERS = tuple(sorted(NODE_IDS))  # ("A", "B", "C", "D")
LINK_KEYS = tuple((s, d) for s in NODE_LETTERS for d in NODE_LETTERS if s != d)

# The classroom topology. Physically every board hears every other; this is the *logical* rule.
DIAMOND = {"A": {"B", "D"}, "B": {"A", "C"}, "C": {"B", "D"}, "D": {"A", "C"}}
ROUTING_MANUAL = "manual"
ROUTING_CONTROLLER = "controller"
# Default manual routes: the normal path goes through B in both directions. Direct neighbors
# are always reachable directly (added automatically).
DEFAULT_ROUTES = {"A": {"C": "B"}, "C": {"A": "B"}}


@dataclass
class ScenarioConfig:
    seed: int = 0
    reading_interval_ms: int = 2000
    ack_timeout_ms: int = 1000
    max_attempts: int = 3
    ttl: int = DEFAULT_TTL
    queue_capacity: int = 16
    service_ms: int = 5
    trace_max_events: int = 100_000
    # Phase 3 discovery and management traffic. Set discovery=False and
    # ack_delivery="routed" for the plain Phase 2 data-plane behaviour.
    discovery: bool = True
    ack_delivery: str = ACK_FLOODING
    hello_interval_ms: int = 1000
    hello_jitter_ms: int = 100
    neighbor_timeout_ms: int = 3000
    report_interval_ms: int = 5000
    report_coalesce_ms: int = 50
    flood_cache_size: int = 64
    # Phase 4a. routing="manual": routing tables are configured (Phase 2/3 behaviour).
    # routing="controller": nodes start with no data routes; the controller installs them.
    routing: str = "manual"
    probe_interval_ms: int = 0  # 0 = no probes, so no link is ever measured
    probe_jitter_ms: int = 100
    rtt_window: int = 5
    rtt_min_samples: int = 3
    rtt_max_age_ms: int = 10_000
    probe_window: int = 0  # Phase 9: completed probes remembered per neighbor (0 = off)
    probe_stats_min: int = 5
    max_routes: int = 8
    # Phase 4b: local failover (off by default; earlier scenarios are unchanged)
    local_failover: bool = False
    failover_t1: bool = True
    failover_t2_k: int = 3
    send_fail_ms: int = 30
    failover_report_repeats: int = 5
    telemetry_interval_ms: int = 0  # Phase 5a: 0 = nodes send no telemetry
    # Phase 5b: C's side of the laptop link. 0 = C does not watch for heartbeats and
    # passes everything straight up (the Phase 5a behaviour).
    controller_timeout_ms: int = 0
    buffer_capacity: int = 50
    replay_spacing_ms: int = 20
    # Phase 6-prep: the reliable uplink. C keeps at most this many numbered messages waiting
    # for the laptop's acknowledgement.
    uplink_window: int = 16
    # How C's messages reach the laptop side: "direct" (message objects) or "serial" (the
    # serial text format through memory, parsed by a SerialGateway as on hardware).
    transport: str = "direct"
    gateway: str = "C"  # node whose USB link carries events to the controller
    reading_src: str = "A"
    reading_dst: str = "C"
    # Permitted neighbors per node (default: the diamond). Override to model a misconfiguration.
    permitted: dict[str, set[str]] = field(
        default_factory=lambda: {k: set(v) for k, v in DIAMOND.items()}
    )
    # Extra or overriding routes: node -> {destination: next hop}.
    routes: dict[str, dict[str, str]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_ROUTES.items()}
    )
    # Keyed by ("A", "B") for the A->B direction. Missing keys use defaults.
    links: dict[tuple[str, str], LinkConfig] = field(default_factory=dict)
    # Testing aid: first sequence number after boot, per node (e.g. near the 2**32 wrap).
    initial_seq: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Summary:
    readings_created: int  # readings the source made
    attempts: int  # DATA transmissions attempted for them (first tries plus retries)
    frames_sent: int  # every frame put on the air by any node (created or forwarded)
    forwarded: int  # of those, frames relayed for someone else
    delivered_unique: int  # readings accepted at the destination, each counted once
    duplicates: int  # repeat copies received (and re-acknowledged) at the destination
    acknowledged: int  # readings whose ACK reached the source
    lost: int  # readings the source gave up on after its last attempt
    dropped_no_route: int  # readings dropped at creation because the source had no route
    unresolved: int  # still waiting at the end, or forgotten when the source stopped
    hellos: int  # HELLO frames created
    reports: int  # TOPOLOGY_REPORT frames created
    probes: int  # PROBE frames created
    flood_cache_evictions: int  # management identities forgotten because the cache was full
    drops: dict[str, int]  # discarded or rejected frames by reason


class Simulation:
    def __init__(self, config: ScenarioConfig | None = None) -> None:
        self.config = config or ScenarioConfig()
        self._validate()

        self._engine = Engine()
        self.trace = Trace(self.config.trace_max_events)
        self._nodes: dict[str, Node] = {}
        self._links: dict[tuple[str, str], Link] = {}
        self._no_link_drops: Counter[str] = Counter()
        self._mac_rngs: dict[tuple[str, str], random.Random] = {}

        settings = NodeSettings(
            ttl=self.config.ttl,
            queue_capacity=self.config.queue_capacity,
            service_ms=self.config.service_ms,
            reading_interval_ms=self.config.reading_interval_ms,
            ack_timeout_ms=self.config.ack_timeout_ms,
            max_attempts=self.config.max_attempts,
            discovery=self.config.discovery,
            ack_delivery=self.config.ack_delivery,
            hello_interval_ms=self.config.hello_interval_ms,
            hello_jitter_ms=self.config.hello_jitter_ms,
            neighbor_timeout_ms=self.config.neighbor_timeout_ms,
            report_interval_ms=self.config.report_interval_ms,
            report_coalesce_ms=self.config.report_coalesce_ms,
            flood_cache_size=self.config.flood_cache_size,
            gateway_id=NODE_IDS[self.config.gateway],
            probe_interval_ms=self.config.probe_interval_ms,
            probe_jitter_ms=self.config.probe_jitter_ms,
            rtt_window=self.config.rtt_window,
            rtt_min_samples=self.config.rtt_min_samples,
            rtt_max_age_ms=self.config.rtt_max_age_ms,
            probe_window=self.config.probe_window,
            probe_stats_min=self.config.probe_stats_min,
            max_routes=self.config.max_routes,
            local_failover=self.config.local_failover,
            failover_t1=self.config.failover_t1,
            failover_t2_k=self.config.failover_t2_k,
            send_fail_ms=self.config.send_fail_ms,
            failover_report_repeats=self.config.failover_report_repeats,
            telemetry_interval_ms=self.config.telemetry_interval_ms,
        )
        bridge_config = BridgeConfig(
            controller_timeout_ms=self.config.controller_timeout_ms,
            buffer_capacity=self.config.buffer_capacity,
            replay_spacing_ms=self.config.replay_spacing_ms,
            window=self.config.uplink_window,
            node_id=NODE_IDS[self.config.gateway],
        )
        uplink = UplinkConfig(data_ttl=self.config.ttl)
        self._gateway: LaptopGateway | None
        self._link: SimulatedGateway | SerialCDevice
        if self.config.transport == "serial":
            device = SerialCDevice(self._engine, bridge_config, lambda data: gateway.feed(data))
            gateway = SerialGateway(EngineClock(self._engine), device.feed_down, uplink)
            self._gateway, self._link = gateway, device
        elif self.config.transport == "external":
            # The fake C device: C's serial bytes leave this process (`link.write_up`), and the
            # laptop side is a real backend somewhere else. There is no controller in here.
            self._link = SerialCDevice(self._engine, bridge_config, lambda data: None)
            self._gateway = None
        else:
            direct = SimulatedGateway(self._engine, bridge_config, uplink)
            self._gateway, self._link = direct, direct
        for letter in NODE_LETTERS:
            permitted = {NODE_IDS[n] for n in self.config.permitted.get(letter, set())}
            routes: dict[int, int] = {}
            if self.config.routing == ROUTING_MANUAL:
                routes = {NODE_IDS[n]: NODE_IDS[n] for n in DIAMOND[letter]}  # direct neighbors
                for dst, hop in self.config.routes.get(letter, {}).items():
                    routes[NODE_IDS[dst]] = NODE_IDS[hop]
            # With routing="controller" the table starts empty: no data route exists until the
            # controller installs one, so nothing is forwarded by a hidden default.
            is_source = letter == self.config.reading_src
            self._nodes[letter] = Node(
                NODE_IDS[letter],
                self._engine,
                self.trace,
                self._transmit,
                self.config.seed,
                permitted=permitted,
                routes=routes,
                settings=settings,
                reading_dst=NODE_IDS[self.config.reading_dst] if is_source else None,
                initial_seq=self.config.initial_seq.get(letter, 1),
            )

        # Physically, every board can reach every other.
        for src, dst in LINK_KEYS:
            self._links[(src, dst)] = Link(
                src,
                dst,
                self.config.links.get((src, dst), LinkConfig()),
                self.config.seed,
                self._engine,
                self.trace,
                deliver=self._make_delivery(self._nodes[dst], NODE_IDS[src]),
            )

        bridge, c_node = self._link.bridge, self._nodes[self.config.gateway]
        c_node.gateway_sink = bridge.from_radio
        c_node.on_boot = bridge.boot_up
        c_node.on_stop = bridge.power_off
        bridge.attach_originator(c_node.originate)

        for letter in NODE_LETTERS:
            self._nodes[letter].boot()

    def _validate(self) -> None:
        letters = set(NODE_LETTERS)
        unknown = set(self.config.links) - set(LINK_KEYS)
        if unknown:
            raise ValueError(f"unknown links {sorted(unknown)}")
        for name in (self.config.reading_src, self.config.reading_dst):
            if name not in letters:
                raise ValueError(f"unknown node {name!r}")
        if self.config.gateway not in letters:
            raise ValueError(f"unknown gateway {self.config.gateway!r}")
        if self.config.ack_delivery not in (ACK_FLOODING, ACK_ROUTED):
            raise ValueError("ack_delivery must be 'flooding' or 'routed'")
        if self.config.routing not in (ROUTING_MANUAL, ROUTING_CONTROLLER):
            raise ValueError("routing must be 'manual' or 'controller'")
        if self.config.probe_interval_ms < 0:
            raise ValueError("probe_interval_ms must be >= 0")
        if self.config.transport not in ("direct", "serial", "external"):
            raise ValueError("transport must be 'direct', 'serial' or 'external'")
        if self.config.reading_src == self.config.reading_dst:
            raise ValueError("reading source and destination must differ")
        for table in (self.config.permitted, self.config.initial_seq):
            if set(table) - letters:
                raise ValueError(f"unknown nodes in {sorted(set(table) - letters)}")
        for node, table in self.config.routes.items():
            if node not in letters or set(table) - letters or set(table.values()) - letters:
                raise ValueError(f"bad routing table for {node!r}")
        for node, neighbors in self.config.permitted.items():
            if set(neighbors) - letters:
                raise ValueError(f"unknown neighbors for {node!r}")

    # ---- public API ----

    @property
    def now_ms(self) -> int:
        return self._engine.now_ms

    @property
    def gateway(self) -> LaptopGateway:
        """The only door the controller may use to learn about the network."""
        if self._gateway is None:
            raise RuntimeError("external transport: the laptop side is another process")
        return self._gateway

    @property
    def link(self) -> SimulatedGateway | SerialCDevice:
        """The simulated USB link (connected / heartbeats_blocked switches, loss counters)."""
        return self._link

    @property
    def c_bridge(self) -> CBridge:
        """Node C's laptop-link firmware model. An observation aid; never for the controller."""
        return self._link.bridge

    def run(self, duration_ms: int) -> None:
        """Advance the virtual clock by `duration_ms`, processing everything due."""
        self._engine.run_until(self._engine.now_ms + duration_ms)

    def _at(self, at_ms: int | None, action: Callable[[], None]) -> None:
        if at_ms is None:
            action()
        else:
            self._engine.schedule(max(0, at_ms - self._engine.now_ms), action)

    def call_at(self, at_ms: int | None, action: Callable[[], None]) -> None:
        """Run `action` now, or at an absolute simulated time (for scripted controller actions)."""
        self._at(at_ms, action)

    def route_state(self, node: str, dst: str) -> tuple[int, int, str | None] | None:
        """What a node holds for `dst`: (epoch, version, next hop name or None), or None.

        An observation aid for tests and the CLI. The controller must not use it.
        """
        state = self._nodes[node].route_state(NODE_IDS[dst])
        if state is None:
            return None
        epoch, version, hop = state
        return epoch, version, None if hop is None else NODE_NAMES[hop]

    def set_gateway_connected(self, connected: bool, at_ms: int | None = None) -> None:
        """Cut or restore the controller's view of, and commands to, the network.

        Models the controller process (or the USB link to C) being unreachable. Node C keeps
        receiving, acknowledging and forwarding; only the controller is blind and mute. C
        notices by the missing heartbeats and holds what goes up (Phase 5b).
        """

        def apply() -> None:
            self._link.connected = connected
            self.trace.record(
                self._engine.now_ms,
                self.config.gateway,
                GATEWAY_ATTACHED if connected else GATEWAY_DETACHED,
            )

        self._at(at_ms, apply)

    def node_counters(self, node: str):  # noqa: ANN201 - NodeCounters, for tests and the CLI
        """A node's counters (readings, failovers, failover reports, ...)."""
        return self._nodes[node].counters

    def forwarding_state(self, node: str, dst: str) -> dict[str, object] | None:
        """A node's installed entry as strings (names), with failover flags; for tests and CLI."""
        state = self._nodes[node].forwarding_state(NODE_IDS[dst])
        if state is None:
            return None
        names = {
            k: (None if state[k] is None else NODE_NAMES[state[k]])
            for k in (
                "primary",
                "backup",
                "current",
            )
        }  # type: ignore[index]
        return {**state, **names}

    def stop_node(self, name: str, at_ms: int | None = None) -> None:
        """Power a node off (now, or at an absolute simulated time)."""
        self._at(at_ms, self._nodes[name].stop)

    def start_node(self, name: str, at_ms: int | None = None) -> None:
        """Power a stopped node on: it boots with a new session and empty RAM."""
        self._at(at_ms, self._nodes[name].boot)

    def restart_node(self, name: str, at_ms: int | None = None) -> None:
        """Convenience: stop and immediately start a node."""
        self.stop_node(name, at_ms)
        self.start_node(name, at_ms)

    def set_route(
        self, node: str, dst: str, next_hop: str | None, at_ms: int | None = None
    ) -> None:
        """Manually set (or, with next_hop=None, remove) one routing-table entry."""
        target = self._nodes[node]
        dst_id = NODE_IDS[dst]
        hop_id = NODE_IDS[next_hop] if next_hop is not None else None

        def apply() -> None:
            if hop_id is None:
                target.routes.pop(dst_id, None)
            else:
                target.routes[dst_id] = hop_id

        self._at(at_ms, apply)

    def set_permitted(self, node: str, neighbors: set[str], at_ms: int | None = None) -> None:
        """Replace a node's permitted-neighbor list (to model a misconfiguration)."""
        target = self._nodes[node]
        ids = {NODE_IDS[n] for n in neighbors}
        self._at(at_ms, lambda: setattr(target, "permitted", ids))

    def set_link_down(self, src: str, dst: str, down: bool, at_ms: int | None = None) -> None:
        """Fail or restore one direction of a link. The reverse direction is unaffected."""
        link = self._links[(src, dst)]
        self._at(at_ms, lambda: link.reconfigure(down=down))

    def configure_link(
        self,
        src: str,
        dst: str,
        at_ms: int | None = None,
        delay_ms: int | None = None,
        loss: float | None = None,
    ) -> None:
        """Change a link's delay and/or loss probability from the given time on."""
        link = self._links[(src, dst)]
        changes: dict[str, object] = {}
        if delay_ms is not None:
            changes["delay_ms"] = delay_ms
        if loss is not None:
            changes["loss"] = loss
        replace(link.config, **changes)  # validate now, so a bad value fails immediately
        self._at(at_ms, lambda: link.reconfigure(**changes))

    def inject_frame(self, to_name: str, data: bytes, at_ms: int, from_name: str) -> None:
        """Make raw bytes appear at a node's radio at an absolute time (e.g. a corrupted frame)."""
        node = self._nodes[to_name]
        sender = NODE_IDS[from_name]
        self._engine.schedule(
            max(0, at_ms - self._engine.now_ms), lambda: node.receive(data, sender)
        )

    def summary(self) -> Summary:
        """Totals taken from node and link counters (not from the bounded trace)."""
        counters = [node.counters for node in self._nodes.values()]
        drops: Counter[str] = Counter(self._no_link_drops)
        for node in self._nodes.values():
            drops.update(node.drops)
        for link in self._links.values():
            drops.update(link.dropped)
        created = sum(c.created for c in counters)
        acknowledged = sum(c.acknowledged for c in counters)
        lost = sum(c.lost for c in counters)
        no_route = sum(c.dropped_no_route for c in counters)
        return Summary(
            readings_created=created,
            attempts=sum(c.attempts for c in counters),
            frames_sent=sum(c.sent for c in counters),
            forwarded=sum(c.forwarded for c in counters),
            delivered_unique=sum(c.delivered_unique for c in counters),
            duplicates=sum(c.duplicates for c in counters),
            acknowledged=acknowledged,
            lost=lost,
            dropped_no_route=no_route,
            unresolved=created - acknowledged - lost - no_route,
            hellos=sum(c.hellos for c in counters),
            reports=sum(c.reports for c in counters),
            probes=sum(c.probes for c in counters),
            flood_cache_evictions=sum(n.flood_cache_evictions for n in self._nodes.values()),
            drops=dict(sorted(drops.items())),
        )

    # ---- internals ----

    @staticmethod
    def _make_delivery(node: Node, from_id: int) -> Callable[[bytes], None]:
        return lambda data: node.receive(data, from_id)

    def _transmit(
        self, from_id: int, to_id: int, data: bytes, identity: PacketIdentity, msg: str
    ) -> None:
        src, dst = NODE_NAMES.get(from_id), NODE_NAMES.get(to_id)
        link = self._links.get((src, dst)) if src and dst else None
        if link is None:  # no such board
            self._no_link_drops["no_link"] += 1
            self.trace.record(
                self._engine.now_ms,
                src or str(from_id),
                FRAME_DROPPED,
                identity,
                reason="no_link",
            )
            return
        delivered = link.transmit(data, identity, msg)
        sender = self._nodes[src]  # type: ignore[index]
        if sender.wants_send_feedback:
            self._send_feedback(sender, str(src), str(dst), link, delivered)

    def _send_feedback(self, sender: Node, src: str, dst: str, link: Link, delivered: bool) -> None:
        """Deliver the link layer's verdict on a unicast send, as the ESP-NOW callback would.

        The verdict says whether the next hop's *radio* acknowledged the frame. It is "failed"
        when the frame never arrived, when the receiver is powered off, or when the reverse
        direction is down or loses the acknowledgement: so a frame can arrive and still be
        reported failed. It is delayed: by the round trip on success, by `send_fail_ms` (the
        radio's retries) on failure.
        """
        life = sender.life
        ticket = sender.begin_send()
        fail_after = self.config.send_fail_ms
        forward_delay = link.config.delay_ms
        receiver = self._nodes[dst]
        reverse = self._links[(dst, src)]
        rng = self._mac_rngs.setdefault(
            (src, dst), random.Random(f"{self.config.seed}:macack:{src}->{dst}")
        )
        to_id = NODE_IDS[dst]

        def verdict_on_arrival() -> None:
            config = reverse.config
            ack_ok = receiver.running and not config.down
            if ack_ok and config.loss > 0 and rng.random() < config.loss:
                ack_ok = False
            if ack_ok:
                self._engine.schedule(
                    config.delay_ms, lambda: sender.send_result(ticket, to_id, True, life, True)
                )
            else:
                wait = max(0, fail_after - forward_delay)
                self._engine.schedule(
                    wait, lambda: sender.send_result(ticket, to_id, False, life, receiver.running)
                )

        if delivered:
            self._engine.schedule(forward_delay, verdict_on_arrival)
        else:
            self._engine.schedule(
                fail_after, lambda: sender.send_result(ticket, to_id, False, life, False)
            )
