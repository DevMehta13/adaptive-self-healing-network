"""The real serial path end to end, without boards.

The simulated network's node C speaks the serial line format into memory and a SerialGateway
parses it, exactly as on hardware; the controller on top is unchanged. Selected scenarios must
give the same results as the direct gateway, a short outage must lose nothing silently, and
faults on the byte stream (corrupted, truncated, interleaved and over-long lines, a disconnect
in the middle of a line) must be counted and never applied.
"""

import ast
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from iot_backend.simulator import Simulation
from iot_backend.simulator.scenario_file import ControllerHost, apply_actions, load_scenario
from iot_backend.simulator.trace import READING_DELIVERED
from iot_backend.transports import UplinkLossEvent
from iot_backend.transports.serial_gateway import SerialGateway

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"
SELECTED = [
    "auto-initial-route",
    "b-failure-auto-reroute",
    "local-failover-b-stops",
    "controller-outage-replay",
]


@dataclass
class Run:
    sim: Simulation
    host: ControllerHost
    records: list = field(default_factory=list)
    losses: list = field(default_factory=list)

    def readings(self) -> list[tuple[int, int, int]]:
        return [(r.origin, r.session, r.seq) for r in self.records]

    def made_at_c(self, start=0, end=10**12) -> set[tuple[int, int, int]]:
        return {
            (e.identity.src, e.identity.session, e.identity.seq)
            for e in self.sim.trace.of_type(READING_DELIVERED)
            if e.node == "C" and start <= e.time_ms <= end
        }


def build(name: str, transport: str, actions: list[dict] | None = None, **settings: Any) -> Run:
    spec = load_scenario(SCENARIOS / f"{name}.json")
    spec.config.transport = transport
    for key, value in settings.items():
        setattr(spec.config, key, value)
    sim = Simulation(spec.config)
    run = Run(sim, None)  # type: ignore[arg-type]

    def on_start(view, controller, session, ledger):
        ledger.subscribe(run.records.append)

    run.host = ControllerHost(sim, spec.controller, on_start=on_start)
    sim.gateway.subscribe(
        lambda e: run.losses.append(e) if isinstance(e, UplinkLossEvent) else None
    )
    apply_actions(sim, spec.actions if actions is None else actions, run.host)
    return run


def outcome(run: Run) -> dict[str, Any]:
    """Everything that must not depend on how C's messages reach the laptop."""
    return {
        "trace": [
            (e.time_ms, e.node, e.event, e.identity, sorted(e.info.items()))
            for e in run.sim.trace.events
        ],
        "routes": [(e.type, sorted(e.data.items(), key=str)) for e in run.host.route_events],
        "topology": [(e.type, sorted(e.data.items(), key=str)) for e in run.host.topology_events],
        "readings": [
            (r.origin, r.session, r.seq, r.via, r.hops, r.received_ms, r.replayed, r.delay_ms)
            for r in run.records
        ],
    }


@pytest.mark.parametrize("name", SELECTED)
def test_a_scenario_gives_the_same_results_through_the_serial_format(name):
    direct = build(name, "direct")
    serial = build(name, "serial")
    duration = load_scenario(SCENARIOS / f"{name}.json").duration_ms
    direct.sim.run(duration)
    serial.sim.run(duration)
    assert isinstance(serial.sim.gateway, SerialGateway)
    assert outcome(serial) == outcome(direct)
    lines = serial.sim.gateway.lines
    assert lines["message"] > 20 and not [k for k in lines if k.startswith("rejected")]
    assert serial.sim.link.down_lines["message"] > 0  # C parsed the laptop's lines too
    assert serial.records  # readings really travelled as text


@pytest.mark.parametrize("transport", ["direct", "serial"])
def test_a_one_second_outage_loses_nothing_silently(transport):
    """Phase 5b's open gap: too short for C's 3 s timeout, so C never knew. Now the laptop sees
    the missing numbers and asks for them."""
    actions = [
        {"at_ms": 15_000, "action": "detach_gateway"},
        {"at_ms": 16_000, "action": "attach_gateway"},
    ]
    run = build("controller-outage-replay", transport, actions, reading_interval_ms=200)
    run.sim.run(30_000)
    made = run.made_at_c(14_000, 17_000)
    got = set(run.readings())
    assert len(made) >= 10 and made <= got  # every reading made around the outage arrived
    assert len(run.readings()) == len(got)  # once
    assert run.sim.c_bridge.counters.outages == 0  # C never noticed
    assert run.sim.gateway.stats.resend_requests >= 1  # the laptop noticed
    assert not run.losses


@pytest.mark.parametrize("seconds", [1, 2, 3, 4])
def test_short_outages_of_every_length_deliver_everything_once(seconds):
    actions = [
        {"at_ms": 15_000, "action": "detach_gateway"},
        {"at_ms": 15_000 + seconds * 1000, "action": "attach_gateway"},
    ]
    run = build("controller-outage-replay", "direct", actions, reading_interval_ms=200)
    run.sim.run(35_000)
    assert run.made_at_c() == set(run.readings()) and len(run.readings()) == len(run.made_at_c())


# ---- faults on the byte stream ----


def corrupting(rate: float, seed: int = 1):
    rng = random.Random(seed)

    def fault(data: bytes) -> bytes:
        if data.startswith(b"{") and rng.random() < rate:
            i = rng.randrange(len(data) - 1)
            return data[:i] + bytes([data[i] ^ 0x04]) + data[i + 1 :]
        return data

    return fault


def watched(**settings: Any) -> Run:
    """A run as on hardware: heartbeat on, C keeps what it sent until acknowledged."""
    return build("controller-outage-replay", "serial", [], reading_interval_ms=500, **settings)


def test_corrupted_lines_are_dropped_counted_and_their_content_recovered():
    run = watched()
    run.sim.link.fault = corrupting(0.1)
    run.sim.run(40_000)
    rejected = sum(v for k, v in run.sim.gateway.lines.items() if k.startswith("rejected"))
    assert rejected > 10
    assert run.sim.gateway.stats.resend_requests > 0
    # nothing was lost or doubled: every reading C accepted reached the controller once
    assert run.made_at_c(0, 38_000) <= set(run.readings())
    assert len(run.readings()) == len(set(run.readings()))
    assert not run.losses
    assert run.host.controller.route_state()["state"] == "active"


def test_without_a_heartbeat_c_keeps_nothing_and_the_losses_are_still_counted():
    """The Phase 4a scenarios run C without heartbeat watching (nothing is held). A corrupted
    line is then gone for good, and the laptop says so instead of skipping it."""
    run = build("b-failure-auto-reroute", "serial", reading_interval_ms=500)
    run.sim.link.fault = corrupting(0.3)
    run.sim.run(30_000)
    missing = run.made_at_c(0, 25_000) - set(run.readings())
    assert missing  # some readings really were lost
    counted = sum(e.count for e in run.losses)
    assert counted >= len(missing)
    assert all(e.reason == "gone_at_c" for e in run.losses)
    assert sum(e.unknown for e in run.losses) == counted  # C kept nothing, so it cannot say


def test_truncated_lines_are_refused_whole():
    run = watched()
    rng = random.Random(4)

    def cut(data: bytes) -> bytes:
        if data.startswith(b"{") and rng.random() < 0.1:
            return data[: rng.randrange(1, len(data) - 1)] + b"\n"  # the rest was lost
        return data

    run.sim.link.fault = cut
    run.sim.run(40_000)
    reasons = {k for k in run.sim.gateway.lines if k.startswith("rejected")}
    assert reasons and reasons <= {"rejected:no_checksum", "rejected:bad_checksum"}
    assert run.made_at_c(0, 38_000) <= set(run.readings())
    assert not run.losses


def test_interleaved_log_and_noise_lines_change_nothing():
    clean = build("local-failover-b-stops", "serial")
    noisy = build("local-failover-b-stops", "serial")

    def chatter(data: bytes) -> bytes:
        return b"# a log line from the firmware\n ets Jan  8 2013,rst cause:2\n" + data

    noisy.sim.link.fault = chatter
    clean.sim.run(30_000)
    noisy.sim.run(30_000)
    assert outcome(noisy) == outcome(clean)
    lines = noisy.sim.gateway.lines
    assert lines["log"] > lines["noise"] > 50  # C's own log lines as well
    assert len(noisy.sim.gateway.c_log) == 50  # kept for people, bounded


def test_a_burst_longer_than_any_buffer_is_dropped_and_the_stream_recovers():
    run = watched()
    run.sim.run(10_000)
    run.sim.gateway.feed(b"Z" * 100_000)  # a runaway print with no newline...
    run.sim.run(10_500)  # ...that swallows the next real line too
    run.sim.gateway.feed(b"\n")
    run.sim.run(30_000)
    assert run.sim.gateway.lines["rejected:too_long"] == 1
    assert len(run.sim.gateway._splitter._buffer) < 640  # memory stayed bounded
    assert run.made_at_c(0, 28_000) <= set(run.readings())  # what it swallowed came again
    assert not run.losses


def test_a_disconnect_in_the_middle_of_a_line_is_counted_and_forgotten():
    run = watched()
    run.sim.run(10_000)
    run.sim.gateway.feed(b'{"v":1,"t":"st","b":1')  # the cable went in the middle of a line
    run.sim.gateway.port_closed()
    assert run.sim.gateway.lines["rejected:truncated"] == 1
    run.sim.run(20_000)
    assert run.made_at_c(0, 18_000) <= set(run.readings())


def test_corrupted_lines_to_c_are_refused_by_c():
    run = watched()
    device, gateway = run.sim.link, run.sim.gateway
    rng = random.Random(8)
    original = gateway._write

    def corrupt_down(data: bytes) -> None:
        if rng.random() < 0.2:
            i = rng.randrange(len(data) - 1)
            data = data[:i] + bytes([data[i] ^ 0x08]) + data[i + 1 :]
        original(data)

    gateway._write = corrupt_down
    run.sim.run(40_000)
    assert device.down_lines["rejected"] > 0
    assert run.host.controller.route_state()["state"] == "active"  # retries got through
    assert run.made_at_c(0, 38_000) <= set(run.readings())


# ---- the controller is the same code whatever the gateway ----


def test_the_controller_imports_only_the_gateway_interface():
    import iot_backend.controller as controller_pkg

    for path in Path(controller_pkg.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("iot_backend.simulator"), path.name
                if node.module.startswith("iot_backend.transports"):
                    # the interface and its event types, nothing about how they travel
                    allowed = ("iot_backend.transports", "iot_backend.transports.gateway")
                    assert node.module in allowed, (path.name, node.module)
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("serial") for a in node.names), path.name
