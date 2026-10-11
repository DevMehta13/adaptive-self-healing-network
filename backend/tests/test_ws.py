import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from iot_backend.config import Settings
from iot_backend.main import create_app

ENVELOPE_KEYS = {"v", "seq", "ts", "type", "data"}


def make_client() -> TestClient:
    # A short interval keeps the tests fast.
    return TestClient(create_app(Settings(_env_file=None, heartbeat_interval_seconds=0.05)))


def test_status_event_first_with_envelope():
    with make_client().websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert set(msg) == ENVELOPE_KEYS
    assert msg["v"] == 1
    assert msg["seq"] == 1
    assert msg["type"] == "status"
    assert msg["ts"].endswith("Z")
    assert msg["data"]["backend"] == "ok"
    assert msg["data"]["mode"] == "simulation"
    assert msg["data"]["snapshot_required"] is True
    assert msg["data"]["heartbeat_interval_s"] == 0.05


def test_heartbeats_follow_with_increasing_seq():
    with make_client().websocket_connect("/ws") as ws:
        messages = [ws.receive_json() for _ in range(4)]
    assert [m["type"] for m in messages] == ["status", "heartbeat", "heartbeat", "heartbeat"]
    assert [m["seq"] for m in messages] == [1, 2, 3, 4]
    for m in messages[1:]:
        assert set(m) == ENVELOPE_KEYS
        assert m["data"] == {"interval_s": 0.05}


def test_foreign_origin_is_rejected():
    with pytest.raises(WebSocketDisconnect):
        with make_client().websocket_connect("/ws", headers={"Origin": "http://evil.example"}):
            pass


def test_allowed_origin_is_accepted():
    with make_client().websocket_connect("/ws", headers={"Origin": "http://localhost:5173"}) as ws:
        assert ws.receive_json()["type"] == "status"


def test_client_messages_do_not_speed_up_heartbeats():
    interval = 0.3
    client = TestClient(create_app(Settings(_env_file=None, heartbeat_interval_seconds=interval)))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # status
        started = time.monotonic()
        for _ in range(5):
            ws.send_text("ping")
        heartbeat = ws.receive_json()
        elapsed = time.monotonic() - started
    assert heartbeat["type"] == "heartbeat"
    assert elapsed >= interval * 0.8


def test_seq_restarts_on_new_connection_and_asks_for_snapshot():
    client = make_client()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert ws.receive_json()["seq"] == 2
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
    assert first["seq"] == 1
    assert first["type"] == "status"
    assert first["data"]["snapshot_required"] is True
