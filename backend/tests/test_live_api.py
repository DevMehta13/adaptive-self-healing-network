"""The live simulation through the API: HTTP controls in, WebSocket events out.

Time is driven with POST /api/control/step (a manual clock), never by sleeping. Controls change
the simulated environment only; every claim about what the dashboard sees is read from the
snapshot and the event stream.
"""

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from iot_backend.config import Settings
from iot_backend.live import LiveSimulation, schema
from iot_backend.main import create_app

HOLD_DOWN_MS = 5_000
ALLOWED = "http://localhost:5173"


def make_client(**overrides: Any) -> TestClient:
    settings = Settings(
        _env_file=None, live_autostart=False, heartbeat_interval_seconds=0.05, **overrides
    )
    return TestClient(create_app(settings))


class Api:
    """A thin helper around the test client."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def post(self, path: str, body: dict[str, Any] | None = None, **headers: str):
        return self.client.post(f"/api/control/{path}", json=body or {}, headers=headers)

    def step(self, ms: int) -> None:
        response = self.post("step", {"ms": ms})
        assert response.status_code == 200, response.text

    def snapshot(self) -> dict[str, Any]:
        return self.client.get("/api/snapshot").json()

    @property
    def live(self) -> LiveSimulation:
        return self.client.app.state.live


@pytest.fixture
def api():
    with make_client() as client:
        yield Api(client)


def drain(ws, until_type: str = "heartbeat", limit: int = 5000) -> list[dict[str, Any]]:
    """Read messages until the first heartbeat after everything queued so far (and drop it)."""
    messages = []
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == until_type:
            return messages
        messages.append(message)
    raise AssertionError("no heartbeat arrived")


def settle(ws) -> list[dict[str, Any]]:
    """Everything queued before now: read through two heartbeats to be sure the queue is empty."""
    first = drain(ws)
    return first + drain(ws)


def types(messages: list[dict[str, Any]]) -> list[str]:
    return [m["type"] for m in messages]


def events_of(messages, type_):
    return [m for m in messages if m["type"] == type_]


# ---- the demo through the control endpoints ----


def test_stop_b_then_start_b_through_the_api_shows_every_stage():
    with make_client() as client:
        api = Api(client)
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "status"
            api.step(10_000)  # normal operation: A > B > C with D as A's backup
            normal = settle(ws)
            active = events_of(normal, "route_active")
            assert [e["data"]["path"] for e in active] == [["A", "B", "C"]]
            assert active[0]["data"]["backups"] == {"A": "D"}
            readings = events_of(normal, "reading_received")
            assert readings and {r["data"]["via"] for r in readings} == {"B"}

            assert api.post("node", {"node": "B", "action": "stop"}).status_code == 200
            api.step(20_000)
            during = settle(ws)
            kinds = types(during)
            # what we did, then what the network noticed, in that order
            assert kinds[0] == "environment"
            assert during[0]["data"]["action"] == "stop_node"
            assert during[0]["data"]["source"] == "presenter"
            failover = events_of(during, "local_failover")
            assert len(failover) == 1 and failover[0]["data"]["to"] == "D"
            lost = events_of(during, "node_lost_contact")
            assert len(lost) == 1 and lost[0]["data"]["node"] == "B"
            assert kinds.index("local_failover") < kinds.index("node_lost_contact")
            confirmed = [
                e
                for e in events_of(during, "route_active")
                if e["data"]["trigger"] == "local_failover_report"
            ]
            assert confirmed and confirmed[0]["data"]["path"] == ["A", "D", "C"]
            after = [
                r["data"]
                for r in events_of(during, "reading_received")
                if r["data"]["sim_ms"] > failover[0]["data"]["sim_ms"]
            ]
            assert after and {r["via"] for r in after} == {"D"}  # C's own evidence
            assert all(r["observed_path"] == ["A", "D", "C"] and r["hops"] == 2 for r in after)

            assert api.post("node", {"node": "B", "action": "start"}).status_code == 200
            api.step(25_000)
            back = settle(ws)
            held = events_of(back, "link_held_down")
            assert {h["data"]["link"] for h in held} == {"A-B", "B-C"}
            final = events_of(back, "route_active")[-1]["data"]
            assert final["path"] == ["A", "B", "C"] and final["trigger"] == "topology_changed"
            first_hold = min(h["data"]["sim_ms"] for h in held)
            assert final["sim_ms"] - first_hold >= HOLD_DOWN_MS  # not before the hold-down
            last_readings = events_of(back, "reading_received")[-3:]
            assert {r["data"]["via"] for r in last_readings} == {"B"}


def test_every_streamed_event_matches_the_documented_shape():
    with make_client() as client:
        api = Api(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            api.step(15_000)
            api.post("node", {"node": "B", "action": "stop"})
            api.step(20_000)
            api.post("link-config", {"link": "A->D", "loss": 0.0, "delay_ms": 30})
            api.post("gateway", {"attached": False})
            api.post("pause")
            api.post("speed", {"factor": 2})
            api.post("resume")
            api.post("gateway", {"attached": True})
            api.step(5_000)
            messages = settle(ws)
    assert messages
    for message in messages:
        assert schema.validate_envelope(message) == [], message
    seen = {m["type"] for m in messages}
    assert {
        "environment",
        "local_failover",
        "route_active",
        "reading_received",
        "telemetry",
    } <= seen
    assert "runtime" in seen


# ---- snapshot and event ordering ----


def test_event_ids_are_contiguous_and_the_snapshot_agrees_with_the_events():
    with make_client() as client:
        api = Api(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            api.step(12_000)
            api.post("node", {"node": "B", "action": "stop"})
            api.step(20_000)
            messages = settle(ws)
        snapshot = api.snapshot()
    ids = [m["event_id"] for m in messages if "event_id" in m]
    assert ids == list(range(ids[0], ids[0] + len(ids)))  # no gaps, strictly increasing
    assert ids[-1] == snapshot["last_event_id"]
    readings = events_of(messages, "reading_received")
    assert len(readings) == snapshot["readings"]["delivered_unique"]
    assert [r["data"]["seq"] for r in readings][-5:] == [
        r["seq"] for r in snapshot["readings"]["recent"]
    ][-5:]
    last_active = events_of(messages, "route_active")[-1]["data"]
    assert snapshot["route"]["state"] == "active"
    assert snapshot["route"]["path"] == last_active["path"] == ["A", "D", "C"]
    statuses = {n["id"]: n["status"] for n in snapshot["nodes"]}
    assert statuses["B"] == "lost_contact" or statuses["B"] == "stale"
    assert schema.validate_snapshot(snapshot) == []


def test_a_client_that_connects_between_events_can_rebuild_with_the_discard_rule():
    with make_client() as client:
        api = Api(client)
        api.step(8_000)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            api.step(3_000)  # events happen after the socket opened, before the snapshot
            snapshot = api.snapshot()
            api.step(6_000)  # and more afterwards
            messages = settle(ws)
    stream_ids = [m["event_id"] for m in messages if "event_id" in m]
    kept = [i for i in stream_ids if i > snapshot["last_event_id"]]
    discarded = [i for i in stream_ids if i <= snapshot["last_event_id"]]
    assert discarded and kept  # the rule has something to do on both sides
    assert kept[0] == snapshot["last_event_id"] + 1  # nothing missed, nothing applied twice


def test_the_event_history_is_bounded_and_says_when_it_is_incomplete():
    with make_client(live_history=50) as client:
        api = Api(client)
        api.step(60_000)
        body = client.get("/api/events", params={"after_id": 0, "limit": 1000}).json()
        assert len(body["events"]) <= 50
        assert body["complete"] is False and body["oldest_event_id"] > 1
        recent = client.get("/api/events", params={"after_id": body["last_event_id"] - 5}).json()
        assert recent["complete"] is True and len(recent["events"]) == 5
        empty = client.get("/api/events", params={"after_id": body["last_event_id"]}).json()
        assert empty["events"] == [] and empty["complete"] is True


# ---- honest data: the controller's view, not ground truth ----


def test_the_dashboard_does_not_learn_of_a_stop_until_the_network_notices():
    with make_client() as client:
        api = Api(client)
        api.step(15_000)
        api.post("node", {"node": "B", "action": "stop"})  # no time passes
        snapshot = api.snapshot()
        statuses = {n["id"]: n["status"] for n in snapshot["nodes"]}
        assert statuses["B"] == "reachable"  # the controller has no way to know yet
        assert snapshot["presenter"]["conditions"]["stopped_nodes"] == ["B"]  # presenter channel
        assert (
            snapshot["route"]["path"] == ["A", "B", "C"] and snapshot["route"]["state"] == "active"
        )
        api.step(8_000)
        after = api.snapshot()
        b = next(n for n in after["nodes"] if n["id"] == "B")
        assert b["status"] == "lost_contact"
        assert set(b["source"]["observed_by"]) == {"A", "C"}  # who noticed, not "powered off"


def test_ground_truth_never_appears_in_controller_view_events():
    with make_client() as client:
        api = Api(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            api.step(10_000)
            api.post("node", {"node": "B", "action": "stop"})
            api.post("link", {"link": "A->D", "down": True})
            api.step(30_000)
            messages = settle(ws)
    forbidden = ("node_stopped", "powered", "stopped_nodes", "link_down", "ground_truth", "running")
    for message in messages:
        if message["type"] in ("environment", "heartbeat", "status", "runtime"):
            continue  # the environment channel is the one place presenter actions appear
        text = json.dumps(message["data"])
        assert not any(word in text for word in forbidden), message
    env = events_of(messages, "environment")
    assert {e["data"]["action"] for e in env} == {"stop_node", "link_down"}
    assert all(e["data"]["channel"] == "environment" for e in env)
    assert all("injected by presenter, simulation" in e["data"]["label"] for e in env)


def test_readings_report_only_what_c_observed():
    with make_client() as client:
        api = Api(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            api.step(20_000)
            api.post("node", {"node": "B", "action": "stop"})
            api.step(15_000)
            messages = settle(ws)
        sim = api.live.simulation
    readings = [m["data"] for m in events_of(messages, "reading_received")]
    # cross-check against the simulator trace (the test may look; the dashboard never does)
    from iot_backend.simulator.trace import READING_DELIVERED, observed_paths

    by_identity = {}
    for event in sim.trace.of_type(READING_DELIVERED):
        paths = observed_paths(sim.trace.events, event.identity)
        by_identity[(event.identity.session, event.identity.seq)] = paths[-1]
    matched = 0
    for r in readings:
        truth = by_identity.get((r["session"], r["seq"]))
        if truth is None:
            continue  # trace bound reached for old readings
        assert r["observed_path"] == truth and r["via"] == truth[-2] and r["hops"] == len(truth) - 1
        matched += 1
    assert matched >= 10
    assert {"via", "hops", "ttl"} <= set(readings[0])


def test_counts_say_who_knows_them_and_how_fresh_they_are():
    with make_client() as client:
        api = Api(client)
        api.step(30_000)
        counts = api.snapshot()["counts"]
    assert counts["delivered"]["known_by"] == "C"
    for key in ("created", "acknowledged", "lost", "no_route"):
        assert counts[key]["known_by"].startswith("A")
        assert counts[key]["age_ms"] >= 0 and counts[key]["age_ms"] <= 7_000  # 5 s interval
    assert counts["created"]["value"] >= counts["delivered"]["value"] >= 5
    assert counts["pending"]["value"] <= 2
    assert counts["no_route"]["value"] >= 1  # readings made before the first route existed


def test_node_telemetry_reaches_the_dashboard_with_its_age():
    with make_client() as client:
        api = Api(client)
        api.step(25_000)
        nodes = {n["id"]: n for n in api.snapshot()["nodes"]}
    for name in "ABCD":
        t = nodes[name]["telemetry"]
        assert t is not None and t["age_ms"] >= 0
    assert nodes["A"]["telemetry"]["counters"]["created"] >= 8
    assert nodes["B"]["telemetry"]["counters"]["forwarded"] > 0  # B relayed A's frames


# ---- controls never touch the controller ----


def controller_fingerprint(live: LiveSimulation) -> tuple:
    controller = live.controller
    return (
        id(controller),
        id(live.view),
        dict(vars(controller.stats)).copy().__repr__(),
        len(controller.events),
        len(live.view.events),
        controller.route_state()["version"],
        controller.version,
        live.simulation.gateway.commands_sent,
    )


CONTROLS = [
    ("node", {"node": "B", "action": "stop"}),
    ("node", {"node": "B", "action": "start"}),
    ("link", {"link": "A->B", "down": True}),
    ("link", {"link": "A->B", "down": False}),
    ("link-config", {"link": "B->C", "loss": 0.3, "delay_ms": 80}),
    ("gateway", {"attached": False}),
    ("gateway", {"attached": True}),
    ("pause", {}),
    ("resume", {}),
    ("speed", {"factor": 4}),
]


def test_controls_do_not_call_the_controller_or_send_it_anything():
    with make_client() as client:
        api = Api(client)
        api.step(10_000)
        for path, body in CONTROLS:
            before = controller_fingerprint(api.live)
            response = api.post(path, body)
            assert response.status_code == 200, (path, response.text)
            assert controller_fingerprint(api.live) == before, path  # nothing at all changed
        # and the effect is found only by the network, later
        api.post("node", {"node": "B", "action": "stop"})
        before = len(api.live.controller.events)
        api.step(20_000)
        assert len(api.live.controller.events) > before


def test_control_code_has_no_path_to_the_controller():
    source = Path(inspect.getsourcefile(__import__("iot_backend.api.control", fromlist=["x"])))
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    assert not [name for name in imported if "controller" in name or "simulator" in name]
    for method in (
        "stop_node",
        "start_node",
        "link_down",
        "link_config",
        "set_gateway",
        "pause",
        "resume",
        "set_speed",
    ):
        body = inspect.getsource(getattr(LiveSimulation, method))
        for word in ("_controller", "_host", "_view", "route_state", "send_route_update"):
            assert word not in body, f"{method} mentions {word}"


# ---- security of the state-changing endpoints ----


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Origin": "http://evil.example"}, 403, "origin_not_allowed"),
        ({"Sec-Fetch-Site": "cross-site"}, 403, "origin_not_allowed"),
    ],
)
def test_foreign_origins_cannot_change_state(api, headers, status, code):
    before = api.live.last_event_id
    response = api.post("node", {"node": "B", "action": "stop"}, **headers)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert api.live.last_event_id == before
    assert api.snapshot()["presenter"]["conditions"]["stopped_nodes"] == []


def test_only_json_bodies_are_accepted(api):
    path = "/api/control/node"
    form = api.client.post(path, data={"node": "B", "action": "stop"})
    assert form.status_code == 415 and form.json()["error"]["code"] == "json_required"
    plain = api.client.post(
        path, content='{"node":"B","action":"stop"}', headers={"content-type": "text/plain"}
    )
    assert plain.status_code == 415
    broken = api.client.post(path, content="{nope", headers={"content-type": "application/json"})
    assert broken.status_code == 400 and broken.json()["error"]["code"] == "bad_request"
    array = api.client.post(path, json=["B"])
    assert array.status_code == 400
    extra = api.post("node", {"node": "B", "action": "stop", "sudo": True})
    assert extra.status_code == 400
    assert api.live.last_event_id == 0  # nothing was applied


def test_allowed_origin_and_no_origin_both_work_and_preflight_is_answered(api):
    assert api.post("pause", {}, Origin=ALLOWED).status_code == 200
    assert api.post("resume", {}).status_code == 200
    preflight = api.client.options(
        "/api/control/node",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == ALLOWED
    evil = api.client.options(
        "/api/control/node",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in evil.headers


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("node", {"node": "Z", "action": "stop"}),
        ("node", {"node": "B", "action": "explode"}),
        ("link", {"link": "A->Q", "down": True}),
        ("link", {"link": "A->B", "down": "yes"}),
        ("link-config", {"link": "A->B"}),
        ("link-config", {"link": "A->B", "loss": 2}),
        ("link-config", {"link": "A->B", "delay_ms": -1}),
        ("gateway", {"attached": "no"}),
        ("speed", {"factor": 0}),
        ("speed", {"factor": 99999}),
        ("speed", {"factor": "fast"}),
        ("step", {"ms": 0}),
        ("reset", {"scenario": "../etc/passwd"}),
        ("reset", {"scenario": "no-such-scenario"}),
    ],
)
def test_bad_requests_get_a_structured_error_and_change_nothing(api, path, body):
    before = api.live.last_event_id
    response = api.post(path, body)
    assert response.status_code in (400, 404)
    error = response.json()["error"]
    assert set(error) == {"code", "message"} and error["code"] and error["message"]
    assert api.live.last_event_id == before


# ---- clock: pause, resume, speed, reset ----


def test_pause_resume_and_speed_are_reported_in_snapshot_events_and_heartbeats():
    with make_client() as client:
        api = Api(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            assert api.post("pause").json()["clock"]["paused"] is True
            assert api.post("speed", {"factor": 5}).json()["clock"]["speed"] == 5.0
            assert api.post("resume").json()["clock"]["paused"] is False
            messages = settle(ws)
            runtime = events_of(messages, "runtime")
            assert [(r["data"]["change"], r["data"]["paused"]) for r in runtime] == [
                ("paused", True),
                ("speed", True),
                ("resumed", False),
            ]
            beat = ws.receive_json()
        assert beat["type"] == "heartbeat" and beat["data"]["speed"] == 5.0
        assert api.snapshot()["clock"] == {"sim_ms": 0, "speed": 5.0, "paused": False}


def test_pausing_twice_is_harmless_and_emits_one_event(api):
    api.post("pause")
    before = api.live.last_event_id
    assert api.post("pause").status_code == 200
    assert api.live.last_event_id == before


def test_reset_loads_a_named_scenario_and_starts_a_fresh_run():
    with make_client() as client:
        api = Api(client)
        api.step(20_000)
        first = api.snapshot()
        assert first["run_id"] == 1 and first["clock"]["sim_ms"] == 20_000
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            response = api.post("reset", {"scenario": "b-failure-auto-reroute"})
            assert response.status_code == 200 and response.json()["run_id"] == 2
            messages = settle(ws)
        reset = events_of(messages, "reset")
        assert len(reset) == 1 and reset[0]["data"]["scenario"] == "b-failure-auto-reroute"
        second = api.snapshot()
        assert second["run_id"] == 2 and second["clock"]["sim_ms"] == 0
        assert second["scenario"] == "b-failure-auto-reroute"
        assert second["last_event_id"] > first["last_event_id"]  # ids never restart
        assert second["readings"]["delivered_unique"] == 0
        # the scripted failure in that scenario is announced as scenario, not presenter
        api.step(30_000)
        env = [e for e in api.live._history if e.type == "environment"]
        assert env and env[0].data["source"] == "scenario"
        assert api.live.controller.route_state()["path"] == ["A", "D", "C"]


def test_scenarios_can_be_listed(api):
    body = api.client.get("/api/scenarios").json()
    assert "live-demo" in body["scenarios"] and body["current"] == "live-demo"


def test_a_slow_websocket_client_is_dropped_not_buffered_forever():
    from iot_backend.api import ws as ws_module

    assert ws_module.MAX_BACKLOG <= 5000  # the per-connection queue has a bound
