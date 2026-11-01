"""Discrete-event engine: a virtual clock and a queue of actions ordered by time.

Nothing here sleeps. Time is an integer number of simulated milliseconds that jumps
straight to the time of the next queued action, so a 20-second scenario runs instantly
and identically every time.
"""

import heapq
import itertools
from collections.abc import Callable

Action = Callable[[], None]


class Engine:
    def __init__(self, max_steps: int = 1_000_000) -> None:
        self._now_ms = 0
        self._queue: list[tuple[int, int, Action]] = []
        self._order = itertools.count()  # breaks ties: same-time actions run in schedule order
        self._max_steps = max_steps

    @property
    def now_ms(self) -> int:
        return self._now_ms

    def schedule(self, delay_ms: int, action: Action) -> None:
        if delay_ms < 0:
            raise ValueError("cannot schedule in the past")
        heapq.heappush(self._queue, (self._now_ms + delay_ms, next(self._order), action))

    def run_until(self, end_ms: int) -> None:
        """Run every action due at or before `end_ms`, then set the clock to `end_ms`."""
        steps = 0
        while self._queue and self._queue[0][0] <= end_ms:
            time_ms, _, action = heapq.heappop(self._queue)
            self._now_ms = time_ms
            action()
            steps += 1
            if steps > self._max_steps:
                raise RuntimeError("simulation exceeded its step limit")
        self._now_ms = max(self._now_ms, end_ms)
