"""Command-line demo: run the two-node scenario and print its trace and summary."""

import argparse
from collections.abc import Sequence

from iot_backend.simulator.link import LinkConfig
from iot_backend.simulator.scenario import LINK_KEYS, ScenarioConfig, Simulation
from iot_backend.simulator.trace import format_event


def _link_key(text: str) -> tuple[str, str]:
    key = tuple(text.upper().split("-"))
    if key not in LINK_KEYS:
        raise argparse.ArgumentTypeError(f"link must be A-B or B-A, got {text!r}")
    return key  # type: ignore[return-value]


def _split(text: str) -> tuple[tuple[str, str], str]:
    link, _, value = text.partition(":")
    if not value:
        raise argparse.ArgumentTypeError(f"expected LINK:VALUE, got {text!r}")
    return _link_key(link), value


def _drop(text: str) -> tuple[tuple[str, str], frozenset[int]]:
    key, value = _split(text)
    try:
        return key, frozenset(int(n) for n in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"frame numbers must be integers: {text!r}") from None


def _loss(text: str) -> tuple[tuple[str, str], float]:
    key, value = _split(text)
    try:
        return key, float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"loss must be a number: {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m iot_backend.simulator",
        description="Run virtual node A sending readings to virtual node B.",
    )
    parser.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    parser.add_argument(
        "--duration", type=float, default=20.0, help="simulated seconds (default 20)"
    )
    parser.add_argument("--interval-ms", type=int, default=2000, help="reading interval")
    parser.add_argument("--ack-timeout-ms", type=int, default=1000)
    parser.add_argument("--delay-ms", type=int, default=20, help="one-way link delay")
    parser.add_argument(
        "--drop",
        type=_drop,
        action="append",
        default=[],
        metavar="LINK:N[,N...]",
        help="drop the Nth frame offered to a link, e.g. A-B:3 or B-A:2,4",
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
        help="deliver a corrupted 3-byte frame to B at this time",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    drops = {key: set() for key in LINK_KEYS}
    for key, numbers in args.drop:
        drops[key] |= numbers
    losses = dict(args.loss)
    links = {
        key: LinkConfig(
            delay_ms=args.delay_ms,
            loss=losses.get(key, 0.0),
            drop_frames=frozenset(drops[key]),
        )
        for key in LINK_KEYS
    }
    config = ScenarioConfig(
        seed=args.seed,
        reading_interval_ms=args.interval_ms,
        ack_timeout_ms=args.ack_timeout_ms,
        links=links,
    )

    sim = Simulation(config)
    if args.restart_a is not None:
        sim.restart_node("A", round(args.restart_a * 1000))
    if args.malformed_at is not None:
        sim.inject_frame("B", b"\x01\x02\x03", round(args.malformed_at * 1000))
    sim.run(round(args.duration * 1000))

    print(f"seed={args.seed} duration={args.duration}s (simulated time; no real waiting)")
    print(f"{'time':>9}  {'nd':<2}  {'event':<22}  {'packet':<18} details")
    for event in sim.trace.events:
        print(format_event(event))

    s = sim.summary()
    print()
    print("Summary")
    print(f"  readings sent by A ........ {s.readings_sent}")
    print(f"  delivered (B accepted) .... {s.delivered}")
    print(f"  acknowledged (A got ACK) .. {s.acknowledged}")
    print(f"  unacknowledged (timeout) .. {s.unacknowledged}")
    print(f"  unresolved ................ {s.unresolved}")
    print(f"  frames dropped by links ... {s.frames_dropped_by_link}")
    print(f"  frames rejected ........... {s.frames_rejected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
