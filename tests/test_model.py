"""Tests for the pure EMS model (no Home Assistant needed)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Load model.py directly so the tests do not import the HA-dependent package __init__.
_PATH = Path(__file__).parents[1] / "custom_components" / "home_ems" / "model.py"
_SPEC = importlib.util.spec_from_file_location("home_ems_model", _PATH)
model = importlib.util.module_from_spec(_SPEC)
sys.modules["home_ems_model"] = model
_SPEC.loader.exec_module(model)

Advice, Inputs, RunTracker, evaluate = model.Advice, model.Inputs, model.RunTracker, model.evaluate

DAY = datetime(2026, 10, 24)


def inputs(power=5.0, flow=21.0, living=19.5, grid=300.0):
    return Inputs(heat_pump_power_w=power, flow_temp=flow, living_temp=living, grid_power_w=grid)


def at(hour, minute=0):
    return DAY.replace(hour=hour, minute=minute)


def test_unknown_without_heat_pump_power():
    result = evaluate(inputs(power=None), RunTracker(), at(12))
    assert result.advice is Advice.UNKNOWN


def test_wait_when_floor_ok_and_no_surplus():
    result = evaluate(inputs(flow=21.0, living=19.5, grid=300), RunTracker(), at(12))
    assert result.advice is Advice.WAIT
    assert result.floor_margin == 1.5
    assert result.pv_surplus_w == 0


def test_run_when_floor_below_margin():
    result = evaluate(inputs(flow=19.8, living=19.5), RunTracker(), at(10))
    assert result.advice is Advice.RUN
    assert "floor" in result.reason


def test_run_on_pv_surplus():
    result = evaluate(inputs(flow=21.5, living=19.5, grid=-1500), RunTracker(), at(12))
    assert result.advice is Advice.RUN
    assert result.pv_surplus_w == 1500


def test_no_pv_run_when_floor_full():
    result = evaluate(inputs(flow=23.0, living=19.5, grid=-1500), RunTracker(), at(12))
    assert result.advice is Advice.WAIT


def test_peak_window_waits_unless_floor_far_below():
    tracker = RunTracker()
    assert evaluate(inputs(flow=19.8, living=19.5), tracker, at(18)).advice is Advice.WAIT
    assert evaluate(inputs(flow=18.8, living=19.5), tracker, at(18, 5)).advice is Advice.RUN


def test_keep_running_until_minimum_then_running():
    tracker = RunTracker()
    start = at(13)
    evaluate(inputs(power=5), tracker, start - timedelta(minutes=1))
    assert evaluate(inputs(power=400), tracker, start).advice is Advice.KEEP_RUNNING
    assert evaluate(inputs(power=400), tracker, start + timedelta(minutes=29)).advice is Advice.KEEP_RUNNING
    assert evaluate(inputs(power=400), tracker, start + timedelta(minutes=30)).advice is Advice.RUNNING


def test_run_going_at_startup_is_not_a_start():
    tracker = RunTracker()
    result = evaluate(inputs(power=400), tracker, at(21))
    assert result.advice is Advice.RUNNING
    assert tracker.stats.runs == 0
    # its end is not counted as a short run either (start unknown)
    evaluate(inputs(power=5), tracker, at(21, 10))
    assert tracker.stats.short_runs == 0


def test_floor_margin_only_at_rest():
    tracker = RunTracker()
    assert evaluate(inputs(power=400, flow=26.0, living=19.5), tracker, at(9)).floor_margin is None
    assert evaluate(inputs(power=5, flow=21.0, living=19.5), tracker, at(9, 5)).floor_margin == 1.5


def step(tracker, minutes_from, minutes_to, **kw):
    for m in range(minutes_from, minutes_to):
        evaluate(inputs(**kw), tracker, at(10) + timedelta(minutes=m))


def test_run_tracker_counts_runs_and_short_runs_and_resets_daily():
    tracker = RunTracker()
    step(tracker, 0, 1, power=5)
    step(tracker, 1, 11, power=400)    # 10 min run -> short
    step(tracker, 11, 20, power=5)
    step(tracker, 20, 80, power=400)   # 60 min run
    step(tracker, 80, 81, power=5)
    assert tracker.stats.runs == 2
    assert tracker.stats.short_runs == 1
    evaluate(inputs(power=5), tracker, at(10) + timedelta(days=1))
    assert tracker.stats.runs == 0
    assert tracker.stats.short_runs == 0


def test_comparison_counters():
    tracker = RunTracker()
    # 10 min: floor below margin, heat pump off -> run advised
    step(tracker, 0, 11, power=5, flow=19.8, living=19.5)
    assert round(tracker.stats.advised_run_minutes) == 10
    # start follows the advice -> not unadvised
    step(tracker, 11, 21, power=400, grid=-500)
    assert tracker.stats.unadvised_starts == 0
    # each interval counts for the state at its end, so the start minute counts as running
    assert round(tracker.stats.running_minutes) == 10
    assert round(tracker.stats.pv_running_minutes) == 10
    # rest with floor ok (advice wait), then a start -> unadvised
    step(tracker, 21, 41, power=5, flow=21.0, living=19.5)
    step(tracker, 41, 45, power=400)
    assert tracker.stats.unadvised_starts == 1


def test_gaps_longer_than_max_step_are_not_counted():
    tracker = RunTracker()
    evaluate(inputs(power=400), tracker, at(10))
    evaluate(inputs(power=400), tracker, at(11))
    assert tracker.stats.running_minutes == 0


def test_restore_after_short_restart_keeps_run_and_counters():
    tracker = RunTracker()
    step(tracker, 0, 1, power=5)
    step(tracker, 1, 21, power=400)
    data = tracker.to_dict()
    restored = RunTracker.from_dict(data, at(10, 25))
    assert restored.stats.runs == 1
    assert restored.run_start == at(10, 1)
    assert evaluate(inputs(power=400), restored, at(10, 26)).advice is Advice.KEEP_RUNNING
    assert restored.stats.runs == 1


def test_restore_after_long_restart_keeps_counters_only():
    tracker = RunTracker()
    step(tracker, 0, 1, power=5)
    step(tracker, 1, 21, power=400)
    restored = RunTracker.from_dict(tracker.to_dict(), at(12))
    assert restored.stats.runs == 1
    assert evaluate(inputs(power=400), restored, at(12)).advice is Advice.RUNNING
    assert restored.stats.runs == 1


def test_restore_on_next_day_resets_counters():
    tracker = RunTracker()
    step(tracker, 0, 1, power=5)
    step(tracker, 1, 21, power=400)
    restored = RunTracker.from_dict(tracker.to_dict(), at(10) + timedelta(days=1))
    assert restored.stats.runs == 0
