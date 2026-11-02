"""Tests for the executor (writes the target setpoint, with safety rules)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

_PATH = Path(__file__).parents[1] / "custom_components" / "home_ems" / "executor.py"
_SPEC = importlib.util.spec_from_file_location("home_ems_executor", _PATH)
executor = importlib.util.module_from_spec(_SPEC)
sys.modules["home_ems_executor"] = executor
_SPEC.loader.exec_module(executor)

T0 = datetime(2026, 11, 10, 12, 0)
S = executor.ExecutorSettings()


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def run(state, target, current, minute, source="planner", control=True):
    return executor.step(state, target, source, "test", current, at(minute), control, S)


def test_nothing_happens_while_control_is_off():
    action = run(executor.ExecutorState(), 19.3, 19.0, 0, control=False)
    assert (action.write, action.status) == (None, "off")


def test_writes_only_on_a_change():
    state = executor.ExecutorState()
    assert run(state, 19.3, 19.0, 0).write == 19.3
    state.record_write(19.3, "planner", at(0))
    assert run(state, 19.3, 19.3, 1).status == "steady"


def test_same_reason_waits_for_the_minimum_interval_other_reason_does_not():
    state = executor.ExecutorState()
    state.record_write(19.3, "planner", at(0))
    assert run(state, 19.4, 19.3, 3).status == "waiting"
    assert run(state, 19.4, 19.3, 5).write == 19.4
    assert run(state, 18.0, 19.3, 3, source="idle").write == 18.0


def test_change_by_hand_pauses_control():
    state = executor.ExecutorState()
    state.record_write(19.3, "planner", at(0))
    # the thermostat has not reported our write yet: no override, and no second write
    action = run(state, 19.3, 19.0, 1)
    assert action.status == "waiting" and action.write is None
    # after the settle time someone set 20.5: paused for two hours
    action = run(state, 19.3, 20.5, 3)
    assert action.status == "override" and state.override_until == at(3) + S.override_hold
    assert run(state, 19.3, 20.5, 60).status == "override"
    assert run(state, 19.3, 20.5, 3 + 120).write == 19.3


def test_ems_off_writes_the_fallback_once():
    state = executor.ExecutorState()
    state.record_write(18.0, "idle", at(0))
    action = run(state, None, 18.0, 1, source="off")
    assert (action.write, action.status) == (S.fallback_setpoint, "fallback")
    state.record_write(action.write, "off", at(1), steering=False)
    assert run(state, None, S.fallback_setpoint, 2, source="off").status == "off"
    # a change by hand while the EMS is off is not an override
    assert run(state, None, 20.0, 10, source="off").status == "off"


def test_ems_off_without_earlier_writes_does_nothing():
    assert run(executor.ExecutorState(), None, 19.0, 0, source="off").write is None


def test_missing_data_falls_back_after_a_while():
    state = executor.ExecutorState()
    state.record_write(19.3, "floor", at(0))
    assert run(state, None, 19.3, 1, source="unknown").status == "waiting"
    assert run(state, None, 19.3, 10).status == "waiting"
    assert run(state, None, 19.3, 11, source="unknown").write == S.fallback_setpoint
    # data back: the timer starts again
    assert run(state, 19.4, 19.3, 12).write == 19.4
    assert state.unknown_since is None
