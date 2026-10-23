"""Unit tests for the relay-selection helpers of hardware_unplug_trials.py (no hardware).

    cd backend && uv run pytest ../scripts
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hardware_unplug_trials as h  # noqa: E402


def snap(path, state="active", kind="route"):
    return {"route": {"kind": kind, "state": state, "path": path}}


def test_active_relay_follows_the_route():
    assert h.active_relay(snap(["A", "B", "C"])) == "B"
    assert h.active_relay(snap(["A", "D", "C"])) == "D"


def test_no_relay_when_route_is_not_active_or_absent():
    assert h.active_relay(snap(["A", "B", "C"], state="installing")) is None
    assert h.active_relay(snap([], kind="no_route")) is None
    assert h.active_relay(snap(["A", "C"])) is None


def test_other_relay():
    assert h.other_relay("B") == "D" and h.other_relay("D") == "B"
