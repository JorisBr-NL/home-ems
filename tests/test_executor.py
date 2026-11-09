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


def test_a_write_that_did_not_arrive_is_retried_then_an_error():
    state = executor.ExecutorState()
    state.record_write(19.8, "planner", at(0), before=19.5)
    # after the settle time the thermostat still shows the value from before: retry
    action = run(state, 19.8, 19.5, 3)
    assert (action.write, action.status) == (19.8, "writing")
    state.record_write(19.8, "planner", at(3), before=19.5)
    assert run(state, 19.8, 19.5, 6).write == 19.8
    state.record_write(19.8, "planner", at(6), before=19.5)
    # retries used up: error and a pause, not a change by hand
    action = run(state, 19.8, 19.5, 9)
    assert (action.write, action.status) == (None, "error")
    assert run(state, 19.8, 19.5, 60).status == "error"
    assert run(state, 19.8, 19.5, 9 + 120).write == 19.8


def test_a_write_that_arrived_resets_the_retries():
    state = executor.ExecutorState()
    state.record_write(19.8, "planner", at(0), before=19.5)
    run(state, 19.8, 19.5, 3)
    assert state.retries == 1
    state.record_write(19.8, "planner", at(3), before=19.5)
    assert run(state, 19.8, 19.8, 6).status == "steady"
    assert state.retries == 0


def test_another_value_is_still_a_change_by_hand():
    state = executor.ExecutorState()
    state.record_write(19.8, "planner", at(0), before=19.5)
    assert run(state, 19.8, 21.0, 3).status == "override"


def test_small_changes_wait_for_the_minimum_step_but_start_and_stop_do_not():
    s = executor.ExecutorSettings(min_step=0.3)
    state = executor.ExecutorState()
    state.record_write(19.8, "planner", at(0))
    step = executor.step
    # 0.1 lower, still above the room: not written
    assert step(state, 19.7, "planner", "t", 19.8, at(10), True, s, living=19.5).status == "steady"
    # 0.2 lower and now below the room (stops the heat demand): written
    assert step(state, 19.6, "idle", "t", 19.8, at(10), True, s, living=19.7).write == 19.6
    # without the room temperature only the step counts
    assert step(state, 19.6, "idle", "t", 19.8, at(10), True, s).status == "steady"
    assert step(state, 19.4, "idle", "t", 19.8, at(10), True, s).write == 19.4


def test_write_at_once_when_the_room_catches_up_with_the_setpoint():
    from datetime import timedelta as _td
    st = executor.ExecutorState()
    now = T0
    st.record_write(19.3, "to_top", now)
    s = executor.ExecutorSettings()
    # 2 minutes later the room is at the setpoint: no waiting for the minimum interval
    a = executor.step(st, 19.4, "to_top", "r", 19.3, now + _td(minutes=2), True, s, living=19.3)
    assert a.write == 19.4
    # the room is still below the setpoint: the interval applies
    a = executor.step(st, 19.4, "to_top", "r", 19.3, now + _td(minutes=2), True, s, living=19.2)
    assert a.write is None


def test_executor_state_roundtrip():
    st = executor.ExecutorState(last_written=19.2, last_write=T0, last_source="minimum",
                                override_until=T0, pause_status="error", before=18.5, retries=1)
    again = executor.ExecutorState.from_dict(st.to_dict())
    assert (again.last_written, again.last_write, again.last_source, again.override_until,
            again.pause_status, again.before, again.retries) == (19.2, T0, "minimum", T0, "error", 18.5, 1)
