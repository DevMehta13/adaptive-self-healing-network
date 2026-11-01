"""The live runtime itself: pacing against the wall clock, determinism, bounded memory."""

import asyncio
from pathlib import Path

from iot_backend.live import LiveConfig, LiveSimulation

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


def make_live(**overrides) -> LiveSimulation:
    return LiveSimulation(LiveConfig(scenarios_dir=SCENARIOS, **overrides))


def test_the_clock_only_moves_when_it_is_told_to():
    live = make_live()
    assert live.sim_ms == 0
    live.advance(1_500)
    assert live.sim_ms == 1_500
    live.advance(0)
    live.advance(-5)
    assert live.sim_ms == 1_500


def test_stepping_in_small_pieces_equals_one_big_step():
    big = make_live()
    big.advance(30_000)
    small = make_live()
    for _ in range(300):
        small.advance(100)
    assert [(e.type, e.data) for e in big._history] == [(e.type, e.data) for e in small._history]
    assert big.snapshot()["counts"] == small.snapshot()["counts"]


def test_the_same_run_twice_gives_identical_events():
    def run():
        live = make_live()
        live.advance(12_000)
        live.stop_node("B")
        live.advance(30_000)
        live.start_node("B")
        live.advance(30_000)
        return [(e.event_id, e.type, e.data) for e in live._history]

    assert run() == run()


def test_the_pacing_task_follows_the_wall_clock_times_the_speed_and_obeys_pause():
    async def scenario() -> tuple[int, int, int]:
        live = make_live(speed=200.0, tick_s=0.01)
        live.start()
        await asyncio.sleep(0.3)
        ran = live.sim_ms
        live.pause()
        frozen = live.sim_ms
        await asyncio.sleep(0.1)
        still = live.sim_ms
        live.resume()
        await asyncio.sleep(0.1)
        resumed = live.sim_ms
        await live.stop()
        return ran, still - frozen, resumed - still

    ran, while_paused, after_resume = asyncio.run(scenario())
    assert ran >= 20_000  # 0.3 s at 200x is about 60 s of simulated time (allow a slow machine)
    assert while_paused == 0  # a paused simulation does not move at all
    assert after_resume > 0


def test_a_slow_machine_falls_behind_instead_of_spiralling():
    async def scenario() -> int:
        # an absurd speed with a small per-tick cap: time advances by at most the cap per tick
        live = make_live(speed=1000.0, tick_s=0.01, max_advance_ms=250)
        live.start()
        await asyncio.sleep(0.2)
        reached = live.sim_ms
        await live.stop()
        return reached

    reached = asyncio.run(scenario())
    assert 0 < reached <= 250 * 25  # at most the cap on each of ~20 wake-ups


def test_memory_stays_bounded_over_a_long_accelerated_run():
    live = make_live(history=200, trace_max_events=3_000, recent_readings=50)
    sim = live.simulation
    checkpoints = []
    for minute in range(40):  # 40 simulated minutes, with failures and returns in between
        if minute % 10 == 3:
            live.stop_node("B")
        if minute % 10 == 6:
            live.start_node("B")
        if minute % 10 == 8:
            live.link_down("A->D", True)
        if minute % 10 == 9:
            live.link_down("A->D", False)
        live.advance(60_000)
        if minute in (10, 39):
            checkpoints.append(
                {
                    "history": len(live._history),
                    "trace": len(sim.trace.events),
                    "readings": len(live._readings),
                    "controller_events": len(live.controller.events),
                    "view_events": len(live.view.events),
                    "engine_queue": len(sim._engine._queue),
                    "compute": len(live.controller.stats.compute_us),
                }
            )
    early, late = checkpoints
    assert late["history"] <= 200 and late["trace"] <= 3_000 and late["readings"] <= 50
    assert late["controller_events"] <= 1_000 and late["view_events"] <= 1_000
    assert late["compute"] <= 1_000
    # the pending-work queue does not grow with run time
    assert late["engine_queue"] <= 200
    assert sim.trace.dropped > 0  # the trace really hit its bound and shed old entries
    # the counters keep counting even though old detail was dropped
    snapshot = live.snapshot()
    assert snapshot["readings"]["delivered_unique"] > 500


def test_snapshots_stay_small_however_long_the_run():
    live = make_live()
    live.advance(60_000)
    small = len(str(live.snapshot()))
    live.advance(30 * 60_000)
    large = len(str(live.snapshot()))
    assert large < small * 3  # bounded lists, so it does not scale with run length
