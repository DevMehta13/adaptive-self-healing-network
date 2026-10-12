"""Command-line demo: run a four-node scenario and print its trace, observed paths and summary."""

import argparse
import statistics
from collections.abc import Sequence
from dataclasses import replace

from iot_backend.controller import RoutingConfig, TopologyView
from iot_backend.protocol import NODE_IDS
from iot_backend.simulator.link import LinkConfig
from iot_backend.simulator.measurements import detection_measurements, recovery_measurement
from iot_backend.simulator.scenario import (
    LINK_KEYS,
    ROUTING_CONTROLLER,
    ScenarioConfig,
    Simulation,
)
from iot_backend.simulator.scenario_file import (
    ControllerHost,
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
    "flood_duplicate",
    "own_frame",
    "not_from_gateway",
    "probe_unmatched",
)
HIDDEN_BY_DEFAULT = {"HELLO", "PROBE", "PROBE_REPLY"}


def _link_value(text: str) -> tuple[tuple[str, str], str]:
    link, _, value = text.partition(":")
    if not value:
        raise argparse.ArgumentTypeError(f"expected LINK:VALUE, got {text!r}")
    try:
        return parse_link(link), value
    except ScenarioError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _drop(text: str) -> tuple[tuple[str, str], frozenset[int], str | None]:
    """LINK:N[,N...][:MSG] -- MSG (e.g. DATA) makes only that message type count."""
    head, _, msg = text.rpartition(":") if text.count(":") >= 2 else (text, "", "")
    key, value = _link_value(head)
    try:
        return key, frozenset(int(n) for n in value.split(",")), (msg.upper() or None)
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
        metavar="LINK:N[,N...][:MSG]",
        help=(
            "drop the Nth frame offered to a link, e.g. A-B:3 or B-C:2,4. Add a message type "
            "to count only those frames: A-B:3:DATA. Repeatable."
        ),
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
    parser.add_argument(
        "--measure-seeds",
        type=int,
        metavar="N",
        help=(
            "run the scenario with seeds 1..N and print recovery statistics "
            "(min / median / max, simulated time); prints no trace"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="also show HELLO frames")
    return parser


def _build(args: argparse.Namespace) -> tuple[str, str, int, Simulation, list, RoutingConfig]:
    routing = RoutingConfig()
    if args.scenario:
        spec = load_scenario(args.scenario)
        config, name, description = spec.config, spec.name, spec.description
        duration_ms, actions, routing = spec.duration_ms, spec.actions, spec.controller
    else:
        config = ScenarioConfig(seed=42)
        name, description, duration_ms, actions = (
            "default",
            "A -> B -> C (manual routes, discovery and flooded ACKs on)",
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
    for key, numbers, msg in args.drop:
        current = links.get(key, LinkConfig())
        links[key] = replace(
            current,
            drop_frames=current.drop_frames | numbers,
            drop_msg=msg if msg is not None else current.drop_msg,
        )
    for key, loss in args.loss:
        links[key] = replace(links.get(key, LinkConfig()), loss=loss)
    config.links = links
    return name, description, duration_ms, Simulation(config), actions, routing


def _spread(values: list[int | None]) -> str:
    known = [v for v in values if v is not None]
    if not known:
        return "never"
    text = f"{min(known)} / {int(statistics.median(known))} / {max(known)} ms"
    return text if len(known) == len(values) else f"{text} ({len(values) - len(known)} never)"


def _measure(args: argparse.Namespace) -> int:
    if not args.scenario:
        print("error: --measure-seeds needs a scenario file")
        return 2
    runs = []
    compute_us: list[int] = []
    for seed in range(1, args.measure_seeds + 1):
        args.seed = seed
        _, _, duration_ms, sim, actions, routing = _build(args)
        host = ControllerHost(sim, routing) if sim.config.routing == ROUTING_CONTROLLER else None
        apply_actions(sim, actions, host)
        sim.run(duration_ms)
        if host is None:
            print("error: --measure-seeds needs a scenario with routing 'controller'")
            return 2
        recovery = recovery_measurement(
            actions, sim.trace.events, host.route_events, host.topology_events
        )
        if recovery is None:
            print("error: the scenario has no scripted failure to measure")
            return 2
        runs.append(recovery)
        compute_us.extend(host.controller.stats.compute_us)
    print(f"scenario: {args.scenario}  seeds 1..{args.measure_seeds}  (SIMULATION, not hardware)")
    print(f"reference: scripted failure `{runs[0].failure}` at {runs[0].failure_ms / 1000:.3f} s")
    print("                              min / median / max")
    rows = (
        ("failure detection", [r.detection_ms for r in runs]),
        ("route active (after failure)", [r.route_active_after_ms for r in runs]),
        ("  distribution only", [r.distribution_ms for r in runs]),
        ("service recovery", [r.service_recovery_ms for r in runs]),
        ("delivery gap at C", [r.delivery_gap_ms for r in runs]),
    )
    for label, values in rows:
        print(f"  {label:<28} {_spread(values)}")
    lost = [r.readings_lost for r in runs]
    spread = f"{min(lost)} / {int(statistics.median(lost))} / {max(lost)}"
    print(f"  {'readings lost in recovery':<28} {spread}")
    if compute_us:
        print(
            f"  route computation, wall-clock CPU on this laptop (not simulated): "
            f"{min(compute_us)} / {int(statistics.median(compute_us))} / {max(compute_us)} us "
            f"over {len(compute_us)} runs"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.measure_seeds:
        try:
            return _measure(args)
        except (ScenarioError, ValueError) as error:
            print(f"error: {error}")
            return 2
    try:
        name, description, duration_ms, sim, actions, routing = _build(args)
        host = ControllerHost(sim, routing) if sim.config.routing == ROUTING_CONTROLLER else None
        apply_actions(sim, actions, host)
    except (ScenarioError, ValueError) as error:
        print(f"error: {error}")
        return 2

    if args.restart_a is not None:
        sim.restart_node("A", round(args.restart_a * 1000))
    if args.malformed_at is not None:
        sim.inject_frame("B", b"\x01\x02\x03", round(args.malformed_at * 1000), from_name="A")
    view = host.view if host else TopologyView(sim.gateway) if sim.config.discovery else None
    sim.run(duration_ms)

    print(f"scenario: {name} - {description}")
    print(
        f"seed={sim.config.seed} duration={duration_ms / 1000}s (simulated time; no real waiting)"
    )
    if not args.no_trace:
        print(f"{'time':>9}  {'nd':<2}  {'event':<22}  {'packet':<18} details")
        for event in sim.trace.events:
            if not args.verbose and event.info.get("msg") in HIDDEN_BY_DEFAULT:
                continue
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
    print(f"  dropped, no route (source). {s.dropped_no_route}")
    print(f"  unresolved ................ {s.unresolved}")
    print(f"  HELLO / PROBE / report .... {s.hellos} / {s.probes} / {s.reports}")
    print("  drops by reason:")
    reasons = list(DROP_REASONS) + sorted(set(s.drops) - set(DROP_REASONS))
    for reason in reasons:
        print(f"    {reason:<24} {s.drops.get(reason, 0)}")

    if view is not None:
        _print_controller(view, actions)
    if host is not None:
        _print_routes(sim, host, actions)
    return 0


def _route_text(state: tuple) -> str:
    epoch, version, hop = state
    return f"epoch {epoch} v{version} via {hop}" if hop else f"epoch {epoch} v{version}: NO ROUTE"


def _print_routes(sim: Simulation, host: ControllerHost, actions: list) -> None:
    print()
    print("Routes held by nodes at the end (epoch, version, next hop for C)")
    for node in "ABCD":
        state = sim.route_state(node, "C")
        print(f"  node {node}: {'none installed' if state is None else _route_text(state)}")
    print()
    print("Route events (controller)")
    for event in host.route_events:
        print(
            f"  #{event.event_id:<3} {event.time_ms / 1000:8.3f} s  {event.type:<20} {event.data}"
        )
    stats = host.controller.stats
    print(
        f"  updates sent {stats.updates_sent}, retries {stats.retries}, "
        f"acks ignored {stats.acks_ignored}, plans {stats.plans}"
    )
    recovery = recovery_measurement(
        actions, sim.trace.events, host.route_events, host.topology_events
    )
    if recovery is None:
        return
    print()
    print("Recovery measurement (simulated; reference = scripted failure time)")
    print(f"  failure ................. {recovery.failure} at {recovery.failure_ms / 1000:.3f} s")
    for label, value in (
        ("failure detected after", recovery.detection_ms),
        ("route active after", recovery.route_active_after_ms),
        ("  of which distribution", recovery.distribution_ms),
        ("service recovery after", recovery.service_recovery_ms),
        ("delivery gap at C", recovery.delivery_gap_ms),
    ):
        text = "never" if value is None else f"{value} ms"
        print(f"  {label:<24} {text}")
    print(f"  readings lost in recovery  {recovery.readings_lost}")
    if stats.compute_us:
        print(
            f"  route computation (wall-clock CPU on this laptop, not simulated time): "
            f"{len(stats.compute_us)} runs, worst {max(stats.compute_us)} us"
        )


def _print_controller(view: TopologyView, actions: list) -> None:
    snapshot = view.snapshot()
    print()
    print("Controller view (built only from reports that reached C)")
    for node in snapshot["nodes"]:
        extra = ""
        if node["status"] == "lost_contact":
            src = node["source"]
            extra = (
                f"  observed by {','.join(src['observed_by'])}: no HELLO for "
                f"{src['silent_for_ms']} ms (not proof of power loss)"
            )
        since = node["since_ms"] / 1000
        print(f"  node {node['id']}: {node['status']:<13} since {since:.3f} s{extra}")
    for link in snapshot["links"]:
        print(
            f"  link {link['a']}-{link['b']}: {link['state']:<8} usable={link['usable']}  "
            f"{link['directions']}"
        )
    print()
    print("Topology events")
    for event in view.events:
        when = event.time_ms / 1000
        print(f"  #{event.event_id:<3} {when:8.3f} s  {event.type:<22} {event.data}")
    detections = detection_measurements(actions, view.events)
    if detections:
        print()
        print("Detection measurements (simulated; reference = scripted time in the scenario)")
        for d in detections:
            what = "lost contact" if d.kind == "loss" else "regained contact"
            if d.detected_ms is None:
                print(f"  {d.node} at {d.reference_ms / 1000:.3f} s: {what} never declared")
            else:
                print(
                    f"  {d.node} at {d.reference_ms / 1000:.3f} s -> {what} declared at "
                    f"{d.detected_ms / 1000:.3f} s ({d.latency_ms / 1000:.3f} s)"
                )


__all__ = ["NODE_IDS", "build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
