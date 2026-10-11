from fastapi.testclient import TestClient

from iot_backend import __version__
from iot_backend.config import Settings
from iot_backend.main import create_app


def make_client(**overrides) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, **overrides)))


def test_health():
    response = make_client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "simulation", "version": __version__}


def test_snapshot_is_minimal_and_empty():
    body = make_client().get("/api/snapshot").json()
    assert body["v"] == 1
    assert body["mode"] == "simulation"
    assert body["backend"] == {"status": "ok", "version": __version__}
    assert body["server_time"].endswith("Z")
    assert body["last_event_id"] == 0
    assert body["nodes"] == []
    assert body["links"] == []


def test_cors_allows_configured_origin_only():
    client = make_client()
    allowed = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    other = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_cors_origins_are_configurable():
    settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test")
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]
