"""Tests for the checks on the planner with the house model (v0.12.2)."""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DIR = Path(__file__).parents[1] / "custom_components" / "home_ems"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _DIR / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mc = _load("model_check", "model_check.py")
T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
Q = timedelta(minutes=15)


def _predicted(made: datetime, temp: float) -> list[tuple[datetime, float]]:
    """A flat prediction from `made` for 36 hours, per quarter."""
    start = made.replace(minute=made.minute - made.minute % 15)
    return [(start + k * Q, temp) for k in range(36 * 4)]


def test_a_prediction_is_compared_with_the_room_three_hours_later():
    log = mc.PredictionLog()
    log.add_plan(T0, _predicted(T0, 19.2))
    assert log.compare(T0 + timedelta(hours=2), 19.0) is None       # not due yet
    assert log.compare(T0 + timedelta(hours=3), 19.5) == 0.3        # measured - predicted
    assert log.last_predicted == 19.2
    # still shown a few minutes later, but counted once
    assert log.compare(T0 + timedelta(hours=3, minutes=4), 19.4) == 0.2
    assert len(log.errors) == 1
    # without a newer prediction it is gone after a quarter
    assert log.compare(T0 + timedelta(hours=3, minutes=16), 19.4) is None
    assert log.last_predicted is None


def test_mean_error_and_hours_compared_over_the_window():
    log = mc.PredictionLog()
    for k in range(13 * 12):                      # a plan every 5 minutes for 13 hours
        made = T0 + k * timedelta(minutes=5)
        log.add_plan(made, _predicted(made, 19.0))
        log.compare(made, 19.1 if k % 2 else 18.9)
    assert log.mean_abs_error() == 0.1
    assert 9.5 <= log.hours_compared() <= 10.25   # the first 3 hours have nothing due yet
    # errors older than the window drop out
    log.compare(T0 + timedelta(days=3), 19.0)
    assert log.errors == [] and log.mean_abs_error() is None


@dataclass
class Run:
    start: datetime
    end: datetime


def test_night_run_hours_counts_quarters_in_the_night_window():
    night = lambda t: t.hour >= 20 or t.hour < 7       # noqa: E731
    runs = [Run(T0.replace(hour=14), T0.replace(hour=17)),            # afternoon: 0
            Run(T0.replace(hour=19), T0.replace(hour=21, minute=30))]  # 1.5 h at night
    assert mc.night_run_hours(runs, night) == 1.5
    assert mc.night_run_hours([], night) == 0


def _good_log() -> mc.PredictionLog:
    log = mc.PredictionLog()
    for k in range(16 * 12):
        made = T0 + k * timedelta(minutes=5)
        log.add_plan(made, _predicted(made, 19.0))
        log.compare(made, 19.1)
    return log


def test_readiness_sums_up_the_checks():
    good = _good_log()
    assert mc.readiness(good, 0.0, True, 5.0, 3.0) == mc.Readiness(mc.READY, ())
    # a run at night with a fixed tariff: not ready, with the reason
    r = mc.readiness(good, 1.25, True, 5.0, 3.0)
    assert r.state == mc.NOT_READY and "night" in r.reasons[0]
    # with a dynamic tariff night runs may be cheaper: no objection
    assert mc.readiness(good, 1.25, False, 5.0, 3.0).state == mc.READY
    # a prediction that is off
    bad = mc.PredictionLog()
    for k in range(16 * 12):
        made = T0 + k * timedelta(minutes=5)
        bad.add_plan(made, _predicted(made, 19.0))
        bad.compare(made, 19.6)
    assert mc.readiness(bad, 0.0, True, 5.0, 3.0).state == mc.NOT_READY
    # too little: a fresh log, nothing learned, no plan
    r = mc.readiness(mc.PredictionLog(), None, True, 1.0, 3.0)
    assert r.state == mc.TOO_LITTLE_DATA and len(r.reasons) == 3
    assert mc.readiness(good, 0.0, True, None, 3.0).state == mc.TOO_LITTLE_DATA


def test_only_night_runs_above_the_night_minimum_count():
    night = lambda t: t.hour >= 20 or t.hour < 7       # noqa: E731
    lower = lambda t: 18.5                             # noqa: E731
    temps = {T0.replace(hour=2): 18.55,                # at the night minimum: needed
             T0.replace(hour=22): 19.1}                # well above it: early
    runs = [Run(T0.replace(hour=2), T0.replace(hour=4)),
            Run(T0.replace(hour=22), T0.replace(hour=23)),
            Run(T0.replace(hour=14), T0.replace(hour=16))]   # by day: never counted
    assert mc.night_run_hours(runs, night) == 3.0
    assert mc.early_night_run_hours(runs, night, temps.get, lower) == 1.0
    # without a prediction for the start the run is not counted
    assert mc.early_night_run_hours(runs, night, lambda t: None, lower) == 0.0
