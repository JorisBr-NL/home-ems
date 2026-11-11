"""Tests for the planner with the house model (v0.11.0)."""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

_DIR = Path(__file__).parents[1] / "custom_components" / "home_ems"

# load planner.py and planner_model.py as a small package (planner_model imports .planner)
_pkg = types.ModuleType("hems_pm")
_pkg.__path__ = [str(_DIR)]
sys.modules["hems_pm"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"hems_pm.{name}", _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"hems_pm.{name}"] = module
    spec.loader.exec_module(module)
    return module


planner = _load("planner")
pm = _load("planner_model")

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)   # 13:00 in Amsterdam
PS = planner.PlannerSettings(heat_rate_kw=7.0, heat_rate_slope=0.4)
MS = pm.ModelSettings(min_run=timedelta(minutes=60), start_band=0.3)


def slots(hours: int = 36, temp: float = 5.0, price=lambda h: 0.30):
    return [planner.Slot(T0 + i * planner.QUARTER, price(T0 + i * planner.QUARTER), market=0.10,
                         temp=temp) for i in range(hours * 4)]


def day_night(moment: datetime, night_store: float | None = None) -> tuple[float, float, float, bool]:
    """Like the coordinator (v0.10.9): night 20:00-05:00 with the night ceiling, 19.0 in the
    last 2.5 h, the day top in the last 45 min; storage by day to 20.0, at night to
    `night_store` (dynamic contract) or not at all."""
    local = (moment + timedelta(hours=1)).time()        # UTC+1
    if local >= time(20, 0) or local < time(5, 0):
        top = 18.8
        if time(2, 30) <= local < time(5, 0):
            top = 19.0
        if time(4, 15) <= local < time(5, 0):
            top = 19.5
        return 18.5, top, max(top, night_store or top), True
    return 19.0, 19.5, 20.0, False


def test_house_model_cools_and_heats_like_the_house():
    model = pm.HouseModel.from_planner(PS, 19.0)
    # cooling at 5 C from 19.5: about 0.1-0.25 K per hour
    room = 19.5
    for _ in range(4):
        room = model.step(room, 5.0, 0.0)
    assert 0.08 < 19.5 - room < 0.3
    # heating with ~5 kW: the room rises
    assert model.step(19.0, 5.0, 5.0) > 19.0


def test_no_run_while_the_room_is_warm_and_the_first_run_waits_for_the_limit():
    p = pm.plan_with_model(slots(), T0, 20.0, day_night, PS, MS)
    assert p is not None and p.model
    first = p.runs[0]
    assert first.start >= T0 + timedelta(hours=3)        # 20.0 takes hours to reach the limit
    # the predicted room never drops below the lower limit (minus a little lag)
    for moment, temp in p.predicted:
        assert temp >= day_night(moment)[0] - 0.25


def test_runs_go_on_to_the_top_and_last_at_least_the_minimum_run_time():
    p = pm.plan_with_model(slots(), T0, 19.1, day_night, PS, MS)
    assert p.runs
    full = [r for r in p.runs if r.end < p.horizon_end]       # the last one may hit the horizon
    assert all(r.end - r.start >= timedelta(minutes=60) for r in full)
    assert len(p.runs) <= 6                              # long runs, not many short ones


def test_a_cheap_window_before_a_peak_is_used():
    # from 10:00 local: cheap until 16:00, expensive peak 16:00-21:00, normal otherwise
    start = T0 - timedelta(hours=3)
    def price(moment: datetime) -> float:
        h = (moment + timedelta(hours=1)).hour
        return 0.15 if 10 <= h < 16 else (0.60 if 16 <= h < 21 else 0.30)
    day = [planner.Slot(start + i * planner.QUARTER, price(start + i * planner.QUARTER), market=0.10,
                        temp=5.0) for i in range(24 * 4)]
    p = pm.plan_with_model(day, start, 19.2, day_night, PS, MS)
    in_peak = [r for r in p.runs
               if 16 <= (r.start + timedelta(hours=1)).hour < 21]
    assert not in_peak, [(r.start, r.end, r.reason) for r in p.runs]
    assert any(r.reason == "store" for r in p.runs)


def test_a_run_in_progress_goes_on_first():
    p = pm.plan_with_model(slots(), T0, 19.2, day_night, PS, MS, running=True)
    assert p.runs[0].start == T0 and p.runs[0].reason == "in_progress"


def test_no_slots_or_no_room_gives_no_plan():
    assert pm.plan_with_model([], T0, 19.0, day_night, PS, MS) is None
    assert pm.plan_with_model(slots(), T0, None, day_night, PS, MS) is None


def test_no_planned_run_at_night_with_a_fixed_tariff():
    # night 20:00-05:00 local; the room is just above the night minimum
    start = T0 + timedelta(hours=8)                      # 21:00 local
    night = [planner.Slot(start + i * planner.QUARTER, 0.25, market=0.25, temp=3.0)
             for i in range(10 * 4)]
    p = pm.plan_with_model(night, start, 18.9, day_night, PS, MS)
    for r in p.runs:
        local = (r.start + timedelta(hours=1)).time()
        if local >= time(20, 0) or local < time(5, 0):
            # only a start at the night minimum: the predicted room is at the limit then
            temp = dict(p.predicted)[r.start]
            assert temp < 18.5 + 0.1, (r.start, temp)


def test_cheap_night_hours_store_heat_with_a_dynamic_contract():
    # 21:00 to 21:00: night 0.20 (cloudy, no wind), day 0.45
    start = T0 + timedelta(hours=8)
    def price(moment: datetime) -> float:
        h = (moment + timedelta(hours=1)).hour
        return 0.20 if (h >= 22 or h < 6) else 0.45
    day = [planner.Slot(start + i * planner.QUARTER, price(start + i * planner.QUARTER),
                        market=0.10, temp=3.0) for i in range(24 * 4)]
    p = pm.plan_with_model(day, start, 18.9, lambda m: day_night(m, night_store=19.5), PS, MS)
    stores = [r for r in p.runs if r.reason == "store"]
    assert stores, [(r.start, r.end, r.reason) for r in p.runs]
    assert any((r.start + timedelta(hours=1)).hour >= 22 or (r.start + timedelta(hours=1)).hour < 6
               for r in stores)
