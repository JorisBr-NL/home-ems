"""Tests for appliances that wait for a good moment (v0.13.0)."""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).parents[1] / "custom_components" / "home_ems"
# appliance_control imports .appliances: load both as a small package
_pkg = types.ModuleType("hems_ac")
_pkg.__path__ = [str(_DIR)]
sys.modules["hems_ac"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"hems_ac.{name}", _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"hems_ac.{name}"] = module
    spec.loader.exec_module(module)
    return module


ap = _load("appliances")
ac = _load("appliance_control")
TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)
DISH = ap.Appliance("Vaatwasser", kwh=1.1, hours=2.5, ready_by=time(18, 0),
                    plug="switch.vaatwasser", max_wait=6.0)          # 440 W


def advice(start, deadline=NOW.replace(hour=18)):
    return ap.StartAdvice("Vaatwasser", start, None, deadline, None, None, None)


def test_power_and_parse_with_plug_and_max_wait():
    assert DISH.power_w == pytest.approx(440.0)
    items = ap.parse_appliances([{"naam": "Wasmachine", "kwh": 1.0, "duur": 2, "klaar_voor": "17:00",
                                  "stekker": "switch.wasmachine", "max_wachten": 8}])
    assert (items[0].plug, items[0].max_wait) == ("switch.wasmachine", 8.0)
    assert ap.parse_appliances([{"naam": "Droger"}])[0].plug is None
    with pytest.raises(ValueError):
        ap.parse_appliances([{"naam": "X", "stekker": "light.keuken"}])


def test_waits_for_the_best_window_and_starts_on_solar_before_it():
    later = advice(NOW.replace(hour=12))
    d = ac.decide(DISH, NOW, NOW, later, available_w=100.0)
    assert not d.start and "12:00" in d.detail
    # solar that covers the machine starts it before the planned window
    d = ac.decide(DISH, NOW, NOW + timedelta(minutes=30), later, available_w=500.0)
    assert (d.start, d.reason) == (True, ac.SOLAR)
    # the planned window has come
    d = ac.decide(DISH, NOW, NOW.replace(hour=12), later, available_w=0.0)
    assert (d.start, d.reason) == (True, ac.PLANNED)


def test_deadline_and_timeout_start_it_anyway():
    much_later = advice(NOW.replace(hour=17))
    patient = ap.Appliance("Vaatwasser", kwh=1.1, hours=2.5, ready_by=time(18, 0), max_wait=12.0)
    # 18:00 ready and 2.5 h long: at 15:30 at the latest
    assert ac.decide(patient, NOW, NOW.replace(hour=15, minute=29), much_later, 0.0).start is False
    d = ac.decide(patient, NOW, NOW.replace(hour=15, minute=30), much_later, 0.0)
    assert (d.start, d.reason) == (True, ac.DEADLINE)
    # waited max_wait hours (armed at 07:00)
    d = ac.decide(DISH, NOW.replace(hour=7), NOW.replace(hour=13), much_later, 0.0)
    assert (d.start, d.reason) == (True, ac.TIMEOUT)


def test_without_plan_data_it_waits_for_solar_or_the_timeout():
    assert ac.decide(DISH, NOW, NOW + timedelta(hours=1), None, None).start is False
    assert ac.decide(DISH, NOW, NOW + timedelta(hours=1), None, 600.0).reason == ac.SOLAR
    assert ac.decide(DISH, NOW, NOW + timedelta(hours=6), None, None).reason == ac.TIMEOUT


def test_available_power_is_export_plus_battery_charging():
    assert ac.available_power(-300.0, 500.0) == 800.0     # exporting 300 W, batteries take 500 W
    assert ac.available_power(400.0, 0.0) == 0.0          # importing
    assert ac.available_power(None, 500.0) is None


def test_equal_costs_pick_the_window_with_the_most_solar():
    Slot = types.SimpleNamespace
    base = NOW.replace(hour=0)
    slots = [Slot(start=base + k * ap.QUARTER, price=0.25, market=0.25,
                  pv_kw=3.0 if 11 <= (base + k * ap.QUARTER).hour < 14 else 0.0)
             for k in range(24 * 4)]
    adv = ap.best_start(slots, NOW, DISH)
    assert adv.start.hour == 11 and adv.pv_share == pytest.approx(1.0)


def test_without_plan_data_the_deadline_still_holds_and_max_wait_is_optional():
    free = ap.Appliance("Wasmachine", kwh=1.0, hours=2.0, ready_by=time(17, 0), plug="switch.w")
    assert free.max_wait is None
    assert ac.decide(free, NOW, NOW.replace(hour=14, minute=59), None, 0.0).start is False
    d = ac.decide(free, NOW, NOW.replace(hour=15), None, 0.0)    # 17:00 minus 2 h
    assert (d.start, d.reason) == (True, ac.DEADLINE)


def _adv(start, end, deadline, cost, pv):
    return ap.StartAdvice("Vaatwasser", start, end, deadline, cost, None, pv)


def test_ask_only_when_finishing_later_is_clearly_better():
    deadline = NOW.replace(hour=17)
    early = _adv(NOW.replace(hour=10), NOW.replace(hour=12, minute=30), deadline, 0.30, 0.2)
    later_end = deadline + timedelta(hours=5)
    cheaper = _adv(later_end - timedelta(hours=2.5), later_end, deadline + ac.LATER_WINDOW, 0.20, 0.0)
    sunnier = _adv(later_end - timedelta(hours=2.5), later_end, deadline + ac.LATER_WINDOW, 0.30, 0.9)
    same = _adv(later_end - timedelta(hours=2.5), later_end, deadline + ac.LATER_WINDOW, 0.28, 0.3)
    in_time = _adv(NOW.replace(hour=11), NOW.replace(hour=13, minute=30), deadline + ac.LATER_WINDOW, 0.10, 0.9)
    assert ac.later_is_better(early, cheaper)        # saves 0.10 EUR
    assert ac.later_is_better(early, sunnier)        # 70 % more on solar
    assert not ac.later_is_better(early, same)       # not worth asking
    assert not ac.later_is_better(early, in_time)    # it is ready in time anyway
    assert not ac.later_is_better(None, cheaper)
    too_late = _adv(None, None, deadline, None, None)
    assert not ac.later_is_better(too_late, cheaper)  # the deadline rule starts it now


def test_an_agreed_later_moment_has_no_timeout():
    late = advice(NOW.replace(hour=20), deadline=NOW.replace(hour=23))
    assert ac.decide(DISH, NOW, NOW + timedelta(hours=7), late, 0.0, approved_later=True).start is False
    assert ac.decide(DISH, NOW, NOW + timedelta(hours=7), late, 0.0).reason == ac.TIMEOUT
