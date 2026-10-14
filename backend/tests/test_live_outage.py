"""The controller outage through the API: what a dashboard is shown while C holds the readings."""

import json

from test_live_api import Api, events_of, make_client, settle, types

from iot_backend.live import schema


def test_outage_demo_through_the_api_shows_the_link_loss_the_replay_and_the_catch_up():
    with make_client() as client:
        api = Api(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            api.step(12_000)
            settle(ws)  # normal operation
            assert api.snapshot()["gateway"]["state"] == "connected"

            assert api.post("gateway", {"attached": False}).status_code == 200
            api.step(10_000)
            during = settle(ws)
            assert types(during)[0] == "environment"
            lost = events_of(during, "gateway_link_lost")
            assert len(lost) == 1 and lost[0]["data"]["silent_for_ms"] >= 3_000
            snap = api.snapshot()
            assert snap["gateway"]["state"] == "lost"
            assert snap["gateway"]["network_view"].startswith("stale")
            # nothing from the network reached the dashboard in the meantime
            assert events_of(during, "reading_received") == []

            api.post("node", {"node": "B", "action": "stop"})
            api.step(15_000)
            blind = settle(ws)
            assert events_of(blind, "local_failover") == []  # A failed over, the laptop cannot know
            assert events_of(blind, "node_lost_contact") == []
            b = next(n for n in api.snapshot()["nodes"] if n["id"] == "B")
            assert b["status"] in ("reachable", "stale")  # never "lost" on ground truth

            assert api.post("gateway", {"attached": True}).status_code == 200
            api.step(10_000)
            after = settle(ws)
            kinds = types(after)
            assert kinds.index("gateway_link_restored") < kinds.index("replay_started")
            assert kinds.index("replay_started") < kinds.index("replay_finished")
            replayed = [
                m["data"] for m in events_of(after, "reading_received") if m["data"]["replayed"]
            ]
            assert len(replayed) >= 10 and all(r["delay_ms"] > 0 for r in replayed)
            started = events_of(after, "replay_started")[0]["data"]
            finished = events_of(after, "replay_finished")[0]["data"]
            assert started["entries"] >= len(replayed) and finished["dropped_overflow"] == 0
            # the controller learned of the failover from the replay and confirmed the route
            failover = events_of(after, "local_failover")
            assert len(failover) == 1 and failover[0]["data"]["to"] == "D"
            confirmed = [
                e
                for e in events_of(after, "route_active")
                if e["data"]["trigger"] == "local_failover_report"
            ]
            assert confirmed and confirmed[0]["data"]["path"] == ["A", "D", "C"]
            for message in after + during + blind:
                assert schema.validate_envelope(message) == [], message

        snapshot = api.snapshot()
        assert snapshot["gateway"]["state"] == "connected"
        assert snapshot["gateway"]["last_replay"]["state"] == "finished"
        assert snapshot["readings"]["replayed"] == len(replayed)
        assert snapshot["counts"]["delivered_late"]["value"] == len(replayed)
        assert snapshot["route"]["path"] == ["A", "D", "C"]
        assert schema.validate_snapshot(snapshot) == []
        text = json.dumps(snapshot["gateway"])
        assert "bridge" not in text  # C's internals are reported by C, not read


def test_the_snapshot_without_a_heartbeat_says_it_cannot_tell():
    with make_client() as client:
        api = Api(client)
        api.post("reset", {"scenario": "normal-via-b"})  # a manual scenario: no controller
        gateway = api.snapshot()["gateway"]
        assert gateway["state"] == "unmonitored"
