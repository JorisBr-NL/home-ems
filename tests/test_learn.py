"""Tests for learning COP and heat per outdoor temperature (v0.11.0)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DIR = Path(__file__).parents[1] / "custom_components" / "home_ems"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _DIR / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


learn = _load("home_ems_learn", "learn.py")
planner = _load("home_ems_planner_l", "planner.py")
T0 = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)


def run(learner, outside, heat_w, el_w, hours, start=T0):
    for m in range(int(hours * 60)):
        learner.add(outside, heat_w, el_w, True, 1.0, start + timedelta(minutes=m))


def test_learns_cop_and_heat_per_bucket_after_enough_running():
    lr = learn.HeatPumpLearner()
    run(lr, 1.0, 5000, 1600, 2)                  # 2 h at 1 C: not enough yet
    assert lr.points() == ()
    run(lr, 1.0, 5000, 1600, 2, T0 + timedelta(hours=2))
    (temp, cop, heat), = lr.points()
    assert temp == 1.0 and abs(cop - 3.12) < 0.02 and abs(heat - 5.0) < 0.05


def test_idle_minutes_and_missing_data_are_ignored():
    lr = learn.HeatPumpLearner()
    lr.add(1.0, 5000, 5, True, 60, T0)           # standby power, not running
    lr.add(1.0, 5000, 1600, False, 60, T0)
    lr.add(None, 5000, 1600, True, 60, T0)
    assert lr.buckets == {}


def test_old_data_fades_and_roundtrip():
    lr = learn.HeatPumpLearner(half_life=timedelta(days=14))
    run(lr, 5.0, 4000, 1000, 4)
    later = T0 + timedelta(days=14)
    lr.add(5.0, 0, 0, False, 1, later)           # only decays
    assert abs(lr.buckets[2].hours - 2.0) < 0.05
    again = learn.HeatPumpLearner.from_dict(lr.to_dict())
    assert again.buckets[2].hours == round(lr.buckets[2].hours, 3)


def test_planner_uses_learned_points():
    ps = planner.PlannerSettings(cop_points=((1.0, 3.0), (9.0, 5.0)), heat_points=((1.0, 6.0), (9.0, 3.5)))
    assert planner.cop_at(5.0, ps) == 4.0
    assert planner.cop_at(-5.0, ps) == 3.0                     # flat beyond the points
    assert abs(planner.heat_rate_at(5.0, ps) - 4.75) < 1e-9
    assert planner.cop_at(5.0, planner.PlannerSettings()) == 3.5 + 0.15 * 5    # formula without
