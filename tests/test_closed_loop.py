"""Closed-loop test: a simple house, heat pump and thermostat around the real arbiter,
model and executor, minute by minute for a day (v0.10.8). Snapshot tests of one call miss
what happens over minutes: compressor pauses, a slow start, the room catching up with the
setpoint, the switch between day and night."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

_DIR = Path(__file__).parents[1] / "custom_components" / "home_ems"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _DIR / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


model = _load("home_ems_model_cl", "model.py")
arbiter = _load("home_ems_arbiter_cl", "arbiter.py")
executor = _load("home_ems_executor_cl", "executor.py")

NIGHT = (time(20, 0), time(5, 0))


def settings_at(now: datetime, latch=None) -> tuple:
    """Like the coordinator: night limits, the day top in the last 2.5 h of the night, and a
    run keeps the top it started with."""
    night = arbiter.in_window(now.time(), *NIGHT)
    if night:
        top = 18.8
        if arbiter.until_window_end(now.time(), NIGHT[1]) <= timedelta(hours=2, minutes=30):
            top = 19.5
        if latch is not None and latch.since is not None and latch.ceiling is not None:
            top = max(top, latch.ceiling)
        return arbiter.ArbiterSettings(living_min=18.5, run_ceiling=top, night=True,
                                       comfort_max=20.0, low_price=0), True
    return arbiter.ArbiterSettings(living_min=19.0, run_ceiling=19.5, comfort_max=20.0,
                                   low_price=0), False


def simulate(outside: float = 5.0, minutes: int = 24 * 60):
    t0 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    room, water = 19.2, 0.0          # water: share of the heat pump output reaching the room
    tracker = model.RunTracker(settings=model.Settings(min_run=timedelta(minutes=30)))
    latch = arbiter.RunLatch()
    ex = executor.ExecutorState()
    es = executor.ExecutorSettings()
    setpoint, on_for, compressor = 18.5, 0, False
    log = []
    for m in range(minutes):
        now = t0 + timedelta(minutes=m)
        s, night = settings_at(now, latch)
        reported = round(room, 1)                       # tado reports 0.1 steps
        # thermostat and heat pump: heat while the setpoint is above the reported room
        demand = setpoint > reported + 0.01
        if demand:
            on_for += 1
        else:
            on_for = 0
        # start takes 3 minutes; the compressor pauses 6 minutes after every 50 minutes
        compressor = demand and on_for > 3 and (on_for - 3) % 56 < 50
        power = 900.0 if compressor else 5.0
        inputs = model.Inputs(heat_pump_power_w=power, flow_temp=None, living_temp=reported,
                              grid_power_w=300.0)
        evaluation = model.evaluate(inputs, tracker, now,
                                    model.Settings(min_run=timedelta(minutes=30),
                                                   floor_enabled=not night))
        steer = model.Evaluation(**{**evaluation.__dict__,
                                    "heat_pump_running": latch.running(compressor, now, timedelta(minutes=10))})
        target = arbiter.target_setpoint(steer, reported, False, settings=s,
                                         current_setpoint=setpoint, pv_hold=False)
        latch.update(target.source, now, s.run_ceiling)
        action = executor.step(ex, target.setpoint, target.source, target.reason, setpoint, now,
                               True, es, living=reported)
        if action.write is not None:
            ex.record_write(action.write, target.source, now, before=setpoint)
            setpoint = action.write
        # house: 15 kWh/K, loss 0.25 kW/K, 0.5 kW gains; heat reaches the room with a lag
        water += ((1.0 if compressor else 0.0) - water) / 20
        heat = (7.0 - 0.4 * outside) * water
        room += (heat - 0.25 * (room - outside) + 0.5) / 15 / 60
        log.append((now, room, setpoint, compressor, target.source, night))
    return log


def runs(log):
    out, start = [], None
    for now, _room, _sp, comp, _src, _night in log:
        if comp and start is None:
            start = now
        if not comp and start is not None:
            out.append((start, now))
            start = None
    return out


def test_a_day_has_few_long_runs_and_stays_within_the_limits():
    log = simulate()
    # runs as the EMS sees them: merge compressor gaps up to 10 minutes
    merged = []
    for a, b in runs(log):
        if merged and a - merged[-1][1] <= timedelta(minutes=10):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    assert merged, "the house needs heat at 5 C"
    assert len(merged) <= 4
    # no new start within an hour of the end of a run (no night run followed by a day start)
    assert all(b2[0] - a2[1] >= timedelta(hours=1) for a2, b2 in zip(merged, merged[1:]))
    assert all(b - a >= timedelta(minutes=30) for a, b in merged[:-1])
    assert min(r for _, r, *_ in log) >= 18.0
    night_rooms = [r for now, r, _sp, _c, _s, night in log if night and now.hour >= 22]
    assert max(night_rooms) <= 19.6        # the night may start warm, but no night run to 20
    day_rooms = [r for now, r, *_rest in log if 8 <= now.hour < 20]
    assert max(day_rooms) <= 19.8


def test_the_setpoint_is_never_left_at_the_room_during_a_run():
    log = simulate()
    stuck, worst = 0, 0
    for _now, room, sp, _comp, src, _night in log:
        if src not in ("idle", "off", "unknown") and sp <= round(room, 1) + 0.01:
            stuck += 1
            worst = max(worst, stuck)
        else:
            stuck = 0
    assert worst <= 2
