"""Arbiter: one owner of the living room setpoint (pure logic, no Home Assistant imports).

The advice, the anti short-cycling rule and the day planner all want something from
the same thermostat. The arbiter turns them into one target setpoint per moment, in a
fixed order of priority:

    switched off > comfort minimum > floor > keep running (minimum run time)
    > planner > PV surplus (start, or hold while still exporting) > idle

Runs are started and held with a setpoint just above the room, because tado modulates
on that gap (small gap = low compressor speed = higher COP). While the current setpoint
still asks for heat with such a small gap, it is kept, so the thermostat is not rewritten
for every 0.1 C the room warms up.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # no runtime import, so the tests can load this file on its own
    from .model import Evaluation


@dataclass(frozen=True)
class ArbiterSettings:
    comfort_min: float = 18.0
    comfort_max: float = 21.5
    start_offset: float = 0.2   # start a run: this far above the room
    hold_offset: float = 0.1    # keep a run going: just above the room
    # No run wanted: the setpoint goes to the comfort minimum, so tado itself only
    # asks for heat as a safety net and the EMS decides when runs happen.
    idle_setpoint: float = 18.0
    # keep the current setpoint during a run while it is above the room and at most this
    # far above the wanted value
    keep_band: float = 0.2


@dataclass(frozen=True)
class Target:
    setpoint: float | None   # None = the EMS has no target (off or no data)
    source: str              # off, unknown, comfort_min, floor, keep_running, planner, pv, idle
    reason: str

    def differs_from(self, current: float | None) -> bool:
        """True when writing the target would change the thermostat (0.1 C steps)."""
        return self.setpoint is not None and (current is None or abs(self.setpoint - current) >= 0.05)


def target_setpoint(evaluation: Evaluation, living_temp: float | None, plan_run_now: bool,
                    enabled: bool = True, settings: ArbiterSettings | None = None,
                    current_setpoint: float | None = None) -> Target:
    s = settings or ArbiterSettings()
    if not enabled:
        return Target(None, "off", "EMS switched off: the thermostat schedule leads")
    # advice values compare as plain strings (StrEnum)
    if evaluation.advice == "unknown" or living_temp is None:
        return Target(None, "unknown", "no heat pump power or living room temperature")

    def clamp(value: float) -> float:
        return round(min(s.comfort_max, max(s.comfort_min, value)), 1)

    def keep(value: float) -> float:
        # the current setpoint still asks for heat with a small gap: leave it as it is
        if (current_setpoint is not None and living_temp < current_setpoint
                and current_setpoint <= min(value + s.keep_band, s.comfort_max)):
            return current_setpoint
        return value

    start = keep(clamp(living_temp + s.start_offset))
    hold = keep(clamp(living_temp + s.hold_offset))
    running = evaluation.heat_pump_running

    if evaluation.advice == "run" and evaluation.cause == "comfort_min":
        return Target(start, "comfort_min", evaluation.reason)
    if evaluation.advice == "run" and evaluation.cause == "floor":
        return Target(start, "floor", evaluation.reason)
    if evaluation.advice == "keep_running":
        return Target(hold, "keep_running", "hold the run until the minimum run time")
    if plan_run_now:
        if running:
            return Target(hold, "planner", "planned run in progress")
        return Target(start, "planner", "planned run starts")
    if evaluation.advice == "run" and evaluation.cause == "pv":
        return Target(start, "pv", evaluation.reason)
    if running and evaluation.pv_surplus_w:
        # still exporting while the heat pump runs: a long run on solar is the cheapest heat
        return Target(hold, "pv", "run on solar surplus: keep it going")
    idle = clamp(s.idle_setpoint)
    if running:
        return Target(idle, "idle", "run not needed any more: let it stop")
    return Target(idle, "idle", evaluation.reason)
