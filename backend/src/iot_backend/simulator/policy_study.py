"""Phase 9 study: the two routing policies on the same lossy / reliable network.

Everything here is simulation. The loss rates and delays are configured values in the scenario
file, so the table shows what each policy does in *that* network, not what real radios do.
The same seeds and the same network are used for each policy; only the policy differs.
"""

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from iot_backend.simulator.studies import run_scenario
from iot_backend.simulator.trace import ACK_RECEIVED, READING_DELIVERED, READING_LOST

COMPARED_POLICIES = ("lowest_delay", "highest_reliability")


@dataclass(frozen=True)
class PolicyRun:
    """One simulated run under one policy. Readings are judged from the simulator's own trace."""

    seed: int
    policy: str
    delivered: int  # readings accepted at C, each once
    lost: int  # readings the source gave up on that C never received
    delivery_times_ms: tuple[int, ...]  # first attempt to ACK, for readings that were acknowledged
    route_changes: int  # the planned path became a different path (the first route is not one)
    first_route_ms: int | None  # when the first route became active
    trace_dropped: int  # trace events lost to its bound (0 means every figure is complete)

    @property
    def resolved(self) -> int:
        """Readings with a known outcome. A reading still in flight at the end is left out."""
        return self.delivered + self.lost

    @property
    def delivery_rate(self) -> float:
        return self.delivered / self.resolved if self.resolved else 0.0


def policy_run(
    path: str | Path, seed: int, policy: str, duration_ms: int | None = None
) -> PolicyRun:
    run = run_scenario(path, seed, duration_ms=duration_ms, policy=policy)
    trace = run.sim.trace
    delivered_ids = {e.identity for e in trace.of_type(READING_DELIVERED)}
    lost_ids = {e.identity for e in trace.of_type(READING_LOST)} - delivered_ids
    times = tuple(int(e.info["delivery_time_ms"]) for e in trace.of_type(ACK_RECEIVED))
    paths = [e.data["path"] for e in run.host.route_events if e.type == "route_computed"]
    changes = sum(1 for before, after in zip(paths, paths[1:], strict=False) if before != after)
    active = [e.time_ms for e in run.host.route_events if e.type == "route_active"]
    return PolicyRun(
        seed=seed,
        policy=policy,
        delivered=len(delivered_ids),
        lost=len(lost_ids),
        delivery_times_ms=times,
        route_changes=changes,
        first_route_ms=active[0] if active else None,
        trace_dropped=trace.dropped,
    )


def compare_policies(
    path: str | Path, seeds: Iterable[int], duration_ms: int | None = None
) -> dict[str, list[PolicyRun]]:
    """The same scenario and seeds under each policy. Nothing else differs between the runs."""
    seeds = list(seeds)
    return {
        policy: [policy_run(path, seed, policy, duration_ms) for seed in seeds]
        for policy in COMPARED_POLICIES
    }


@dataclass(frozen=True)
class PolicySummary:
    policy: str
    runs: int
    delivered: int
    lost: int
    delivery_rate: float  # pooled over all runs: delivered / (delivered + lost)
    rate_min: float  # the worst and best single run
    rate_max: float
    time_mean_ms: float
    time_median_ms: float
    time_p95_ms: float
    lost_per_run_mean: float
    lost_per_run_max: int
    route_changes_total: int
    route_changes_per_run_mean: float
    route_changes_per_run_max: int
    first_route_median_ms: float | None
    trace_dropped: int


def _percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]) if ordered else 0.0


def summarize_policy(runs: list[PolicyRun]) -> PolicySummary:
    times = [t for r in runs for t in r.delivery_times_ms]
    delivered = sum(r.delivered for r in runs)
    lost = sum(r.lost for r in runs)
    firsts = [r.first_route_ms for r in runs if r.first_route_ms is not None]
    return PolicySummary(
        policy=runs[0].policy,
        runs=len(runs),
        delivered=delivered,
        lost=lost,
        delivery_rate=delivered / (delivered + lost) if delivered + lost else 0.0,
        rate_min=min(r.delivery_rate for r in runs),
        rate_max=max(r.delivery_rate for r in runs),
        time_mean_ms=statistics.fmean(times) if times else 0.0,
        time_median_ms=statistics.median(times) if times else 0.0,
        time_p95_ms=_percentile(times, 0.95),
        lost_per_run_mean=lost / len(runs),
        lost_per_run_max=max(r.lost for r in runs),
        route_changes_total=sum(r.route_changes for r in runs),
        route_changes_per_run_mean=sum(r.route_changes for r in runs) / len(runs),
        route_changes_per_run_max=max(r.route_changes for r in runs),
        first_route_median_ms=statistics.median(firsts) if firsts else None,
        trace_dropped=sum(r.trace_dropped for r in runs),
    )


def format_comparison(scenario: str, results: dict[str, list[PolicyRun]], duration_s: float) -> str:
    """The comparison as text, the form the CLI prints and STATUS.md quotes."""
    summaries = {name: summarize_policy(runs) for name, runs in results.items()}
    names = list(summaries)
    runs = summaries[names[0]].runs
    width = 24
    lines = [
        f"scenario: {scenario}  seeds 1..{runs}  {duration_s:g} s per run "
        "(SIMULATION, not hardware)",
        "the same seeds and the same network under each policy; only the policy differs",
        "",
        f"{'':<42}" + "".join(f"{n:>{width}}" for n in names),
    ]

    def row(label: str, getter) -> None:  # noqa: ANN001
        lines.append(f"{label:<42}" + "".join(f"{getter(summaries[n]):>{width}}" for n in names))

    row("readings delivered (sum of runs)", lambda s: s.delivered)
    row("readings lost (sum of runs)", lambda s: s.lost)
    row("delivery rate, pooled", lambda s: f"{s.delivery_rate:.2%}")
    row("delivery rate, worst / best run", lambda s: f"{s.rate_min:.1%} / {s.rate_max:.1%}")
    row(
        "readings lost per run, mean (max)",
        lambda s: f"{s.lost_per_run_mean:.2f} ({s.lost_per_run_max})",
    )
    row("delivery time, mean (ms)", lambda s: f"{s.time_mean_ms:.0f}")
    row(
        "delivery time, median / 95th pct (ms)",
        lambda s: f"{s.time_median_ms:.0f} / {s.time_p95_ms:.0f}",
    )
    row(
        "route changes per run, mean (max)",
        lambda s: f"{s.route_changes_per_run_mean:.2f} ({s.route_changes_per_run_max})",
    )
    row(
        "first route active, median (ms)",
        lambda s: "-" if s.first_route_median_ms is None else f"{s.first_route_median_ms:.0f}",
    )
    paired = list(zip(*(results[n] for n in names), strict=True))
    better = sum(1 for a, b in paired if b.delivery_rate > a.delivery_rate)
    equal = sum(1 for a, b in paired if b.delivery_rate == a.delivery_rate)
    lines += [
        "",
        f"per seed, {names[1]} against {names[0]}: higher delivery rate in {better}, "
        f"equal in {equal}, lower in {len(paired) - better - equal} of {len(paired)} runs",
        "delivery time = first attempt to the acknowledgement of an acknowledged reading "
        "(retries included); a lost reading has no time",
    ]
    dropped = sum(s.trace_dropped for s in summaries.values())
    if dropped:
        lines.append(f"warning: the trace bound dropped {dropped} events; figures are incomplete")
    return "\n".join(lines)
