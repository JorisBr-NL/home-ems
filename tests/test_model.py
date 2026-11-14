"""Tests for the pure EMS model (no Home Assistant needed)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Load model.py directly so the tests do not import the HA-dependent package __init__.
_PATH = Path(__file__).parents[1] / "custom_components" / "home_ems" / "model.py"
_SPEC = importlib.util.spec_from_file_location("home_ems_model", _PATH)
model = importlib.util.module_from_spec(_SPEC)
sys.modules["home_ems_model"] = model
_SPEC.loader.exec_module(model)

Advice, Inputs, RunTracker, evaluate = model.Advice, model.Inputs, model.RunTracker, model.evaluate

DAY = datetime(2026, 10, 24)
CET = ZoneInfo("Europe/Amsterdam")


def inputs(power=5.0, flow=21.0, living=19.5, grid=300.0, price=None):
    # a day with a morning and an evening price peak; `price` is the current quarter
    day = (0.20,) * 72 + (0.40,) * 24 if price is not None else ()
    return Inputs(heat_pump_power_w=power, flow_temp=flow, living_temp=living, grid_power_w=grid,
                  price_now=price, day_prices=day)


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
    result = evaluate(inputs(flow=19.2, living=19.3), RunTracker(), at(10))
    assert result.advice is Advice.RUN
    assert "floor" in result.reason


def test_no_run_while_floor_water_above_room():
    # floor water just above the living room: comfort is fine, no run needed
    result = evaluate(inputs(flow=19.5, living=19.4), RunTracker(), at(16, 25))
    assert result.advice is Advice.WAIT
    assert result.setpoint_hint is None


def test_setpoint_hint_just_above_living_room_when_run_advised():
    result = evaluate(inputs(flow=19.0, living=19.1), RunTracker(), at(10))
    assert result.advice is Advice.RUN
    assert result.setpoint_hint == 19.3


def test_no_floor_run_when_living_room_warm():
    # 26 Oct 2026, 20:55: living room 19.8 C (comfort 19.0 C), floor water below it
    result = evaluate(inputs(flow=19.6, living=19.8), RunTracker(), at(20, 55))
    assert result.advice is Advice.WAIT
    assert "warm enough" in result.reason


def test_floor_rule_applies_just_below_comfort_band():
    assert evaluate(inputs(flow=19.3, living=19.4), RunTracker(), at(10)).advice is Advice.RUN
    assert evaluate(inputs(flow=19.4, living=19.5), RunTracker(), at(10)).advice is Advice.WAIT


def test_no_peak_floor_run_when_living_room_warm():
    result = evaluate(inputs(flow=18.2, living=19.6, price=0.40), RunTracker(), at(18))
    assert result.advice is Advice.WAIT


def test_run_on_pv_surplus():
    result = evaluate(inputs(flow=21.5, living=19.5, grid=-1500), RunTracker(), at(12))
    assert result.advice is Advice.RUN
    assert result.pv_surplus_w == 1500


def test_no_pv_run_when_floor_full():
    result = evaluate(inputs(flow=23.0, living=19.5, grid=-1500), RunTracker(), at(12))
    assert result.advice is Advice.WAIT


def test_price_peak_waits_unless_floor_far_below():
    tracker = RunTracker()
    result = evaluate(inputs(flow=19.0, living=19.3, price=0.40), tracker, at(8))
    assert result.advice is Advice.WAIT and result.in_peak and "price peak" in result.reason
    assert evaluate(inputs(flow=18.2, living=19.3, price=0.40), tracker, at(8, 5)).advice is Advice.RUN


def test_no_fixed_evening_window():
    # 18:00 at a normal price is no peak: the floor rule applies as at any other time
    result = evaluate(inputs(flow=19.0, living=19.3, price=0.22), RunTracker(), at(18))
    assert not result.in_peak and result.advice is Advice.RUN


def test_is_expensive_takes_the_top_share_of_the_day():
    day = (0.20,) * 60 + (0.40,) * 8 + (0.30,) * 28   # 96 quarters, top 25% = 24 quarters
    assert model.is_expensive(0.40, day, 0.25)
    assert model.is_expensive(0.30, day, 0.25)        # the 24th most expensive is 0.30
    assert not model.is_expensive(0.20, day, 0.25)
    assert not model.is_expensive(0.40, day, 0.0)     # share 0 = never a peak
    assert not model.is_expensive(None, day, 0.25) and not model.is_expensive(0.40, (), 0.25)


def quarter_rows(day, prices):
    """Jeroen.nl-style rows (`today` attribute), UTC start times, for a local day in CET."""
    first = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=1)
    return [{"start": (first + i * timedelta(minutes=15)).isoformat(), "price": p}
            for i, p in enumerate(prices)]


def test_price_peak_ranks_the_whole_day_not_what_is_left():
    # Nov 2026 (CET): morning and evening peak at 0.40, rest 0.20; at 23:30 only two
    # cheap quarters are left, which must not count as a peak
    day = [0.20] * 96
    day[28:36] = [0.40] * 8      # 07:00-09:00
    day[68:84] = [0.40] * 16     # 17:00-21:00
    rows = quarter_rows(datetime(2026, 11, 2), day)
    rows += quarter_rows(datetime(2026, 11, 3), [0.90] * 96)    # tomorrow is left out
    late = datetime(2026, 11, 2, 23, 30, tzinfo=CET)
    price_now, prices = model.prices_today(rows, late)
    assert price_now == 0.20 and len(prices) == 96
    assert not model.is_expensive(price_now, prices, 0.25)
    # a flat price has no peak (also what is left of a day)
    assert not model.is_expensive(price_now, prices[-2:], 0.25)
    assert not model.is_expensive(0.25, (0.25,) * 96, 0.25)
    evening = datetime(2026, 11, 2, 18, 0, tzinfo=CET)
    assert model.is_expensive(*model.prices_today(rows, evening), 0.25)


def test_prices_today_skips_bad_rows():
    rows = [{"start": "2026-11-02T10:00:00+00:00", "price": "x"}, {"price": 0.3},
            {"start": "2026-11-02T10:00:00", "price": 0.3},             # no time zone
            {"start": "2026-11-02T10:00:00+00:00", "price": 0.25}]
    assert model.prices_today(rows, datetime(2026, 11, 2, 11, 5, tzinfo=CET)) == (0.25, (0.25,))
    assert model.prices_today([], datetime(2026, 11, 2, 11, 5, tzinfo=CET)) == (None, ())


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
    step(tracker, 0, 11, power=5, flow=19.2, living=19.3)
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


def test_kitchen_below_comfort_minimum_asks_for_a_run_also_in_a_peak():
    cold = Inputs(heat_pump_power_w=5.0, flow_temp=21.0, living_temp=19.4, grid_power_w=300.0,
                  price_now=0.40, day_prices=(0.20,) * 72 + (0.40,) * 24, kitchen_temp=17.4)
    result = evaluate(cold, RunTracker(), at(18))
    assert result.advice is Advice.RUN and result.cause == "comfort_min"
    assert "kitchen" in result.reason and result.in_peak


def test_floor_margin_ignored_while_the_floor_pump_stands_still():
    still = Inputs(heat_pump_power_w=5.0, flow_temp=18.0, living_temp=19.3, grid_power_w=300.0,
                   flow_rate_lph=10.0)
    result = evaluate(still, RunTracker(), at(10))
    assert result.floor_margin is None and result.advice is Advice.WAIT
    pumping = Inputs(heat_pump_power_w=5.0, flow_temp=18.0, living_temp=19.3, grid_power_w=300.0,
                     flow_rate_lph=274.0)
    result = evaluate(pumping, RunTracker(), at(10))
    assert result.floor_margin == -1.3 and result.cause == "floor"


def test_export_at_negative_market_price_is_counted():
    tracker = RunTracker()
    export = dict(heat_pump_power_w=5.0, flow_temp=22.0, living_temp=19.3, grid_power_w=-2000.0)
    evaluate(Inputs(**export, market_now=-0.01), tracker, at(13))
    evaluate(Inputs(**export, market_now=-0.01), tracker, at(13, 3))   # 3 min at 2 kW
    evaluate(Inputs(**export, market_now=0.02), tracker, at(13, 6))    # positive: not counted
    assert tracker.stats.negative_price_export_kwh == pytest.approx(0.1)


def test_market_price_now_from_today_rows():
    rows = [{"start": "2026-11-02T10:00:00+00:00", "price": 0.25, "market": -0.004}]
    assert model.prices_today(rows, datetime(2026, 11, 2, 11, 5, tzinfo=CET), "market")[0] == -0.004


def test_surplus_counts_what_the_running_heat_pump_uses():
    # 800 W of solar goes into the heat pump, 200 W still exported: surplus is 1000 W
    result = evaluate(inputs(power=800.0, grid=-200.0), RunTracker(), at(12))
    assert result.pv_surplus_w == 1000
    # importing more than the heat pump uses: no surplus
    assert evaluate(inputs(power=800.0, grid=900.0), RunTracker(), at(12)).pv_surplus_w == 0
    # at rest only the export counts (standby power is not added)
    assert evaluate(inputs(power=5.0, grid=-200.0), RunTracker(), at(12)).pv_surplus_w == 200


def test_surplus_is_never_more_than_the_solar_production():
    i = Inputs(heat_pump_power_w=1800.0, flow_temp=21.0, living_temp=19.5, grid_power_w=900.0,
               pv_power_w=700.0)
    assert evaluate(i, RunTracker(), at(12)).pv_surplus_w == 700


def test_battery_charging_counts_as_surplus_and_discharging_does_not():
    base = dict(heat_pump_power_w=5.0, flow_temp=21.0, living_temp=19.5, grid_power_w=0.0)
    # batteries hold the meter at zero while charging 800 W: 800 W the heat pump could use
    assert evaluate(Inputs(**base, battery_charge_w=800.0), RunTracker(), at(12)).pv_surplus_w == 800
    # discharging: no surplus
    assert evaluate(Inputs(**base, battery_charge_w=-500.0), RunTracker(), at(12)).pv_surplus_w == 0


def test_solar_start_waits_for_a_steady_five_minute_surplus():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    run = model.Evaluation(advice=model.Advice.RUN, reason="PV surplus available",
                           heat_pump_running=False, floor_margin=None, pv_surplus_w=900.0,
                           in_peak=False, cause="pv")
    burst = [(now - timedelta(minutes=m), w) for m, w in ((5, 0.0), (4, 0.0), (3, 0.0), (2, 900.0), (1, 900.0), (0, 900.0))]
    assert model.average_surplus(burst, now) < 650
    held = model.confirm_pv_start(run, model.average_surplus(burst, now), 650, True)
    assert (held.advice, held.cause) == (model.Advice.WAIT, None) and "steady" in held.reason
    steady = [(now - timedelta(minutes=m), 900.0) for m in range(6)]
    assert model.confirm_pv_start(run, model.average_surplus(steady, now), 650, True) is run
    # right after a restart there is no five-minute history yet
    assert model.average_surplus([(now, 900.0)], now) == 0.0
    # the forecast for the coming hour drops: no start
    assert model.confirm_pv_start(run, 900.0, 650, False).advice == model.Advice.WAIT
    # other advice is left alone
    other = model.Evaluation(advice=model.Advice.RUN, reason="x", heat_pump_running=False,
                             floor_margin=None, pv_surplus_w=0.0, in_peak=False, cause="floor")
    assert model.confirm_pv_start(other, 0.0, 650, False) is other


def test_solar_forecast_for_the_coming_hour():
    assert model.pv_forecast_ok([1.2, 1.1, 1.0, 0.9], 0.4, 650)       # ~650 W expected
    assert not model.pv_forecast_ok([0.5, 0.5, 0.4, 0.4], 0.4, 650)   # ~50 W expected
    assert model.pv_forecast_ok([], 0.4, 650)                          # no forecast: no hold


def test_kitchen_counts_half_a_degree_below_the_comfort_minimum():
    warmish = Inputs(heat_pump_power_w=5.0, flow_temp=21.0, living_temp=19.4, grid_power_w=300.0,
                     kitchen_temp=17.8)
    assert evaluate(warmish, RunTracker(), at(12)).cause != "comfort_min"


def test_comfort_minimum_run_goes_on_until_a_little_above_the_limit():
    tracker = RunTracker()
    off = Inputs(heat_pump_power_w=5.0, flow_temp=21.0, living_temp=19.0, grid_power_w=300.0,
                 kitchen_temp=17.4)
    assert evaluate(off, tracker, at(2)).cause == "comfort_min"
    on = Inputs(heat_pump_power_w=900.0, flow_temp=25.0, living_temp=19.0, grid_power_w=300.0,
                kitchen_temp=17.6)
    evaluate(on, tracker, at(2))
    later = evaluate(on, tracker, at(3))                 # past the minimum run time
    assert (later.advice, later.cause) == (Advice.RUNNING, "comfort_min")
    warm = Inputs(heat_pump_power_w=900.0, flow_temp=25.0, living_temp=19.0, grid_power_w=300.0,
                  kitchen_temp=17.9)
    assert evaluate(warm, tracker, at(4)).cause is None


def test_floor_rule_is_relative_to_the_minimum_and_off_at_night():
    from dataclasses import replace as _r
    cold_floor = Inputs(heat_pump_power_w=5.0, flow_temp=17.0, living_temp=18.7, grid_power_w=300.0)
    night = model.Settings(floor_reference=18.5, floor_enabled=False)
    assert evaluate(cold_floor, RunTracker(), at(2), night).cause != "floor"
    day = model.Settings(floor_reference=18.5, floor_enabled=True, floor_rule_band=0.2)
    assert evaluate(cold_floor, RunTracker(), at(2), day).cause is None      # 18.7 >= 18.5 + 0.2
    assert evaluate(_r(cold_floor, living_temp=18.6), RunTracker(), at(2), day).cause == "floor"


def test_no_solar_start_right_after_a_run_ended():
    from datetime import datetime, timedelta, timezone
    run = model.Evaluation(advice=model.Advice.RUN, reason="PV surplus available",
                           heat_pump_running=False, floor_margin=None, pv_surplus_w=2000.0,
                           in_peak=False, cause="pv")
    held = model.confirm_pv_start(run, 2000.0, 700, True, since_run_end=timedelta(minutes=5))
    assert (held.advice, held.cause) == (model.Advice.WAIT, None) and "just ended" in held.reason
    assert model.confirm_pv_start(run, 2000.0, 700, True, since_run_end=timedelta(minutes=30)) is run
    assert model.confirm_pv_start(run, 2000.0, 700, True, since_run_end=None) is run
    # the pause is only for solar starts
    floor = model.Evaluation(advice=model.Advice.RUN, reason="x", heat_pump_running=False,
                             floor_margin=-0.5, pv_surplus_w=0.0, in_peak=False, cause="floor")
    assert model.confirm_pv_start(floor, 0.0, 700, True, since_run_end=timedelta(0)) is floor
    # the plain five-minute average smooths a passing cloud, also without a full window
    now = datetime(2026, 11, 12, 10, 50, tzinfo=timezone.utc)
    pv = [(now - timedelta(minutes=m), w) for m, w in ((4, 4500.0), (3, 4500.0), (2, 4500.0), (1, 4500.0), (0, 300.0))]
    assert model.average_power(pv, now) == 3660.0
    assert model.average_power([(now, 300.0)], now) == 300.0
    assert model.average_power([], now) is None
