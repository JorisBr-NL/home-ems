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
    # no extra gap from the grid meter yet: the normal small gap
    assert (t.setpoint, t.source, t.boost) == (19.6, "pv", None)
    t = arbiter.target_setpoint(ev(Advice.RUNNING, running=True, surplus=400.0), 19.5, False,
                                pv_extra=0.2)
    assert (t.setpoint, t.source, t.boost, t.boost_k) == (19.8, "pv", "solar surplus", 0.2)


def test_off_level_is_below_the_living_room_minimum():
    t = target(ev(Advice.WAIT), living=19.3)
    assert (t.setpoint, t.source) == (17.5, "idle")          # minimum 18.0 - 0.5
    s = arbiter.ArbiterSettings(living_min=18.5, off_margin=0.5)
    assert arbiter.target_setpoint(ev(Advice.WAIT), 19.3, False, settings=s).setpoint == 18.0
    low = arbiter.ArbiterSettings(living_min=15.2, off_margin=1.0)
    assert arbiter.target_setpoint(ev(Advice.WAIT), 19.3, False, settings=low).setpoint == 15.0


def test_target_stays_below_the_comfort_maximum():
    assert target(ev(Advice.RUN, "pv"), living=21.6).setpoint == 21.5
    assert target(ev(Advice.RUN, "comfort_min"), living=16.0).setpoint == 16.2


def test_differs_from_current_setpoint():
    t = target(ev(Advice.RUN, "floor"), living=19.1)
    assert t.differs_from(19.0) and t.differs_from(None) and not t.differs_from(19.3)
    assert not target(ev(Advice.WAIT), enabled=False).differs_from(19.0)


def test_setpoint_follows_the_room_up_never_down():
    run = ev(Advice.RUNNING, running=True)
    t = arbiter.target_setpoint(run, 19.2, True, current_setpoint=19.4)
    assert (t.setpoint, t.source) == (19.4, "planner")      # room fell back: setpoint stays
    t = arbiter.target_setpoint(run, 19.4, True, current_setpoint=19.4)
    assert t.setpoint == 19.5                                # room reached it: one step up
    t = arbiter.target_setpoint(run, 18.4, True, current_setpoint=18.6)
    assert t.setpoint == 18.6                                # cold start: no chasing the room down
    t = arbiter.target_setpoint(ev(Advice.WAIT), 19.0, False, current_setpoint=19.2)
    assert (t.setpoint, t.source) == (17.5, "idle")          # off is not kept


def test_living_room_minimum_starts_a_run_a_little_above_it():
    s = arbiter.ArbiterSettings(living_min=18.5, run_ceiling=19.0)
    t = arbiter.target_setpoint(ev(Advice.WAIT), 18.6, False, settings=s)
    assert (t.setpoint, t.source) == (18.8, "minimum")
    t = arbiter.target_setpoint(ev(Advice.WAIT), 18.7, False, settings=s)
    assert (t.setpoint, t.source) == (18.0, "idle")
    # at night the run still climbs to the minimum + start band, not to an equal ceiling
    night = arbiter.ArbiterSettings(living_min=18.5, run_ceiling=18.5, night=True)
    run = ev(Advice.RUNNING, running=True)
    t = arbiter.target_setpoint(run, 18.6, False, settings=night, current_setpoint=18.7)
    assert (t.setpoint, t.source) == (18.7, "to_top")
    t = arbiter.target_setpoint(run, 18.8, False, settings=night, current_setpoint=18.8)
    assert (t.setpoint, t.source) == (18.0, "idle")


def test_run_stops_cleanly_at_the_top():
    s = arbiter.ArbiterSettings(living_min=18.5, run_ceiling=19.0)
    run = ev(Advice.RUNNING, running=True)
    t = arbiter.target_setpoint(run, 18.9, True, settings=s, current_setpoint=19.0)
    assert (t.setpoint, t.source) == (19.0, "planner")
    t = arbiter.target_setpoint(run, 19.0, True, settings=s, current_setpoint=19.0)
    assert (t.setpoint, t.source) == (18.0, "idle")          # never left at setpoint = room
    assert "top" in t.reason
    # before the minimum run time the run is held, also past the top
    t = arbiter.target_setpoint(ev(Advice.KEEP_RUNNING, running=True), 19.0, True, settings=s,
                                current_setpoint=19.0)
    assert (t.setpoint, t.source) == (19.1, "keep_running")


def test_planned_run_starts_only_with_room_to_climb():
    s = arbiter.ArbiterSettings(living_min=18.5, run_ceiling=19.0, start_band=0.3)
    t = arbiter.target_setpoint(ev(Advice.WAIT), 18.7, True, settings=s)
    assert (t.setpoint, t.source) == (18.9, "planner")
    t = arbiter.target_setpoint(ev(Advice.WAIT), 18.8, True, settings=s)
    assert (t.setpoint, t.source) == (18.0, "idle")
    assert "close to the top" in t.reason


def test_a_run_goes_on_to_the_top_after_the_plan_ends_except_in_a_price_peak():
    s = arbiter.ArbiterSettings(living_min=18.5, run_ceiling=19.0)
    run = ev(Advice.RUNNING, running=True)
    t = arbiter.target_setpoint(run, 18.8, False, settings=s, current_setpoint=18.9)
    assert (t.setpoint, t.source) == (18.9, "to_top")
    peak = model.Evaluation(advice=Advice.RUNNING, reason="test", heat_pump_running=True,
                            floor_margin=None, pv_surplus_w=0.0, in_peak=True)
    t = arbiter.target_setpoint(peak, 18.8, False, settings=s, current_setpoint=18.9)
    assert (t.setpoint, t.source) == (18.0, "idle")


def test_bedroom_call_starts_or_holds_a_run_after_floor_and_keep_running():
    t = arbiter.target_setpoint(ev(Advice.WAIT), 19.0, False, bedroom_call=True)
    assert (t.setpoint, t.source) == (19.2, "bedroom")
    t = arbiter.target_setpoint(ev(Advice.RUNNING, running=True), 19.0, False, bedroom_call=True)
    assert (t.setpoint, t.source) == (19.1, "bedroom")
    assert arbiter.target_setpoint(ev(Advice.RUN, "floor"), 19.0, False, bedroom_call=True).source == "floor"
    assert arbiter.target_setpoint(ev(Advice.WAIT), 19.0, False, enabled=False, bedroom_call=True).source == "off"


def test_runs_stay_below_the_ceiling_except_on_solar():
    s = arbiter.ArbiterSettings(run_ceiling=20.0)
    # a floor run asks for heat also at the ceiling (never setpoint = room)
    t = arbiter.target_setpoint(ev(Advice.RUN, "floor"), 20.0, False, settings=s)
    assert t.setpoint == 20.2
    run = ev(Advice.RUNNING, running=True)
    # a setpoint above the ceiling (set by hand earlier) is not kept
    assert arbiter.target_setpoint(run, 19.6, True, settings=s, current_setpoint=20.4).setpoint == 20.0
    # on solar surplus the heat may go into the house up to the comfort maximum
    pv = ev(Advice.RUNNING, running=True, surplus=500.0)
    assert arbiter.target_setpoint(pv, 20.3, False, settings=s, pv_extra=0.2).setpoint == 20.6


def test_gap_grows_with_the_extra_gap_from_the_grid_meter():
    s = arbiter.ArbiterSettings(gap_max=0.6, low_price=0)
    run = ev(Advice.RUNNING, running=True)
    assert arbiter.target_setpoint(run, 19.0, True, settings=s).setpoint == 19.1     # no extra: small gap
    t = arbiter.target_setpoint(run, 19.0, True, settings=s, pv_extra=0.5)
    assert (t.setpoint, t.boost, t.boost_k) == (19.6, "solar surplus", 0.5)
    assert arbiter.target_setpoint(run, 19.0, True, settings=s, pv_extra=2.0).setpoint == 19.6  # capped
    # the ceiling for boosted runs is the comfort maximum, not the run ceiling
    assert arbiter.target_setpoint(run, 19.9, True, settings=s, pv_extra=0.5).setpoint == 20.5


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
    assert (t.source, t.setpoint, t.boost) == ("idle", 17.5, None)


def test_boost_function():
    s = arbiter.ArbiterSettings(low_price=0.2)
    assert arbiter.boost(0.3, 0.3, s) == ("solar surplus", 0.3)
    assert arbiter.boost(0.3, 0.1, s) == ("low price", 0.0)
    assert arbiter.boost(0.0, None, s) == (None, 0.0)


def test_pv_boost_follows_the_five_minute_average_of_the_grid_meter():
    from datetime import datetime, timedelta
    s = arbiter.ArbiterSettings(gap_max=0.6, boost_margin_w=300)
    b = arbiter.PvBoost()
    t0 = datetime(2026, 11, 10, 12, 0)
    five = timedelta(minutes=5)
    minute = [0]

    def feed(readings, running=True):
        for w in readings:
            b.update(w, running, t0 + timedelta(minutes=minute[0]), five, s)
            minute[0] += 1
        return b.extra

    assert feed([-600] * 5) == 0.0          # first five minutes: averaging, no step yet
    assert feed([-600]) == 0.1              # average 600 W out: one step up
    assert feed([-600] * 5) == 0.2
    # a kettle for one minute (+2000 W) within a sunny five minutes: average -80 W, stays
    assert feed([-600, 2000, -600, -600, -600]) == 0.2
    assert feed([800] * 5) == 0.1           # importing on average: one step down
    assert feed([800] * 5) == 0.0
    assert feed([800] * 5) == 0.0           # never below the normal gap
    assert feed([-2000] * 60) == 0.5        # at most gap_max - hold offset
    assert feed([0], running=False) == 0.0  # at rest without export: 0 at once
    off = arbiter.ArbiterSettings(boost_margin_w=0)
    assert arbiter.PvBoost(extra=0.3).update(-2000, True, t0, five, off) == 0.0


def test_night_window_wraps_past_midnight():
    from datetime import time
    w = arbiter.in_window
    assert w(time(23, 30), time(23, 0), time(6, 0))
    assert w(time(2, 0), time(23, 0), time(6, 0))
    assert not w(time(6, 0), time(23, 0), time(6, 0))
    assert not w(time(22, 59), time(23, 0), time(6, 0))
    assert w(time(13, 0), time(12, 0), time(14, 0))
    assert not w(time(1, 0), time(1, 0), time(1, 0))


def test_night_ceiling_lets_the_house_cool():
    assert arbiter.night_ceiling(20.0, 18.5, 18.0) == 18.5
    assert arbiter.night_ceiling(20.0, 17.0, 18.0) == 18.0   # never below the comfort minimum
    assert arbiter.night_ceiling(19.2, 19.5, 18.0) == 19.2   # never above the day ceiling
    s = arbiter.ArbiterSettings(run_ceiling=18.5, night=True)
    run = ev(Advice.RUNNING, running=True)
    # a planned run with the room above the night ceiling: the run stops (off level)
    t = arbiter.target_setpoint(run, 19.4, True, settings=s, current_setpoint=19.5)
    assert (t.setpoint, t.source) == (17.5, "idle")
    # at night a low price does not lift the ceiling to the comfort maximum
    cheap = arbiter.ArbiterSettings(run_ceiling=18.5, night=True, low_price=0.20)
    assert arbiter.target_setpoint(run, 18.3, True, settings=cheap, price_now=0.10).setpoint == 18.5
    day = arbiter.ArbiterSettings(run_ceiling=18.5, low_price=0.20)
    assert arbiter.target_setpoint(run, 18.3, True, settings=day, price_now=0.10).setpoint > 18.5


def test_pv_boost_halves_on_import():
    from datetime import datetime, timedelta, timezone
    s = arbiter.ArbiterSettings(gap_max=0.6, boost_margin_w=300)
    b = arbiter.PvBoost(extra=0.5)
    t0 = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=5)
    b.update(800.0, True, t0, step, s)
    assert b.update(800.0, True, t0 + step, step, s) == 0.2
    assert b.update(800.0, True, t0 + 2 * step, step, s) == 0.1
    assert b.update(800.0, True, t0 + 3 * step, step, s) == 0.0


def test_a_boost_taken_back_comes_off_the_setpoint():
    run = ev(Advice.RUNNING, running=True, surplus=500.0)
    # written with a raise of 0.3: room 18.8 + 0.1 + 0.3 = 19.2
    t = arbiter.target_setpoint(run, 18.8, False, current_setpoint=19.2, pv_extra=0.0, prev_raised=0.3)
    assert (t.setpoint, t.boost_k) == (18.9, 0.0)
    t = arbiter.target_setpoint(run, 18.8, False, current_setpoint=19.2, pv_extra=0.1, prev_raised=0.3)
    assert t.setpoint == 19.0
    # without a raise the setpoint still never follows the room down
    t = arbiter.target_setpoint(run, 18.6, False, current_setpoint=19.0, prev_raised=0.0)
    assert t.setpoint == 19.0


def test_pv_boost_keeps_its_step_when_the_grid_reading_drops_out():
    from datetime import datetime, timedelta, timezone
    s = arbiter.ArbiterSettings(gap_max=0.6, boost_margin_w=300)
    b = arbiter.PvBoost(extra=0.3)
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    assert b.update(None, True, now, timedelta(minutes=5), s) == 0.3


def test_run_on_solar_stops_cleanly_at_the_comfort_maximum():
    s = arbiter.ArbiterSettings(comfort_max=20.0, living_min=19.0, run_ceiling=19.5)
    pv = ev(Advice.RUNNING, running=True, surplus=2000.0)
    t = arbiter.target_setpoint(pv, 19.9, False, settings=s, current_setpoint=20.0, pv_extra=0.4,
                                prev_raised=0.4)
    assert (t.setpoint, t.source) == (20.0, "pv")
    t = arbiter.target_setpoint(pv, 20.0, False, settings=s, current_setpoint=20.0, pv_extra=0.4,
                                prev_raised=0.4)
    assert (t.setpoint, t.source) == (18.5, "idle")       # never left at setpoint = room


def test_no_solar_start_just_below_the_comfort_maximum():
    s = arbiter.ArbiterSettings(comfort_max=20.0, living_min=19.0, run_ceiling=19.5, start_band=0.3)
    start = ev(Advice.RUN, "pv", surplus=2000.0)
    assert arbiter.target_setpoint(start, 19.7, False, settings=s).source == "pv"
    t = arbiter.target_setpoint(start, 19.8, False, settings=s)
    assert (t.setpoint, t.source) == (18.5, "idle")
