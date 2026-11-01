"""A one-directional simulated wireless link with delay, random loss and scripted drops."""

import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace

from iot_backend.protocol import PacketIdentity
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.trace import FRAME_DROPPED_BY_LINK, Trace


@dataclass(frozen=True)
class LinkConfig:
    delay_ms: int = 20
    loss: float = 0.0  # probability 0..1 that a frame is lost
    drop_frames: frozenset[int] = frozenset()  # 1-based ordinals of frames to drop
    # If set (e.g. "DATA", "DELIVERY_ACK"), only frames of that message type are
    # counted for `drop_frames`; HELLOs and reports then do not shift the ordinals.
    drop_msg: str | None = None
    down: bool = False  # a down link loses every frame

    def __post_init__(self) -> None:
        if self.delay_ms < 0:
            raise ValueError("delay_ms must be >= 0")
        if not 0.0 <= self.loss <= 1.0:
            raise ValueError("loss must be between 0 and 1")
        if any(n < 1 for n in self.drop_frames):
            raise ValueError("drop_frames ordinals start at 1")


class Link:
    """Carries bytes from one node to another. Frames are counted as they are offered.

    Physically every pair of boards can hear each other, so the scenario creates a link for
    every ordered pair. Which neighbors a node *may* use is the nodes' own rule, not the link's.
    """

    def __init__(
        self,
        src: str,
        dst: str,
        config: LinkConfig,
        seed: int,
        engine: Engine,
        trace: Trace,
        deliver: Callable[[bytes], None],
    ) -> None:
        self.name = f"{src}->{dst}"
        self._src = src
        self.config = config
        self._engine = engine
        self._trace = trace
        self._deliver = deliver
        # Each link has its own generator, so scripting a drop on one link never changes
        # the random choices made on another.
        self._rng = random.Random(f"{seed}:link:{self.name}")
        self._offered = 0
        self.dropped: Counter[str] = Counter()  # drops by reason

    def reconfigure(self, **changes: object) -> None:
        """Change delay, loss, drop list or up/down state from now on."""
        self.config = replace(self.config, **changes)  # type: ignore[arg-type]

    def transmit(self, data: bytes, identity: PacketIdentity | None, msg: str = "") -> bool:
        """Offer a frame. True if it will arrive, False if the link lost it."""
        counted = self.config.drop_msg is None or self.config.drop_msg == msg
        if counted:
            self._offered += 1
        reason = None
        if self.config.down:
            reason = "link_down"
        elif counted and self._offered in self.config.drop_frames:
            reason = "scripted"
        elif self.config.loss > 0 and self._rng.random() < self.config.loss:
            reason = "random"
        if reason is not None:
            self.dropped[reason] += 1
            self._trace.record(
                self._engine.now_ms,
                self._src,
                FRAME_DROPPED_BY_LINK,
                identity,
                link=self.name,
                ordinal=self._offered,
                reason=reason,
            )
            return False
        self._engine.schedule(self.config.delay_ms, lambda: self._deliver(data))
        return True
