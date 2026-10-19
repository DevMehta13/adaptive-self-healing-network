"""Scenario files in ../scenarios/ and the command-line runner."""

from pathlib import Path

import pytest

from iot_backend.simulator import Simulation
from iot_backend.simulator.__main__ import main
from iot_backend.simulator.scenario_file import (
    ScenarioError,
    apply_actions,
    load_scenario,
    parse_scenario,
    start_controller,
)

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"
SCENARIO_FILES = sorted(SCENARIO_DIR.glob("*.json"))


def run_scenario(name: str) -> Simulation:
    spec = load_scenario(SCENARIO_DIR / f"{name}.json")
    sim = Simulation(spec.config)
    host = start_controller(sim, spec)  # None for the manual-route scenarios
    apply_actions(sim, spec.actions, host)
    sim.run(spec.duration_ms)
    return sim


def test_the_required_scenarios_exist():
    names = {p.stem for p in SCENARIO_FILES}
    assert {
        "normal-via-b",
        "manual-switch-to-d",
        "b-stopped-no-route-change",
        "ack-loss-with-retry",
        "routing-loop-misconfiguration",
    } <= names


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda p: p.stem)
def test_every_scenario_loads_runs_and_is_repeatable(path):
    first = run_scenario(path.stem)
    second = run_scenario(path.stem)
    assert first.trace.events == second.trace.events
    assert first.summary() == second.summary()


def test_normal_via_b():
    s = run_scenario("normal-via-b").summary()
    assert (s.lost, s.duplicates, s.drops) == (0, 0, {})
    assert s.acknowledged == s.delivered_unique == 9
    assert s.forwarded == 18


def test_manual_switch_to_d():
    s = run_scenario("manual-switch-to-d").summary()
    assert s.lost == 0 and s.acknowledged == s.delivered_unique == 9


def test_b_stopped_no_route_change_loses_readings():
    s = run_scenario("b-stopped-no-route-change").summary()
    assert s.lost >= 4
    assert s.drops["node_stopped"] > 0
    assert s.acknowledged == 3  # readings at 2, 4 and 6 s, before B stopped at 7 s


def test_ack_loss_with_retry():
    s = run_scenario("ack-loss-with-retry").summary()
    assert (s.duplicates, s.lost, s.drops) == (1, 0, {"scripted": 1})
    assert s.attempts == s.readings_created + 1


def test_routing_loop_misconfiguration():
    s = run_scenario("routing-loop-misconfiguration").summary()
    assert s.drops["ttl_expired"] > 0
    assert s.delivered_unique == 0 and s.lost > 0


def test_direct_bypass_attempt():
    s = run_scenario("direct-bypass-attempt").summary()
    assert s.delivered_unique == 0
    assert s.drops["not_permitted_neighbor"] > 0


# ---- strict loader ----


def test_unknown_keys_are_errors():
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "duration_s": 1, "sed": 3})
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "duration_s": 1, "links": {"A->B": {"delay": 5}}})
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "duration_s": 1, "actions": [{"at_ms": 0, "action": "boom"}]})


def test_bad_names_are_errors():
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "duration_s": 1, "links": {"A->Z": {}}})
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "duration_s": 1, "routes": {"A": {"C": "Q"}}})


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(ScenarioError):
        load_scenario(tmp_path / "nope.json")


# ---- command line ----


def test_cli_runs_a_scenario_file_and_prints_paths_and_drop_table(capsys):
    assert main([str(SCENARIO_DIR / "ack-loss-with-retry.json")]) == 0
    out = capsys.readouterr().out
    assert "Observed paths" in out
    assert "A>B>C  A>B>C" in out  # the retried reading shows two attempts
    assert "acknowledged after 2 attempt(s)" in out
    assert "duplicates at dest ........ 1" in out
    for reason in (
        "scripted",
        "random",
        "link_down",
        "ttl_expired",
        "queue_full",
        "no_route",
        "not_permitted_neighbor",
        "node_stopped",
    ):
        assert reason in out


def test_cli_phase1_style_flags_still_work(capsys):
    assert main(["--seed", "42", "--duration", "5", "--drop", "A-B:1"]) == 0
    out = capsys.readouterr().out
    assert "frame_dropped_by_link" in out
    assert "readings created .......... 2" in out


def test_cli_reports_a_bad_scenario(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"name": "x", "duration_s": 1, "oops": 1}')
    assert main([str(bad)]) == 2
    assert "error:" in capsys.readouterr().out
