"""The Phase 1 scenario: virtual A sends readings to virtual B over two directional links.

`Simulation` is the public face. Its nodes, links and counters are private: anything
that later needs to learn about the network (the controller) must do so through a
gateway interface fed by events, never by reading these objects.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from iot_backend.protocol import DEFAULT_TTL, NODE_IDS, PacketIdentity
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.link import Link, LinkConfig
from iot_backend.simulator.nodes import ReceiverNode, SourceNode
from iot_backend.simulator.trace import FRAME_DROPPED_BY_LINK, Trace

LINK_KEYS = (("A", "B"), ("B", "A"))


@dataclass
class ScenarioConfig:
    seed: int = 0
    reading_interval_ms: int = 2000
    ack_timeout_ms: int = 1000
    ttl: int = DEFAULT_TTL
    # Keyed by ("A", "B") for A->B and ("B", "A") for B->A. Missing keys use defaults.
    links: dict[tuple[str, str], LinkConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class Summary:
    readings_sent: int
    delivered: int  # B accepted the reading
    acknowledged: int  # A received the ACK
    unacknowledged: int  # A gave up waiting
    unresolved: int  # still waiting at the end, or forgotten in a restart
    frames_dropped_by_link: int
    frames_rejected: int


class Simulation:
    def __init__(self, config: ScenarioConfig | None = None) -> None:
        self.config = config or ScenarioConfig()
        unknown = set(self.config.links) - set(LINK_KEYS)
        if unknown:
            raise ValueError(f"unknown links {sorted(unknown)}; Phase 1 has only A->B and B->A")

        self._engine = Engine()
        self.trace = Trace()
        self._nodes: dict[str, SourceNode | ReceiverNode] = {}
        self._links: dict[tuple[int, int], Link] = {}

        seed = self.config.seed
        a_id, b_id = NODE_IDS["A"], NODE_IDS["B"]
        self._nodes["A"] = SourceNode(
            a_id,
            "A",
            self._engine,
            self.trace,
            self._send,
            seed,
            dst=b_id,
            reading_interval_ms=self.config.reading_interval_ms,
            ack_timeout_ms=self.config.ack_timeout_ms,
            ttl=self.config.ttl,
        )
        self._nodes["B"] = ReceiverNode(b_id, "B", self._engine, self.trace, self._send, seed)

        for src, dst in LINK_KEYS:
            link = Link(
                src,
                dst,
                self.config.links.get((src, dst), LinkConfig()),
                seed,
                self._engine,
                self.trace,
                deliver=self._make_delivery(self._nodes[dst], NODE_IDS[src]),
            )
            self._links[(NODE_IDS[src], NODE_IDS[dst])] = link

        self._nodes["A"].boot()
        self._nodes["B"].boot()

    # ---- public API ----

    @property
    def now_ms(self) -> int:
        return self._engine.now_ms

    def run(self, duration_ms: int) -> None:
        """Advance the virtual clock by `duration_ms`, processing everything due."""
        self._engine.run_until(self._engine.now_ms + duration_ms)

    def restart_node(self, name: str, at_ms: int) -> None:
        """Power-cycle a node at an absolute simulated time."""
        node = self._nodes[name]
        self._engine.schedule(max(0, at_ms - self._engine.now_ms), node.boot)

    def inject_frame(self, to_name: str, data: bytes, at_ms: int, from_name: str) -> None:
        """Make raw bytes appear at a node's radio at an absolute time (e.g. a corrupted frame)."""
        node = self._nodes[to_name]
        sender = NODE_IDS[from_name]
        self._engine.schedule(
            max(0, at_ms - self._engine.now_ms), lambda: node.receive(data, sender)
        )

    def summary(self) -> Summary:
        source, receiver = self._nodes["A"], self._nodes["B"]
        sent = source.counters.sent
        done = source.counters.acknowledged + source.counters.unacknowledged
        return Summary(
            readings_sent=sent,
            delivered=receiver.counters.delivered,
            acknowledged=source.counters.acknowledged,
            unacknowledged=source.counters.unacknowledged,
            unresolved=sent - done,
            frames_dropped_by_link=len(self.trace.of_type(FRAME_DROPPED_BY_LINK)),
            frames_rejected=source.counters.rejected + receiver.counters.rejected,
        )

    # ---- internals ----

    @staticmethod
    def _make_delivery(node: SourceNode | ReceiverNode, from_id: int) -> Callable[[bytes], None]:
        return lambda data: node.receive(data, from_id)

    def _send(self, from_id: int, to_id: int, data: bytes, identity: PacketIdentity) -> None:
        link = self._links.get((from_id, to_id))
        if link is None:  # no permitted link between these nodes
            from iot_backend.protocol import NODE_NAMES

            self.trace.record(
                self._engine.now_ms,
                NODE_NAMES[from_id],
                FRAME_DROPPED_BY_LINK,
                identity,
                reason="no_link",
            )
            return
        link.transmit(data, identity)
