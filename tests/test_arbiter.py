"""Tests for the arbiter (one target setpoint for the living room)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_DIR = Path(__file__).parents[1] / "custom_components" / "home_ems"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _DIR / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


model = _load("home_ems_model_a", "model.py")
arbiter = _load("home_ems_arbiter", "arbiter.py")
Advice = model.Advice


def ev(advice, cause=None, running=False, surplus=0.0, reason="test"):
    return model.Evaluation(advice=advice, reason=reason, heat_pump_running=running,
                            floor_margin=None, pv_surplus_w=surplus, in_peak=False, cause=cause)


def target(evaluation, living=19.0, plan=False, enabled=True):
    return arbiter.target_setpoint(evaluation, living, plan, enabled)


def test_off_and_unknown_give_no_target():
    assert target(ev(Advice.RUN, "floor"), enabled=False).source == "off"
    assert target(ev(Advice.RUN, "floor"), enabled=False).setpoint is None
    assert target(ev(Advice.UNKNOWN)).setpoint is None
    assert target(ev(Advice.WAIT), living=None).source == "unknown"


def test_runs_start_just_above_the_room():
    t = target(ev(Advice.RUN, "floor"), living=19.1)
    assert (t.setpoint, t.source) == (19.3, "floor")
    assert target(ev(Advice.RUN, "comfort_min"), living=17.6).source == "comfort_min"
    assert target(ev(Advice.RUN, "pv"), living=19.4).setpoint == 19.6


def test_priority_floor_before_planner_and_keep_running_before_planner():
    assert target(ev(Advice.RUN, "floor"), plan=True).source == "floor"
    t = target(ev(Advice.KEEP_RUNNING, running=True), living=19.2, plan=True)
    assert (t.setpoint, t.source) == (19.3, "keep_running")


def test_planner_starts_and_holds_runs():
    assert target(ev(Advice.WAIT), living=19.0, plan=True).setpoint == 19.2
    t = target(ev(Advice.RUNNING, running=True), living=19.4, plan=True)
    assert (t.setpoint, t.source) == (19.5, "planner")


def test_run_on_solar_is_held_while_exporting():
    t = target(ev(Advice.RUNNING, running=True, surplus=400.0), living=19.5)
    assert (t.setpoint, t.source) == (19.6, "pv")


def test_idle_goes_to_the_comfort_minimum():
    t = target(ev(Advice.WAIT), living=19.3)
    assert (t.setpoint, t.source) == (18.0, "idle")
    assert target(ev(Advice.RUNNING, running=True), living=19.3).reason.startswith("run not needed")


def test_target_stays_within_the_comfort_band():
    assert target(ev(Advice.RUN, "pv"), living=21.6).setpoint == 21.5
    assert target(ev(Advice.RUN, "comfort_min"), living=16.0).setpoint == 18.0


def test_differs_from_current_setpoint():
    t = target(ev(Advice.RUN, "floor"), living=19.1)
    assert t.differs_from(19.0) and t.differs_from(None) and not t.differs_from(19.3)
    assert not target(ev(Advice.WAIT), enabled=False).differs_from(19.0)
