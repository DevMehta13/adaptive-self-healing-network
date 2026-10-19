"""Link quality as the controller estimates it from topology reports (Phase 9).

Two estimates per link, both built only from what nodes report:

* **Smoothed RTT.** Every report that carries a measured round trip for a neighbor feeds an
  exponentially weighted moving average, `new = alpha * sample + (1 - alpha) * old`, with
  `alpha` 0.2 to begin with. The first sample is the starting value. What nodes report is
  already the median of their last five probe round trips, so this is smoothing on top of a
  median: slow to follow a change on purpose.
* **Delivery probability.** A node reports, per neighbor, how many of its last (up to 20)
  completed probes were answered. The estimate is answered / completed. A probe is a round trip,
  so this is the chance that a probe *and* its reply both got through, not the chance of one
  frame; it ranks links correctly when both directions behave alike.

Each direction (reporter, neighbor) is kept separately and a link combines its two ends the
same careful way link cost always has: the larger RTT, the smaller delivery probability, and
nothing at all unless **both** ends have reported. A missing estimate is never turned into a
number. A reporter that restarted, or that now lists the neighbor as lost, starts over: its
old history says nothing about the link as it is now.
"""

from dataclasses import dataclass

from iot_backend.transports.gateway import NeighborObservation

DEFAULT_EWMA_ALPHA = 0.2


@dataclass
class _End:
    """What one reporter says about the link to one neighbor."""

    ewma_rtt_ms: float | None = None
    probes_ok: int | None = None
    probes_total: int | None = None


class LinkQuality:
    def __init__(self, alpha: float = DEFAULT_EWMA_ALPHA) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("the smoothing factor must be in (0, 1]")
        self.alpha = alpha
        self._ends: dict[tuple[int, int], _End] = {}  # (reporter, neighbor)
        self._sessions: dict[int, int] = {}  # reporter -> session its estimates belong to

    def observe(
        self, origin: int, session: int, neighbors: tuple[NeighborObservation, ...]
    ) -> None:
        """Fold one accepted topology report in."""
        if self._sessions.get(origin) != session:
            for key in [k for k in self._ends if k[0] == origin]:
                del self._ends[key]  # a restarted reporter forgot everything it knew
            self._sessions[origin] = session
        listed = set()
        for entry in neighbors:
            key = (origin, entry.neighbor)
            listed.add(entry.neighbor)
            if entry.state == "lost":
                self._ends.pop(key, None)
                continue
            end = self._ends.setdefault(key, _End())
            if entry.rtt_ms is None:
                end.ewma_rtt_ms = None  # the measurement aged out: unmeasured, not "unchanged"
            elif end.ewma_rtt_ms is None:
                end.ewma_rtt_ms = float(entry.rtt_ms)
            else:
                end.ewma_rtt_ms = self.alpha * entry.rtt_ms + (1 - self.alpha) * end.ewma_rtt_ms
            end.probes_ok, end.probes_total = entry.probes_ok, entry.probes_total
        for key in [k for k in self._ends if k[0] == origin and k[1] not in listed]:
            del self._ends[key]

    def smoothed_rtt_ms(self, a: int, b: int) -> int | None:
        """The link's smoothed round trip: the larger of its two ends, or None unless both exist."""
        first, second = self._ends.get((a, b)), self._ends.get((b, a))
        if first is None or second is None:
            return None
        if first.ewma_rtt_ms is None or second.ewma_rtt_ms is None:
            return None
        return round(max(first.ewma_rtt_ms, second.ewma_rtt_ms))

    def delivery_probability(self, a: int, b: int) -> float | None:
        """The link's delivery probability: the lower of its two ends, or None unless both exist."""
        first, second = self._ends.get((a, b)), self._ends.get((b, a))
        if first is None or second is None:
            return None
        values = []
        for end in (first, second):
            if end.probes_total is None or end.probes_ok is None:
                return None
            values.append(end.probes_ok / end.probes_total)
        return min(values)

    def probe_counts(self, a: int, b: int) -> dict[int, tuple[int, int] | None]:
        """Each end's (answered, completed), for the dashboard; None where not reported."""
        out: dict[int, tuple[int, int] | None] = {}
        for reporter, neighbor in ((a, b), (b, a)):
            end = self._ends.get((reporter, neighbor))
            has = end is not None and end.probes_total is not None and end.probes_ok is not None
            out[reporter] = (end.probes_ok, end.probes_total) if has else None  # type: ignore[union-attr,assignment]
        return out
