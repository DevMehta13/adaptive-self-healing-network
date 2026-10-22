"""The controller outage through the API: what a dashboard is shown while C holds the readings."""

import json

from test_live_api import Api, events_of, make_client, settle, types

from iot_backend.live import schema


def test_outage_demo_through_the_api_shows_the_link_loss_the_replay_and_the_catch_up():
    with make_client() as client:
        api = Api(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            first = api.snapshot()["gateway"]
            assert first["state"] == "connecting" and first["network_view"].startswith("waiting")
            api.step(12_000)
            settle(ws)  # normal operation
            assert api.snapshot()["gateway"]["state"] == "connected"
            assert api.snapshot()["gateway"]["network_view"] == "live"

            assert api.post("gateway", {"attached": False}).status_code == 200
            api.step(10_000)
            during = settle(ws)
            assert types(during)[0] == "environment"
            lost = events_of(during, "gateway_link_lost")
            assert len(lost) == 1 and lost[0]["data"]["silent_for_ms"] >= 3_000
            snap = api.snapshot()
            assert snap["gateway"]["state"] == "lost"
            assert snap["gateway"]["network_view"].startswith("stale")
            # C's buffer figures are from before the outage, and the snapshot says so
            assert snap["gateway"]["c_status_as_of_ms"] <= snap["gateway"]["since_ms"]
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
            # ...and says it is old news: the report reached C soon after B stopped (22 s),
            # long before the laptop heard of it
            report = failover[0]["data"]
            assert report["replayed"] is True
            assert 22_000 <= report["received_ms"] < 27_000 < report["sim_ms"]
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


def test_readings_dropped_at_c_are_shown_as_lost_even_though_a_counts_them_acknowledged():
    """C acknowledges a reading to A the moment it arrives, so A's counters never see a loss that
    happens later, in C's buffer. The snapshot must still say so: `lost_at_c`, taken from C's own
    status and the laptop's uplink accounting, and `lost_total`."""
    with make_client() as client:
        api = Api(client)
        assert api.post("reset", {"scenario": "buffer-overflow"}).status_code == 200
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            api.step(100_000)  # 15 s up, a 60 s outage into a 20-entry buffer, then the replay
            messages = settle(ws)
            finished = events_of(messages, "replay_finished")
            assert finished, "the replay never finished"
            lost_readings = finished[0]["data"]["dropped_readings"]
            assert lost_readings > 0  # the buffer could not hold 60 s of readings
            assert finished[0]["data"]["dropped_overflow"] >= lost_readings
            for message in messages:
                assert schema.validate_envelope(message) == [], message
        snapshot = api.snapshot()
        counts = snapshot["counts"]
        # A believes every one of these was acknowledged; only C and the laptop know they are gone
        assert counts["lost_at_c"]["value"] >= lost_readings
        assert counts["lost_at_c"]["dropped_waiting"] >= lost_readings
        assert counts["lost_total"]["value"] >= counts["lost_at_c"]["value"] > 0
        assert snapshot["gateway"]["c_buffer"]["dropped_readings"] >= lost_readings
        assert schema.validate_snapshot(snapshot) == []
