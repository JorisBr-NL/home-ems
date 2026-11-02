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
