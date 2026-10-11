"""A one-directional simulated wireless link with delay, random loss and scripted drops."""

import random
from collections.abc import Callable
from dataclasses import dataclass

from iot_backend.protocol import PacketIdentity
from iot_backend.simulator.engine import Engine
from iot_backend.simulator.trace import FRAME_DROPPED_BY_LINK, Trace


@dataclass(frozen=True)
class LinkConfig:
    delay_ms: int = 20
    loss: float = 0.0  # probability 0..1 that a frame is lost
    drop_frames: frozenset[int] = frozenset()  # 1-based ordinals of frames to drop

    def __post_init__(self) -> None:
        if self.delay_ms < 0:
            raise ValueError("delay_ms must be >= 0")
        if not 0.0 <= self.loss <= 1.0:
            raise ValueError("loss must be between 0 and 1")
        if any(n < 1 for n in self.drop_frames):
            raise ValueError("drop_frames ordinals start at 1")


class Link:
    """Carries bytes from one node to another. Frames are counted as they are offered."""

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
        self._config = config
        self._engine = engine
        self._trace = trace
        self._deliver = deliver
        # Each link has its own generator, so scripting a drop on one link never changes
        # the random choices made on another.
        self._rng = random.Random(f"{seed}:link:{self.name}")
        self._offered = 0
        self.dropped = 0  # frames this link has lost (scripted or random)

    def transmit(self, data: bytes, identity: PacketIdentity | None) -> None:
        self._offered += 1
        reason = None
        if self._offered in self._config.drop_frames:
            reason = "scripted"
        elif self._config.loss > 0 and self._rng.random() < self._config.loss:
            reason = "random"
        if reason is not None:
            self.dropped += 1
            self._trace.record(
                self._engine.now_ms,
                self._src,
                FRAME_DROPPED_BY_LINK,
                identity,
                link=self.name,
                ordinal=self._offered,
                reason=reason,
            )
            return
        self._engine.schedule(self._config.delay_ms, lambda: self._deliver(data))
