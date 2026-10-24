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
    assert evaluate(inputs(power=400), tracker, start).advice is Advice.KEEP_RUNNING
    assert evaluate(inputs(power=400), tracker, start + timedelta(minutes=29)).advice is Advice.KEEP_RUNNING
    assert evaluate(inputs(power=400), tracker, start + timedelta(minutes=30)).advice is Advice.RUNNING


def test_run_tracker_counts_runs_and_short_runs_and_resets_daily():
    tracker = RunTracker()
    t = at(8)
    for running, minutes in [(True, 0), (False, 10), (True, 20), (False, 80)]:
        tracker.update(running, t + timedelta(minutes=minutes))
    assert tracker.runs_today == 2
    assert tracker.short_runs_today == 1
    tracker.update(False, t + timedelta(days=1))
    assert tracker.runs_today == 0
    assert tracker.short_runs_today == 0
