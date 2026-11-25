"""Tests for PV curtailment and the appliance start advice (both shadow mode)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).parents[1] / "custom_components" / "home_ems"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, _DIR / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


curtail = _load("home_ems_curtail", "curtail.py")
appliances = _load("home_ems_appliances", "appliances.py")
planner = _load("home_ems_planner_c", "planner.py")

T0 = datetime(2027, 5, 1, 12, 0, tzinfo=timezone.utc)
A, B = "sensor.inverter_a", "sensor.inverter_b"


def at(minutes):
    return T0 + timedelta(minutes=minutes)


# ---------------- curtailment ----------------

def test_positive_price_keeps_everything_on():
    st = curtail.CurtailState()
    assert curtail.advise(st, {A: 2000, B: 3000}, 4000, 0.01, at(0)).off == ()


def test_smallest_inverter_goes_off_when_it_delivers_less_than_the_export():
    st = curtail.CurtailState()
    adv = curtail.advise(st, {A: 2000, B: 3000}, 2500, -0.02, at(0))
    assert adv.off == (A,)
    # export larger than both together (+ margin): both off
    st2 = curtail.CurtailState()
    assert curtail.advise(st2, {A: 2000, B: 3000}, 5500, -0.02, at(0)).off == (A, B)


def test_no_switching_when_each_inverter_delivers_more_than_the_export():
    adv = curtail.advise(curtail.CurtailState(), {A: 2000, B: 3000}, 1500, -0.02, at(0))
    assert adv.off == () and "more than the export" in adv.reason


def test_stays_off_for_the_minimum_and_counts_avoided_export():
    st = curtail.CurtailState()
    curtail.advise(st, {A: 2000, B: 3000}, 2500, -0.02, at(0))
    # export drops (house uses more): A stays off during the minimum time
    assert curtail.advise(st, {A: 2000, B: 3000}, 1000, -0.02, at(5)).off == (A,)
    assert st.avoided_kwh == pytest.approx(2.0 * 5 / 60)
    assert st.avoided_eur == pytest.approx(0.02 * 2.0 * 5 / 60)
    # price positive again: back on at once
    assert curtail.advise(st, {A: 2000, B: 3000}, 2500, 0.01, at(10)).off == ()


def test_max_off_brings_it_back():
    st = curtail.CurtailState()
    curtail.advise(st, {A: 2000, B: 3000}, 2500, -0.02, at(0))
    adv = curtail.advise(st, {A: 2000, B: 3000}, 1000, -0.02, at(4 * 60))
    assert adv.off == ()


# ---------------- appliances ----------------

def slots(prices, pv=None, start=T0.replace(hour=0)):
    """Quarter slots for 36 h; prices/pv per hour index from `start`."""
    out = []
    for q in range(36 * 4):
        h = q // 4
        out.append(planner.Slot(start=start + q * planner.QUARTER, price=prices.get(h, 0.30),
                                market=0.08, pv_kw=(pv or {}).get(h, 0.0)))
    return out


DISH = appliances.Appliance("Vaatwasser", kwh=1.2, hours=2.0, ready_by=time(7, 0))


def test_cheapest_window_before_the_deadline():
    now = T0.replace(hour=20)       # 20:00 UTC, deadline 07:00 next day
    adv = appliances.best_start(slots({27: 0.10, 28: 0.10}), now, DISH)
    assert adv.start == T0.replace(hour=3) + timedelta(days=1)
    assert adv.cost == pytest.approx(1.2 * 0.10)
    assert adv.cost_now == pytest.approx(1.2 * 0.30)
    assert adv.as_dict()["saving"] == pytest.approx(0.24)


def test_solar_surplus_wins_when_the_deadline_allows():
    now = T0.replace(hour=8)
    late = appliances.Appliance("Wasmachine", kwh=1.0, hours=2.0, ready_by=time(18, 0))
    adv = appliances.best_start(slots({}, pv={11: 3.0, 12: 3.0}), now, late)
    assert adv.start == T0.replace(hour=11) and adv.pv_share == 1.0
    assert adv.cost == pytest.approx(1.0 * 0.08)


def test_no_window_left_before_the_deadline():
    now = T0.replace(hour=6, minute=30)
    tight = appliances.Appliance("Vaatwasser", kwh=1.2, hours=2.0, ready_by=time(7, 0))
    adv = appliances.best_start(slots({}), now, tight)
    # 07:00 is 30 min away: the next 07:00 (tomorrow) is used
    assert adv.deadline == T0.replace(hour=7)
    assert adv.start is None or adv.end <= adv.deadline


def test_parse_appliances():
    items = appliances.parse_appliances([{"naam": "Vaatwasser", "kwh": 1.1, "duur": 2.5, "klaar_voor": 420}])
    assert items[0].ready_by == time(7, 0) and items[0].hours == 2.5
    with pytest.raises(ValueError):
        appliances.parse_appliances([{"kwh": 1}])


# ---------------------------------------------------------------- v0.13.1: solar window

def _sunny_day(flat: bool = True):
    """25 Nov: a fixed tariff (every quarter the same price and export value) and a solar
    forecast that rises from a weak morning to a sunny midday."""
    day = datetime(2026, 11, 25, 0, 0, tzinfo=timezone.utc)      # local = UTC + 1
    pv = {7: 0.3, 8: 0.9, 9: 1.4, 10: 1.8, 11: 1.9, 12: 1.5, 13: 0.9, 14: 0.3}   # UTC hours
    return [planner.Slot(day + i * planner.QUARTER, 0.30, market=0.30 if flat else 0.10,
                         pv_kw=pv.get((day + i * planner.QUARTER).hour, 0.0)) for i in range(96)]


def test_dishwasher_profile_heats_first():
    dish = appliances.Appliance("Vaatwasser", kwh=1.1, hours=2.5)
    profile = dish.profile()
    assert len(profile) == 10 and profile[0] == profile[1] == 2.0
    assert abs(sum(profile) * 0.25 - 1.1) < 1e-9          # the energy stays the same


def test_fixed_tariff_picks_the_sunniest_window_not_the_first_covered_one():
    dish = appliances.Appliance("Vaatwasser", kwh=1.1, hours=2.5, ready_by=time(17, 0))
    evening = datetime(2026, 11, 24, 20, 0, tzinfo=timezone.utc)    # armed the evening before
    slots = _sunny_day()
    advice = appliances.best_start(slots, evening, dish, 0.4)
    local_start = advice.start + timedelta(hours=1)
    assert 10 <= local_start.hour <= 12, local_start             # not 09:00


def test_planned_heat_pump_runs_take_the_morning_sun_first():
    dish = appliances.Appliance("Vaatwasser", kwh=1.1, hours=2.5, ready_by=time(17, 0))
    evening = datetime(2026, 11, 24, 20, 0, tzinfo=timezone.utc)
    slots = _sunny_day()
    # the heat pump runs 08:00-11:00 local at 1.2 kW
    hp = {s.start: 1.2 for s in slots if 7 <= s.start.hour < 10}
    advice = appliances.best_start(slots, evening, dish, 0.4, None, hp)
    assert advice.start.hour >= 10                                # after the heat pump run (UTC)
    assert advice.pv_share is not None and advice.pv_share > 0.5
