"""Hardware mode against the fake C device, over a real socket, in real time.

The backend opens `socket://127.0.0.1:<port>` through pyserial exactly as it would open COM3,
and the fake device (the simulated network behind the serial line format) answers. No boards
are involved: this checks the laptop's side of the serial path, not radio or USB behaviour.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from iot_backend.config import Settings
from iot_backend.fake_c import FakeCServer
from iot_backend.main import create_app

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


def wait_until(check, timeout_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def fake():
    server = FakeCServer(SCENARIOS / "live-demo.json", port=0, echo=lambda text: None)
    server.start_in_thread()
    yield server
    server.stop()


def test_hardware_mode_runs_the_controller_over_the_serial_port_and_survives_unplugging(fake):
    settings = Settings(
        _env_file=None, mode="hardware", serial_port=f"socket://127.0.0.1:{fake.port}"
    )
    with TestClient(create_app(settings)) as client:
        snap = lambda: client.get("/api/snapshot").json()  # noqa: E731

        # the controller installs a route through the serial line, and readings come back
        wait_until(lambda: snap()["route"]["state"] == "active", 15, "an active route")
        wait_until(lambda: snap()["readings"]["delivered_unique"] >= 2, 15, "readings")
        s = snap()
        assert s["mode"] == "hardware" and s["label"].startswith("HARDWARE")
        assert s["gateway"]["state"] == "connected" and s["gateway"]["serial"]["state"] == "open"
        assert s["route"]["path"] in (["A", "B", "C"], ["A", "D", "C"])
        assert not s["gateway"]["serial"]["lines"]["rejected"]
        assert (
            client.post("/api/control/node", json={"node": "B", "action": "stop"}).status_code
            == 409
        )

        # unplug C: it loses power and the port disappears
        fake.call(lambda: fake.command("unplug"))
        wait_until(lambda: snap()["gateway"]["state"] == "lost", 10, "the link declared lost")
        assert snap()["gateway"]["serial"]["state"] in ("closed", "waiting")
        assert "available_ports" in snap()["gateway"]["serial"]

        # plug it back: a new boot of C; the backend reopens the port by itself
        fake.call(lambda: fake.command("replug"))
        wait_until(lambda: snap()["gateway"]["serial"]["state"] == "open", 10, "the port again")
        wait_until(lambda: snap()["gateway"]["state"] == "connected", 10, "the link restored")
        events = client.get("/api/events?limit=1000").json()["events"]
        kinds = [e["type"] for e in events]
        assert "gateway_link_lost" in kinds and "gateway_link_restored" in kinds
        assert kinds.count("serial_port") >= 2  # closed, then open again
        restart = [e["data"] for e in events if e["type"] == "uplink_loss"]
        assert restart and restart[-1]["reason"] == "c_restarted" and restart[-1]["lower_bound"]
        assert snap()["gateway"]["uplink"]["c_restarts"] == 1
        # and the network works again through the new boot of C
        before = snap()["readings"]["delivered_unique"]
        wait_until(lambda: snap()["readings"]["delivered_unique"] > before, 20, "new readings")
