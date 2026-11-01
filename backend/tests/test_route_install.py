"""Node-side route installation: versions, epochs, the gateway-only rule, explicit no-route.

The controller is not involved: ROUTE_UPDATE frames are injected as C would originate them, and
the acknowledgements are read from the gateway interface.
"""

from iot_backend.protocol import Frame, RouteUpdatePayload, encode
from iot_backend.simulator import ScenarioConfig, Simulation
from iot_backend.simulator.trace import (
    FRAME_DROPPED,
    FRAME_REJECTED,
    FRAME_SENT,
    ROUTE_INSTALLED,
    ROUTE_UPDATE_IGNORED,
    ROUTE_UPDATE_SENT,
)
from iot_backend.transports import RouteInstallAckEvent, RouteUpdateCommand

A, B, C, D = 1, 2, 3, 4
C_SESSION = 0xC0FFEE01


def make_sim(**kwargs) -> tuple[Simulation, list[RouteInstallAckEvent]]:
    config = ScenarioConfig(seed=1, routing="controller", **kwargs)
    sim = Simulation(config)
    acks: list[RouteInstallAckEvent] = []
    sim.gateway.subscribe(lambda e: acks.append(e) if isinstance(e, RouteInstallAckEvent) else None)
    return sim, acks


_seq = iter(range(1_000_000, 2_000_000))


def update(dst, epoch, version, next_hop, backup=None, destination=C, src=C) -> bytes:
    payload = RouteUpdatePayload(epoch, version, destination, next_hop, backup)
    return encode(Frame(src, dst, C_SESSION, next(_seq), 8, payload))


def send(sim, node, data, at_ms, via):
    sim.inject_frame(node, data, at_ms, via)


def installed(sim, node):
    return [e for e in sim.trace.of_type(ROUTE_INSTALLED) if e.node == node]


def ignored(sim, node):
    return [e for e in sim.trace.of_type(ROUTE_UPDATE_IGNORED) if e.node == node]


def test_nodes_start_with_no_data_routes_and_readings_are_dropped_not_queued():
    sim, _ = make_sim(discovery=False)
    sim.run(9_000)
    assert sim.route_state("A", "C") is None
    s = sim.summary()
    assert s.attempts == 0 and s.frames_sent == 0  # nothing was ever put on the air
    assert s.readings_created == s.dropped_no_route == 4  # readings at 2, 4, 6, 8 s
    assert s.drops == {"no_route": 4} and s.unresolved == 0


def test_an_update_installs_and_is_acknowledged():
    sim, acks = make_sim(discovery=False)
    send(sim, "A", update(A, 1000, 1, B), 100, "B")
    sim.run(1_000)
    assert sim.route_state("A", "C") == (1000, 1, "B")
    (ack,) = acks
    assert (ack.origin, ack.epoch, ack.version, ack.destination, ack.status) == (
        A,
        1000,
        1,
        C,
        "installed",
    )


def test_a_newer_version_replaces_and_an_older_one_never_does():
    sim, acks = make_sim(discovery=False)
    send(sim, "A", update(A, 1000, 5, B), 100, "B")
    send(sim, "A", update(A, 1000, 3, D), 300, "D")  # late, older
    send(sim, "A", update(A, 1000, 6, D), 500, "D")  # newer
    send(sim, "A", update(A, 1000, 4, B), 700, "B")  # late again
    sim.run(2_000)
    assert sim.route_state("A", "C") == (1000, 6, "D")
    assert [e.info["reason"] for e in ignored(sim, "A")] == ["stale", "stale"]
    assert [(a.status, a.epoch, a.version) for a in acks] == [
        ("installed", 1000, 5),
        ("stale", 1000, 5),  # the stale ACK carries the HELD pair, not the update's
        ("installed", 1000, 6),
        ("stale", 1000, 6),
    ]


def test_a_repeated_update_is_acknowledged_again_but_not_installed_twice():
    sim, acks = make_sim(discovery=False)
    for at in (100, 400, 700):
        send(sim, "B", update(B, 1000, 1, C), at, "C")
    sim.run(2_000)
    assert len(installed(sim, "B")) == 1
    assert [e.info["reason"] for e in ignored(sim, "B")] == ["duplicate", "duplicate"]
    assert [a.status for a in acks] == ["installed"] * 3  # the controller can always get its ACK


def test_a_new_epoch_wins_even_with_a_smaller_version_and_an_old_epoch_never_does():
    sim, _ = make_sim(discovery=False)
    send(sim, "A", update(A, 1000, 40, B), 100, "B")
    send(sim, "A", update(A, 1001, 1, D), 300, "D")  # controller restarted: versions start over
    send(sim, "A", update(A, 1000, 99, B), 500, "B")  # a late frame from the previous run
    sim.run(2_000)
    assert sim.route_state("A", "C") == (1001, 1, "D")
    assert [e.info["reason"] for e in ignored(sim, "A")] == ["stale"]


def test_epochs_and_versions_compare_across_the_32_bit_wrap():
    sim, _ = make_sim(discovery=False)
    send(sim, "A", update(A, 2**32 - 1, 2**32 - 1, B), 100, "B")
    send(sim, "A", update(A, 2**32 - 1, 1, D), 300, "D")  # version wrapped: newer
    send(sim, "A", update(A, 1, 1, B), 500, "B")  # epoch wrapped: newer
    sim.run(2_000)
    assert sim.route_state("A", "C") == (1, 1, "B")
    assert ignored(sim, "A") == []


def test_only_the_gateway_may_install_routes():
    sim, acks = make_sim(discovery=False)
    # D claims to be the controller's agent; its frame is a well-formed ROUTE_UPDATE for A
    send(sim, "A", update(A, 1000, 1, B, src=D), 100, "D")
    sim.run(1_000)
    assert sim.route_state("A", "C") is None and acks == []
    (rejected,) = [e for e in sim.trace.of_type(FRAME_REJECTED) if e.node == "A"]
    assert rejected.info["reason"] == "not_from_gateway"


def test_a_next_hop_that_is_not_a_permitted_neighbor_is_refused_and_reported():
    sim, acks = make_sim(discovery=False)
    send(sim, "A", update(A, 1000, 1, C), 100, "B")  # A may not talk to C directly
    sim.run(1_000)
    assert sim.route_state("A", "C") is None
    assert [a.status for a in acks] == ["rejected"]


def test_the_route_table_is_bounded():
    sim, acks = make_sim(discovery=False, max_routes=1)
    send(sim, "B", update(B, 1000, 1, C, destination=C), 100, "C")
    send(sim, "B", update(B, 1000, 2, A, destination=D), 300, "C")  # a second destination
    sim.run(1_000)
    assert [a.status for a in acks] == ["installed", "rejected"]


def test_ram_is_lost_on_restart():
    sim, _ = make_sim(discovery=False)
    send(sim, "A", update(A, 1000, 1, B), 100, "B")
    sim.restart_node("A", at_ms=5_000)
    sim.run(6_000)
    assert sim.route_state("A", "C") is None  # the controller must install it again


def test_a_stale_update_cannot_undo_a_newer_route_at_a_relay():
    sim, _ = make_sim(discovery=False)
    send(sim, "A", update(A, 1000, 7, B), 100, "B")
    send(sim, "B", update(B, 1000, 7, C), 100, "C")
    # a late copy of an older plan, which pointed B at A (a loop), arrives after version 7
    send(sim, "B", update(B, 1000, 4, A), 900, "C")
    sim.run(10_000)
    assert sim.route_state("B", "C") == (1000, 7, "C")
    assert sim.summary().delivered_unique >= 3  # readings keep arriving at C


def test_the_backup_field_is_stored_but_unused_in_phase_4a():
    sim, _ = make_sim(discovery=False)
    send(sim, "A", update(A, 1000, 1, B, backup=D), 100, "B")
    sim.stop_node("B", at_ms=3_000)
    sim.run(10_000)
    sent_after = [
        e
        for e in sim.trace.of_type(FRAME_SENT)
        if e.node == "A" and e.info["msg"] == "DATA" and e.time_ms > 3_000
    ]
    assert sent_after and {e.info["to"] for e in sent_after} == {"B"}  # never switched to D
    assert sim.route_state("A", "C") == (1000, 1, "B")


def test_an_explicit_no_route_drops_readings_with_a_reason_and_keeps_memory_flat():
    sim, acks = make_sim(discovery=False, reading_interval_ms=500)
    send(sim, "A", update(A, 1000, 1, B), 100, "B")
    send(sim, "A", update(A, 1000, 2, None), 5_000, "B")  # "there is no route"
    sim.run(60_000)
    assert sim.route_state("A", "C") == (1000, 2, None)
    assert [a.status for a in acks] == ["installed", "installed"]
    told = [
        e
        for e in sim.trace.of_type(FRAME_DROPPED)
        if e.node == "A" and e.info["reason"] == "no_route" and e.info.get("told_by_controller")
    ]
    s = sim.summary()
    assert len(told) == s.dropped_no_route > 100  # counted, one per reading
    assert s.unresolved <= 1 and s.attempts < s.readings_created  # no retries piled up
    # while the route existed, the frames flowed; afterwards nothing was sent
    last_data = max(
        e.time_ms
        for e in sim.trace.of_type(FRAME_SENT)
        if e.info["msg"] == "DATA" and e.node == "A"
    )
    assert last_data < 5_500


def test_route_updates_reach_a_node_by_flooding_when_the_direct_link_is_down():
    sim, acks = make_sim(discovery=False)
    sim.set_link_down("C", "B", True)  # C cannot reach B directly (nor B's ACK reach C via B)
    sim.set_link_down("B", "C", True)
    sim.gateway.send_route_update(RouteUpdateCommand(B, 1000, 1, C, C))  # the controller's command
    sim.run(1_000)
    assert sim.route_state("B", "C") == (1000, 1, "C")  # it arrived around the ring: C-D-A-B
    assert [a.origin for a in acks] == [B]  # and the acknowledgement came back the same way
    (sent,) = [e for e in sim.trace.of_type(ROUTE_UPDATE_SENT)]
    assert (sent.node, sent.info["target"]) == ("C", "B")  # C originated it for the controller
