"""Tests for the battery reserve guard (charge a backup battery before it runs empty)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

_PATH = Path(__file__).parents[1] / "custom_components" / "home_ems" / "reserve.py"
_SPEC = importlib.util.spec_from_file_location("home_ems_reserve", _PATH)
reserve = importlib.util.module_from_spec(_SPEC)
sys.modules["home_ems_reserve"] = reserve
_SPEC.loader.exec_module(reserve)

T0 = datetime(2026, 11, 14, 3, 0)
S = reserve.ReserveSettings()   # start 15 %, stop 24 %


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def run(state, soc, minute, grid=True, enabled=True):
    decision = reserve.decide(state, enabled, soc, grid, at(minute), S)
    if decision.action == reserve.START:
        state.record_start(at(minute), {"number.other_battery_max_discharge": 800.0})
    elif decision.action == reserve.STOP:
        state.record_stop(at(minute), fault=decision.status == "error", s=S)
    return decision


def warm(state, soc=30):
    """The grid meter has been reporting for a while."""
    run(state, soc, -5)
    return state


def test_nothing_while_switched_off():
    d = run(warm(reserve.ReserveState()), 10, 0, enabled=False)
    assert (d.action, d.status) == (None, "off")


def test_idle_above_the_start_level():
    d = run(warm(reserve.ReserveState()), 16, 0)
    assert (d.action, d.status) == (None, "idle")


def test_starts_at_the_start_level():
    state = warm(reserve.ReserveState())
    d = run(state, 15, 0)
    assert (d.action, d.status) == (reserve.START, "charging")
    assert state.charging and state.since == at(0)


def test_keeps_charging_in_the_band_and_stops_at_the_stop_level():
    state = warm(reserve.ReserveState())
    run(state, 15, 0)
    for minute, soc in ((1, 16), (10, 20), (20, 23.9)):
        assert run(state, soc, minute).action is None
    d = run(state, 24, 30)
    assert (d.action, d.status) == (reserve.STOP, "idle")
    assert not state.charging and state.restore == {}


def test_no_start_without_grid_values():
    state = reserve.ReserveState()
    d = run(state, 10, 0, grid=False)
    assert (d.action, d.status) == (None, "waiting")


def test_grid_must_report_for_a_minute_before_a_start():
    state = reserve.ReserveState()
    assert run(state, 10, 0).action is None          # grid just back
    assert run(state, 10, 0.5).action is None
    assert run(state, 10, 1).action == reserve.START


def test_grid_loss_stops_the_charge():
    state = warm(reserve.ReserveState())
    run(state, 15, 0)
    d = run(state, 17, 5, grid=False)
    assert (d.action, d.status) == (reserve.STOP, "waiting")


def test_switching_off_stops_the_charge():
    state = warm(reserve.ReserveState())
    run(state, 15, 0)
    d = run(state, 17, 5, enabled=False)
    assert (d.action, d.status) == (reserve.STOP, "off")


def test_anti_flap_minimum_pause_after_a_stop():
    state = warm(reserve.ReserveState())
    run(state, 15, 0)
    run(state, 17, 5, grid=False)                     # grid gone: stop
    run(state, 15, 6)                                  # grid back
    assert run(state, 15, 10).action is None           # within 15 min of the stop
    assert run(state, 15, 20).action == reserve.START


def test_a_charge_that_takes_too_long_is_a_fault():
    state = warm(reserve.ReserveState())
    run(state, 15, 0)
    d = run(state, 15, 180)
    assert (d.action, d.status) == (reserve.STOP, "error")
    assert run(state, 14, 200).status == "error"       # held for an hour
    assert run(state, 14, 245).action == reserve.START


def test_unknown_state_of_charge_keeps_a_running_charge_and_starts_none():
    state = warm(reserve.ReserveState())
    assert run(state, None, 0).status == "waiting"
    run(state, 15, 1)
    assert run(state, None, 2).status == "charging"


def test_state_survives_a_restart():
    state = warm(reserve.ReserveState())
    run(state, 15, 0)
    copy = reserve.ReserveState.from_dict(state.to_dict())
    assert copy.charging and copy.since == at(0)
    assert copy.restore == {"number.other_battery_max_discharge": 800.0}
    assert run(copy, 24, 30).action == reserve.STOP


def test_settings_need_a_band():
    assert reserve.valid(S)
    assert not reserve.valid(reserve.ReserveSettings(start_soc=20, stop_soc=21))
