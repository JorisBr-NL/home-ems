"""Tests for the day planner (no Home Assistant needed)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "custom_components" / "home_ems" / "planner.py"
_SPEC = importlib.util.spec_from_file_location("home_ems_planner", _PATH)
planner = importlib.util.module_from_spec(_SPEC)
sys.modules["home_ems_planner"] = planner
_SPEC.loader.exec_module(planner)

Q = planner.QUARTER
T0 = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)


def day(prices=None, temp=8.0, pv=None, hours=24, start=T0):
    """Quarter slots; `prices`/`pv` are per hour (dict hour -> value), defaults flat."""
    out = []
    for q in range(hours * 4):
        h = q // 4
        out.append(planner.Slot(
            start=start + q * Q,
            price=(prices or {}).get(h, 0.25),
            market=0.05,
            pv_kw=(pv or {}).get(h, 0.0),
            temp=temp,
        ))
    return out


S = planner.PlannerSettings(horizon=timedelta(hours=24))


def test_cop_follows_temperature_and_is_clipped():
    assert planner.cop_at(10, S) == pytest.approx(5.0)
    assert planner.cop_at(-30, S) == S.cop_min
    assert planner.cop_at(40, S) == S.cop_max


def test_heat_need_matches_degree_hour_model():
    # 24 h at 8 C: 240 degree-hours -> -5.3 + 0.217 * 240 = 46.8 kWh
    assert planner.heat_need(day(temp=8.0), S) == pytest.approx(-5.3 + 0.217 * 240)
    assert planner.heat_need(day(temp=20.0), S) == 0.0


def test_plan_covers_heat_need_in_whole_hours():
    p = planner.plan(day(temp=8.0), T0, S)
    need = -5.3 + 0.217 * 240
    assert p.heat_need_kwh == pytest.approx(need, abs=0.01)
    assert p.run_hours * S.heat_rate_kw >= need
    assert all((r.end - r.start) >= S.block for r in p.runs)


def test_cheap_hours_are_chosen():
    prices = {h: 0.40 for h in range(24)} | {2: 0.10, 3: 0.10, 13: 0.12}
    p = planner.plan(day(prices=prices, temp=14.0), T0,
                     planner.PlannerSettings(horizon=timedelta(hours=24), max_gap=timedelta(hours=24)))
    starts = {r.start for r in p.runs}
    assert T0 + timedelta(hours=2) in starts or any(
        r.start <= T0 + timedelta(hours=2) < r.end for r in p.runs)
    assert p.expected_cost < p.baseline_cost


def test_solar_surplus_makes_hours_cheap():
    pv = {11: 4.0, 12: 4.0}
    p = planner.plan(day(pv=pv, temp=14.0), T0,
                     planner.PlannerSettings(horizon=timedelta(hours=24), max_gap=timedelta(hours=24)))
    noon = [r for r in p.runs if r.start <= T0 + timedelta(hours=11) < r.end]
    assert noon and noon[0].pv_share == pytest.approx(1.0)


def test_max_gap_adds_runs_when_need_is_small():
    # warm day: one block covers the need, but the gap rule forces runs spread over the day
    s = planner.PlannerSettings(horizon=timedelta(hours=24), max_gap=timedelta(hours=8))
    p = planner.plan(day(temp=15.5), T0, s)
    edges = [T0] + [x for r in p.runs for x in (r.start, r.end)] + [T0 + timedelta(hours=24)]
    gaps = [edges[i + 1] - edges[i] for i in range(0, len(edges) - 1, 2)]
    assert max(gaps) <= timedelta(hours=8) + Q
    assert any(r.reason == "max_gap" for r in p.runs)


def test_no_runs_without_heat_demand():
    p = planner.plan(day(temp=20.0), T0, S)
    assert p.heat_need_kwh == 0 and p.runs == ()


def test_adjacent_blocks_merge_into_one_run():
    prices = {h: 0.40 for h in range(24)} | {4: 0.05, 5: 0.05, 6: 0.05}
    p = planner.plan(day(prices=prices, temp=13.0), T0,
                     planner.PlannerSettings(horizon=timedelta(hours=24), max_gap=timedelta(hours=24)))
    run = next(r for r in p.runs if r.start <= T0 + timedelta(hours=5) < r.end)
    assert run.end - run.start >= timedelta(hours=2)


def test_forecast_prices_are_flagged():
    slots = day(temp=8.0)
    slots = [planner.Slot(x.start, x.price, x.market, forecast=x.start >= T0 + timedelta(hours=12),
                          temp=x.temp) for x in slots]
    p = planner.plan(slots, T0, S)
    assert any(r.forecast_price for r in p.runs) or all(r.end <= T0 + timedelta(hours=12) for r in p.runs)


def test_build_slots_from_jeroen_attributes():
    # shapes as published by the Jeroen.nl integration: prices in UTC, solar hours in Dutch time
    rows = [
        {"start": "2026-10-28T09:00:00+00:00", "end": "2026-10-28T09:15:00+00:00", "price": 0.25, "market": 0.08},
        {"start": "2026-10-28T09:15:00+00:00", "end": "2026-10-28T09:30:00+00:00", "price": 0.26, "market": 0.09,
         "low": 0.2, "high": 0.3, "forecast": True},
        {"start": "bad", "price": 0.1},
        {"start": "2026-10-28T09:30:00+00:00", "price": None},
    ]
    hours = [{"start": "2026-10-28T10:00:00+01:00", "kwh": 2.5}]  # = 09:00 UTC
    temps = {datetime(2026, 10, 28, 9, tzinfo=timezone.utc): 11.0}
    slots = planner.build_slots(rows, hours, temps, fallback_temp=7.0)
    assert len(slots) == 2
    assert slots[0].pv_kw == 2.5 and slots[0].temp == 11.0 and not slots[0].forecast
    assert slots[1].forecast and slots[1].market == 0.09
    assert planner.build_slots(rows[:1], None, None, fallback_temp=7.0)[0].temp == 7.0


def test_prices_keep_runs_out_of_morning_and_evening_peaks():
    # no fixed window: the morning (06-08 UTC) and evening (16-19 UTC) peaks are in the prices
    prices = {h: 0.22 for h in range(24)} | {6: 0.40, 7: 0.40, 16: 0.45, 17: 0.45, 18: 0.45}
    s = planner.PlannerSettings(horizon=timedelta(hours=24), max_gap=timedelta(hours=24))
    result = planner.plan(day(prices=prices, temp=8.0), T0, s)
    assert result.runs
    peak_hours = {6, 7, 16, 17, 18}
    for run in result.runs:
        t = run.start
        while t < run.end:
            assert t.hour not in peak_hours
            t += Q


def test_cheap_evening_is_used_without_fixed_window():
    # 17:00-21:00 NL (16:00-20:00 UTC) cheapest of the day: the planner now uses it
    prices = {h: 0.25 for h in range(24)} | {16: 0.18, 17: 0.18, 18: 0.18, 19: 0.18}
    s = planner.PlannerSettings(horizon=timedelta(hours=24), max_gap=timedelta(hours=24))
    result = planner.plan(day(prices=prices, temp=16.0), T0, s)
    assert any(16 <= r.start.hour < 20 for r in result.runs)


def test_next_run_and_running_now():
    p = planner.plan(day(temp=8.0), T0, S)
    first = p.runs[0]
    assert p.next_run(T0) == first
    assert p.running_now(first.start + Q)
    assert planner.plan([], T0, S) is None


def test_room_below_target_adds_heat_and_warm_room_uses_stored_heat():
    base = planner.plan(day(temp=8.0), T0, S)
    cold = planner.plan(day(temp=8.0), T0, S, living_temp=18.5)
    warm = planner.plan(day(temp=8.0), T0, S, living_temp=19.6)
    assert cold.state_kwh == pytest.approx(S.capacity_kwh_k * 0.5)
    assert cold.heat_need_kwh == pytest.approx(base.heat_need_kwh + S.capacity_kwh_k * 0.5, abs=0.01)
    assert warm.heat_need_kwh == pytest.approx(base.heat_need_kwh - S.capacity_kwh_k * 0.6, abs=0.01)
    assert warm.run_hours < base.run_hours < cold.run_hours


def test_room_state_is_capped_and_ignored_without_heat_demand():
    assert planner.state_heat(10.0, S) == pytest.approx(S.capacity_kwh_k * S.max_state_k)
    assert planner.state_heat(None, S) == 0.0
    summer = planner.plan(day(temp=20.0), T0, S, living_temp=17.0)
    assert summer.heat_need_kwh == 0.0 and not summer.runs


def test_max_gap_still_applies_when_stored_heat_covers_the_need():
    # mild day, very warm house: the corrected need is 0, but no 8 h without a run
    p = planner.plan(day(temp=14.0), T0, S, living_temp=21.0)
    assert p.heat_need_kwh == 0.0
    assert p.runs and all(r.reason == "max_gap" for r in p.runs)


def test_gas_cost_per_kwh_heat():
    s = planner.PlannerSettings(gas_price_m3=1.50)
    assert planner.gas_heat_cost(s) == pytest.approx(1.50 / (9.77 * 0.9))
    assert planner.gas_heat_cost(S) is None


def test_gas_cheaper_quarters_are_valued_at_the_gas_cost():
    # frost and a very expensive evening: heat pump heat costs more than gas there
    prices = {h: 0.25 for h in range(24)} | {h: 0.80 for h in range(16, 22)}
    s = planner.PlannerSettings(horizon=timedelta(hours=24), gas_price_m3=1.50, max_gap=timedelta(hours=24))
    p = planner.plan(day(prices, temp=-5.0), T0, s)
    gas = planner.gas_heat_cost(s)
    assert p.gas_cost_per_kwh_heat == pytest.approx(gas, abs=1e-4)
    assert p.gas_cheaper_quarters == 24          # the six expensive hours
    # the cheap heat pump hours are cheaper than gas, so the runs go there
    assert all(not (16 <= r.start.hour < 22) for r in p.runs)


def test_heat_is_still_asked_for_when_gas_is_cheaper_everywhere():
    # the heat pump only fires the boiler when the thermostat asks for heat, so the
    # plan must still contain runs; they cost gas and no electricity
    s = planner.PlannerSettings(horizon=timedelta(hours=24), gas_price_m3=0.30)
    p = planner.plan(day(temp=8.0), T0, s)
    assert p.gas_cheaper_quarters == 96
    gas = planner.gas_heat_cost(s)
    assert p.runs and any(r.reason == "cheapest" for r in p.runs)
    assert p.run_hours * S.heat_rate_kw >= p.heat_need_kwh
    assert all(r.el_kwh == 0 for r in p.runs)
    assert p.expected_cost == pytest.approx(sum(r.heat_kwh for r in p.runs) * gas, rel=1e-3)


def test_short_pauses_between_runs_are_filled():
    # two cheap hours with one dearer hour in between: one run, not stop-and-start
    prices = {h: 0.40 for h in range(24)} | {10: 0.10, 12: 0.10}
    s = planner.PlannerSettings(horizon=timedelta(hours=24), max_gap=timedelta(hours=24),
                                heat_base_kwh_day=0.0, heat_per_dh=0.0)
    need_two_hours = planner.PlannerSettings(**{**s.__dict__, "heat_base_kwh_day": 2 * s.heat_rate_kw})
    p = planner.plan(day(prices, temp=20.0), T0, need_two_hours)
    assert len(p.runs) == 1
    assert p.runs[0].start == T0 + timedelta(hours=10) and p.runs[0].end == T0 + timedelta(hours=13)


def test_long_pauses_stay_and_zero_disables_filling():
    prices = {h: 0.40 for h in range(24)} | {6: 0.10, 12: 0.10}
    base = dict(horizon=timedelta(hours=24), max_gap=timedelta(hours=24), heat_per_dh=0.0,
                heat_base_kwh_day=2 * S.heat_rate_kw)
    assert len(planner.plan(day(prices, temp=20.0), T0, planner.PlannerSettings(**base)).runs) == 2
    prices = {h: 0.40 for h in range(24)} | {10: 0.10, 12: 0.10}
    off = planner.PlannerSettings(**base, min_pause=timedelta(0))
    assert len(planner.plan(day(prices, temp=20.0), T0, off).runs) == 2


def test_cold_day_becomes_one_long_run():
    # at about 0 C the need fills most of the day: one continuous run, not many short ones
    prices = {h: 0.25 + 0.01 * (h % 5) for h in range(24)}
    p = planner.plan(day(prices, temp=0.0), T0, S)
    assert len(p.runs) == 1 and p.run_hours >= 23


def test_a_run_in_progress_stays_when_the_plan_is_made_again():
    # replanning every 5 minutes must not drop a run that started a quarter ago
    prices = {h: 0.30 for h in range(24)} | {h: 0.20 for h in (2, 3, 4)}
    s = planner.PlannerSettings(horizon=timedelta(hours=24), max_gap=timedelta(hours=24),
                                heat_base_kwh_day=0.0, heat_per_dh=0.0)
    s = planner.PlannerSettings(**{**s.__dict__, "heat_base_kwh_day": 2 * s.heat_rate_kw})
    slots = day(prices, temp=20.0)
    first = planner.plan(slots, T0 + timedelta(hours=2), s)
    run = first.runs[0]
    assert run.start == T0 + timedelta(hours=2)
    later = T0 + timedelta(hours=2, minutes=20)
    again = planner.plan(slots, later, s, keep_until=run.end)
    assert again.running_now(later) and again.runs[0].end >= run.end
    # every 5 minutes through the run: it never drops out
    now, keep = T0 + timedelta(hours=2), None
    while now < run.end:
        p = planner.plan(slots, now, s, keep_until=keep)
        assert p.running_now(now), now
        current = next(r for r in p.runs if r.start <= now < r.end)
        keep, now = current.end, now + timedelta(minutes=5)
