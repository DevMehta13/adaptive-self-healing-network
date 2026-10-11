"""Command-line demo: run a four-node scenario and print its trace, observed paths and summary."""

import argparse
from collections.abc import Sequence
from dataclasses import replace

from iot_backend.protocol import NODE_IDS
from iot_backend.simulator.link import LinkConfig
from iot_backend.simulator.scenario import LINK_KEYS, ScenarioConfig, Simulation
from iot_backend.simulator.scenario_file import (
    ScenarioError,
    apply_actions,
    load_scenario,
    parse_link,
)
from iot_backend.simulator.trace import (
    ACK_RECEIVED,
    FRAME_SENT,
    READING_LOST,
    format_event,
    format_identity,
    observed_paths,
)

DROP_REASONS = (
    "scripted",
    "random",
    "link_down",
    "ttl_expired",
    "queue_full",
    "no_route",
    "not_permitted_neighbor",
    "node_stopped",
)


def _link_value(text: str) -> tuple[tuple[str, str], str]:
    link, _, value = text.partition(":")
    if not value:
        raise argparse.ArgumentTypeError(f"expected LINK:VALUE, got {text!r}")
    try:
        return parse_link(link), value
    except ScenarioError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _drop(text: str) -> tuple[tuple[str, str], frozenset[int]]:
    key, value = _link_value(text)
    try:
        return key, frozenset(int(n) for n in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"frame numbers must be integers: {text!r}") from None


def _loss(text: str) -> tuple[tuple[str, str], float]:
    key, value = _link_value(text)
    try:
        return key, float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"loss must be a number: {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m iot_backend.simulator",
        description=(
            "Run virtual nodes A, B, C and D. Node A sends numbered readings to C through "
            "relays B and D. Without a scenario file, A and C use B."
        ),
    )
    parser.add_argument(
        "scenario", nargs="?", help="scenario JSON file (see scenarios/ in the repository root)"
    )
    parser.add_argument("--seed", type=int, help="random seed (default 42, or the file's)")
    parser.add_argument("--duration", type=float, help="simulated seconds (default 20)")
    parser.add_argument("--interval-ms", type=int, help="reading interval")
    parser.add_argument("--ack-timeout-ms", type=int, help="wait per attempt for an ACK")
    parser.add_argument("--delay-ms", type=int, help="one-way delay on every link")
    parser.add_argument(
        "--drop",
        type=_drop,
        action="append",
        default=[],
        metavar="LINK:N[,N...]",
        help="drop the Nth frame offered to a link, e.g. A-B:3 or B-C:2,4. Repeatable.",
    )
    parser.add_argument(
        "--loss",
        type=_loss,
        action="append",
        default=[],
        metavar="LINK:P",
        help="random loss probability 0..1 on a link, e.g. A-B:0.2",
    )
    parser.add_argument(
        "--restart-a", type=float, metavar="SECONDS", help="power-cycle A at this time"
    )
    parser.add_argument(
        "--malformed-at",
        type=float,
        metavar="SECONDS",
        help="deliver a corrupted 3-byte frame from A to B at this time",
    )
    parser.add_argument("--no-trace", action="store_true", help="print only paths and summary")
    return parser


def _build(args: argparse.Namespace) -> tuple[str, str, int, Simulation, list]:
    if args.scenario:
        spec = load_scenario(args.scenario)
        config, name, description = spec.config, spec.name, spec.description
        duration_ms, actions = spec.duration_ms, spec.actions
    else:
        config = ScenarioConfig(seed=42)
        name, description, duration_ms, actions = (
            "default",
            "A -> B -> C (manual routes)",
            20_000,
            [],
        )
    if args.seed is not None:
        config.seed = args.seed
    if args.duration is not None:
        duration_ms = round(args.duration * 1000)
    if args.interval_ms is not None:
        config.reading_interval_ms = args.interval_ms
    if args.ack_timeout_ms is not None:
        config.ack_timeout_ms = args.ack_timeout_ms

    links = dict(config.links)
    if args.delay_ms is not None:
        for key in LINK_KEYS:
            links[key] = replace(links.get(key, LinkConfig()), delay_ms=args.delay_ms)
    for key, numbers in args.drop:
        links[key] = replace(
            links.get(key, LinkConfig()),
            drop_frames=links.get(key, LinkConfig()).drop_frames | numbers,
        )
    for key, loss in args.loss:
        links[key] = replace(links.get(key, LinkConfig()), loss=loss)
    config.links = links
    return name, description, duration_ms, Simulation(config), actions


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        name, description, duration_ms, sim, actions = _build(args)
        apply_actions(sim, actions)
    except (ScenarioError, ValueError) as error:
        print(f"error: {error}")
        return 2

    if args.restart_a is not None:
        sim.restart_node("A", round(args.restart_a * 1000))
    if args.malformed_at is not None:
        sim.inject_frame("B", b"\x01\x02\x03", round(args.malformed_at * 1000), from_name="A")
    sim.run(duration_ms)

    print(f"scenario: {name} - {description}")
    print(
        f"seed={sim.config.seed} duration={duration_ms / 1000}s (simulated time; no real waiting)"
    )
    if not args.no_trace:
        print(f"{'time':>9}  {'nd':<2}  {'event':<22}  {'packet':<18} details")
        for event in sim.trace.events:
            print(format_event(event))
    if sim.trace.dropped:
        print(f"(trace limit reached: {sim.trace.dropped} oldest events not shown)")

    events = sim.trace.events
    outcome = {}
    for e in events:
        if e.event == ACK_RECEIVED:
            outcome[e.identity] = f"acknowledged after {e.info['attempts']} attempt(s)"
        elif e.event == READING_LOST:
            outcome[e.identity] = "LOST"
    print()
    print("Observed paths (DATA readings, from received-frame evidence)")
    sent = [e.identity for e in events if e.event == FRAME_SENT and e.info["msg"] == "DATA"]
    for identity in dict.fromkeys(sent):
        paths = "  ".join(">".join(p) for p in observed_paths(events, identity))
        print(
            f"  {format_identity(identity):<18} {paths:<28} {outcome.get(identity, 'unresolved')}"
        )

    s = sim.summary()
    print()
    print("Summary")
    print(f"  readings created .......... {s.readings_created}")
    print(f"  attempts (DATA sends) ..... {s.attempts}")
    print(f"  frames sent (all nodes) ... {s.frames_sent}  (forwarded by relays: {s.forwarded})")
    print(f"  unique delivered at dest .. {s.delivered_unique}")
    print(f"  duplicates at dest ........ {s.duplicates}")
    print(f"  acknowledged at source .... {s.acknowledged}")
    print(f"  lost (gave up) ............ {s.lost}")
    print(f"  unresolved ................ {s.unresolved}")
    print("  drops by reason:")
    reasons = list(DROP_REASONS) + sorted(set(s.drops) - set(DROP_REASONS))
    for reason in reasons:
        print(f"    {reason:<24} {s.drops.get(reason, 0)}")
    return 0


__all__ = ["NODE_IDS", "build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
