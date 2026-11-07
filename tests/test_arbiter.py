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
    # 400 W of 1500 W: gap 0.1 + 0.27 x (0.6 - 0.1) = 0.23
    assert (t.setpoint, t.source, t.boost) == (19.7, "pv", "solar surplus")


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


def test_setpoint_follows_the_room_during_a_run():
    run = ev(Advice.RUNNING, running=True)
    t = arbiter.target_setpoint(run, 19.2, True, current_setpoint=19.4)
    assert (t.setpoint, t.source) == (19.3, "planner")      # room 19.2: back to + 0.1
    t = arbiter.target_setpoint(run, 19.3, True, current_setpoint=19.4)
    assert t.setpoint == 19.4                                # already right
    # with a keep band the current setpoint is kept while it still asks for heat
    band = arbiter.ArbiterSettings(keep_band=0.2)
    t = arbiter.target_setpoint(run, 19.3, True, settings=band, current_setpoint=19.5)
    assert t.setpoint == 19.5
    t = arbiter.target_setpoint(run, 19.4, True, current_setpoint=19.4)
    assert t.setpoint == 19.5                                # room reached it: one step up
    t = arbiter.target_setpoint(run, 19.0, True, current_setpoint=19.6)
    assert t.setpoint == 19.1                                # gap too large: back to + 0.1
    t = arbiter.target_setpoint(ev(Advice.WAIT), 19.0, False, current_setpoint=19.2)
    assert t.setpoint == 18.0                                # idle is not kept


def test_bedroom_call_starts_or_holds_a_run_after_floor_and_keep_running():
    t = arbiter.target_setpoint(ev(Advice.WAIT), 19.0, False, bedroom_call=True)
    assert (t.setpoint, t.source) == (19.2, "bedroom")
    t = arbiter.target_setpoint(ev(Advice.RUNNING, running=True), 19.0, False, bedroom_call=True)
    assert (t.setpoint, t.source) == (19.1, "bedroom")
    assert arbiter.target_setpoint(ev(Advice.RUN, "floor"), 19.0, False, bedroom_call=True).source == "floor"
    assert arbiter.target_setpoint(ev(Advice.WAIT), 19.0, False, enabled=False, bedroom_call=True).source == "off"


def test_runs_stay_below_the_ceiling_except_on_solar():
    s = arbiter.ArbiterSettings(run_ceiling=20.0)
    t = arbiter.target_setpoint(ev(Advice.RUN, "floor"), 19.95, False, settings=s)
    assert t.setpoint == 20.0
    run = ev(Advice.RUNNING, running=True)
    # planned run: the room reached the ceiling, no higher setpoint
    assert arbiter.target_setpoint(run, 20.0, True, settings=s, current_setpoint=20.0).setpoint == 20.0
    # a setpoint above the ceiling (set by hand earlier) is not kept
    assert arbiter.target_setpoint(run, 19.6, True, settings=s, current_setpoint=20.4).setpoint == 19.7
    # on solar surplus the heat may go into the house up to the comfort maximum
    pv = ev(Advice.RUNNING, running=True, surplus=500.0)
    assert arbiter.target_setpoint(pv, 20.3, False, settings=s).setpoint == 20.6


def test_gap_grows_with_solar_surplus():
    s = arbiter.ArbiterSettings(gap_max=0.6, full_surplus_w=1500, low_price=0)
    run = lambda w: ev(Advice.RUNNING, running=True, surplus=w)
    assert arbiter.target_setpoint(run(0), 19.0, True, settings=s).setpoint == 19.1     # no surplus: small gap
    t = arbiter.target_setpoint(run(1500), 19.0, True, settings=s)
    assert (t.setpoint, t.boost, t.boost_factor) == (19.6, "solar surplus", 1.0)
    assert arbiter.target_setpoint(run(3000), 19.0, True, settings=s).setpoint == 19.6  # capped at gap_max
    # the ceiling for boosted runs is the comfort maximum, not the run ceiling
    assert arbiter.target_setpoint(run(1500), 19.9, True, settings=s).setpoint == 20.5


def test_low_price_gives_the_largest_gap():
    s = arbiter.ArbiterSettings(gap_max=0.6, low_price=0.20)
    plan = ev(Advice.WAIT)
    t = arbiter.target_setpoint(plan, 19.0, True, settings=s, price_now=0.18)
    assert (t.setpoint, t.source, t.boost) == (19.6, "planner", "low price")
    assert arbiter.target_setpoint(plan, 19.0, True, settings=s, price_now=0.30).setpoint == 19.2
    off = arbiter.ArbiterSettings(low_price=0)
    assert arbiter.target_setpoint(plan, 19.0, True, settings=off, price_now=0.05).setpoint == 19.2


def test_no_boost_on_idle():
    s = arbiter.ArbiterSettings(low_price=0.20)
    t = arbiter.target_setpoint(ev(Advice.WAIT), 19.0, False, settings=s, price_now=0.10)
    assert (t.source, t.setpoint, t.boost) == ("idle", 18.0, None)


def test_boost_function():
    s = arbiter.ArbiterSettings(full_surplus_w=1000, low_price=0.2)
    assert arbiter.boost(500, 0.3, s) == (0.5, "solar surplus")
    assert arbiter.boost(500, 0.1, s) == (1.0, "low price")
    assert arbiter.boost(None, None, s) == (0.0, None)
