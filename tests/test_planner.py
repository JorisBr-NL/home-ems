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


def test_peak_penalty_keeps_runs_out_of_the_evening_window():
    # flat prices: without a penalty the earliest blocks win on ties; make 17:00-21:00 NL
    # (16:00-20:00 UTC) slightly cheaper so they would be chosen without the penalty
    prices = {h: 0.25 for h in range(24)} | {16: 0.24, 17: 0.24, 18: 0.24, 19: 0.24}
    base = dict(horizon=timedelta(hours=24), max_gap=timedelta(hours=24))
    slots = day(prices=prices, temp=16.0)  # small need: 2 run-hours
    without = planner.plan(slots, T0, planner.PlannerSettings(peak_penalty=0.0, **base))
    with_penalty = planner.plan(slots, T0, planner.PlannerSettings(**base))

    def in_peak(run):
        tz = planner.ZoneInfo("Europe/Amsterdam")
        t, quarters = run.start, []
        while t < run.end:
            quarters.append(t.astimezone(tz).time())
            t += Q
        return any(planner.time(17, 30) <= q < planner.time(20, 0) for q in quarters)

    assert any(in_peak(r) for r in without.runs)
    assert not any(in_peak(r) for r in with_penalty.runs)
    # the penalty only steers the choice; the expected cost is the real price
    assert with_penalty.expected_cost >= without.expected_cost


def test_next_run_and_running_now():
    p = planner.plan(day(temp=8.0), T0, S)
    first = p.runs[0]
    assert p.next_run(T0) == first
    assert p.running_now(first.start + Q)
    assert planner.plan([], T0, S) is None
