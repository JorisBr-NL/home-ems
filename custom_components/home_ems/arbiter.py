"""Arbiter: one owner of the living room setpoint (pure logic, no Home Assistant imports).

The advice, the anti short-cycling rule and the day planner all want something from
the same thermostat. The arbiter turns them into one target setpoint per moment, in a
fixed order of priority:

    switched off > comfort minimum > floor > keep running (minimum run time) > bedroom
    below target before bedtime > living room minimum > PV surplus > planner > run on to
    the top > off

The living room minimum is the comfort setpoint by day and the night minimum at night;
the top is the comfort setpoint + headroom by day (up to the comfort maximum on solar or a
low price) and the night ceiling at night.

The EMS owns the switch point (v0.10.5). There are two states and nothing in between:

- off: the setpoint is the living room minimum minus a margin, so tado never asks for
  heat by itself (only as a safety net when the EMS or Home Assistant is down);
- on: the EMS starts the run with a setpoint just above the room and moves it up with the
  room, never down. A run goes on until the room reaches the top (comfort setpoint +
  headroom, or the night ceiling), then the setpoint drops to the off level at once. A
  setpoint equal to the room is never left standing: tado and the heat pump would then
  start and stop on their own.

A planned run only starts while the room is at least `start_band` below the top, so a run
has room to climb and lasts long (heat pumps like long runs at low power). The living
room minimum starts a run by itself, a little above the minimum, because the room keeps
cooling for a while after the start (the water and the floor warm up first).

Runs are started and held with a setpoint just above the room, because tado modulates
on that gap (small gap = low compressor speed = higher COP). Under a low price the gap is
the largest gap. On solar the gap follows the grid meter (`PvBoost`): one step up while
power still flows out, one step down while power comes in, so the heat pump settles where
it uses the solar power and no grid power. Otherwise the gap stays small and the heat pump
runs gently. During a run the setpoint follows the room up only (room + hold offset).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # no runtime import, so the tests can load this file on its own
    from .model import Evaluation


OFF_FLOOR = 15.0   # the off level never goes below this (frost and comfort safety)


@dataclass(frozen=True)
class ArbiterSettings:
    comfort_min: float = 18.0
    comfort_max: float = 21.5
    start_offset: float = 0.2   # start a run: this far above the room
    hold_offset: float = 0.1    # keep a run going: just above the room
    # Living room minimum: the EMS starts a run itself at this temperature (plus
    # `min_lead`). Off level = minimum - off_margin: no run wanted, tado only asks for heat
    # as a safety net and the EMS decides when runs happen.
    living_min: float = 18.0
    off_margin: float = 0.5
    min_lead: float = 0.1
    # a planned run starts only while the room is at least this far below the top
    start_band: float = 0.3
    # Highest setpoint for a run that is not on solar (comfort setpoint + headroom). The
    # setpoint follows the room up during a run; without a ceiling a long run would
    # push the room up to the comfort maximum. Runs on solar may go higher (storage).
    run_ceiling: float = 20.0
    # At night (see in_window) the ceiling holds for every run, also with a low price
    # boost: the house may cool down, nobody needs 20 °C in the middle of the night.
    night: bool = False
    # Variable gap, at most gap_max: on solar it follows the grid meter (PvBoost: steps of
    # boost_step while the 5-minute average export or import is above boost_margin_w, 0 = off); at a price
    # at or below low_price (all-in EUR/kWh, 0 = off) it is gap_max at once. With a boost
    # the ceiling is the comfort maximum (heat stored in the house).
    gap_max: float = 0.6
    boost_margin_w: float = 300.0
    boost_step: float = 0.1
    low_price: float = 0.20


@dataclass(frozen=True)
class Target:
    setpoint: float | None   # None = the EMS has no target (off or no data)
    source: str              # off, unknown, comfort_min, floor, minimum, keep_running, bedroom,
                             # planner, pv, to_top, idle
    reason: str
    boost: str | None = None  # "solar surplus" or "low price" when the gap was raised
    boost_k: float = 0.0      # how far the gap was raised above the normal gap

    def differs_from(self, current: float | None) -> bool:
        """True when writing the target would change the thermostat (0.1 C steps)."""
        return self.setpoint is not None and (current is None or abs(self.setpoint - current) >= 0.05)


@dataclass
class PvBoost:
    """Extra gap on solar, steered by the grid meter (K above the normal gap).

    `grid_w` is the grid power minus what the batteries charge (positive = import): power
    going into the batteries could also go into the heat pump, and heat in the house comes
    first (COP ~5 against ~12% battery losses). Every `interval` the average of the
    readings since the last step decides: above the margin going out, the extra gap goes
    up one step; above the margin coming in, it halves (rounded down to a step), so a cloud
    takes the extra gap away fast; within the margin it stays.
    The average keeps a cooker or a kettle from moving the gap. At rest without export it
    is 0 at once. The surplus is not used here: it includes what the heat pump already
    takes, and running harder on that only pulls power from the grid.
    """

    extra: float = 0.0
    decided: datetime | None = None
    samples: list[float] = field(default_factory=list)

    def reset(self) -> float:
        self.extra, self.decided, self.samples = 0.0, None, []
        return self.extra

    def update(self, grid_w: float | None, running: bool | None, now: datetime,
               interval: timedelta, s: ArbiterSettings) -> float:
        top = max(0.0, s.gap_max - s.hold_offset)
        if s.boost_margin_w <= 0 or top == 0:
            return self.reset()
        if grid_w is None:
            # a grid reading that drops out for a minute keeps the step reached so far
            return self.extra
        if not running and grid_w >= -s.boost_margin_w:
            return self.reset()
        self.samples.append(grid_w)
        if self.decided is None:
            self.decided = now
            return self.extra
        if now - self.decided < interval:
            return self.extra
        average = sum(self.samples) / len(self.samples)
        self.samples, self.decided = [], now
        if average < -s.boost_margin_w:
            self.extra = round(min(top, self.extra + s.boost_step), 2)
        elif average > s.boost_margin_w:
            steps = math.floor(round(self.extra / 2 / s.boost_step, 6))
            self.extra = round(max(0.0, steps * s.boost_step), 2)
        return self.extra


def boost(pv_extra: float, price_now: float | None,
          s: ArbiterSettings) -> tuple[str | None, float]:
    """Why the gap is raised and the extra gap on solar: a low price wins (largest gap)."""
    if s.low_price > 0 and price_now is not None and price_now <= s.low_price:
        return "low price", 0.0
    if pv_extra > 0:
        return "solar surplus", pv_extra
    return None, 0.0


def in_window(moment: time, start: time, end: time) -> bool:
    """`moment` lies in [start, end); a window past midnight (23:00-06:00) wraps."""
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def night_ceiling(day_ceiling: float, night: float, comfort_min: float) -> float:
    """Run ceiling at night: the night value, never below the comfort minimum and never
    above the day ceiling. (A run still climbs to at least the living room minimum plus
    the start band, see target_setpoint.)"""
    return min(day_ceiling, max(night, comfort_min))


def target_setpoint(evaluation: Evaluation, living_temp: float | None, plan_run_now: bool,
                    enabled: bool = True, settings: ArbiterSettings | None = None,
                    current_setpoint: float | None = None, bedroom_call: bool = False,
                    price_now: float | None = None, pv_extra: float = 0.0,
                    prev_raised: float = 0.0) -> Target:
    """`prev_raised`: how far the gap was raised in the previous target (its boost_k). The
    setpoint follows the room up only, but a raise that is taken back (less sun) comes off
    the setpoint again."""
    s = settings or ArbiterSettings()
    if not enabled:
        return Target(None, "off", "EMS switched off: the thermostat schedule leads")
    # advice values compare as plain strings (StrEnum)
    if evaluation.advice == "unknown" or living_temp is None:
        return Target(None, "unknown", "no heat pump power or living room temperature")

    why, extra = boost(pv_extra, price_now, s)

    def gap(base: float) -> float:
        if why == "low price":
            return max(base, s.gap_max)
        return max(base, min(s.gap_max, base + extra))

    # with a boost the heat may go up to the comfort maximum (stored in the house); a run
    # always has room to climb above the minimum
    top = s.comfort_max if why and not s.night else s.run_ceiling
    top = min(s.comfort_max, max(top, s.living_min + s.start_band))
    off = round(max(OFF_FLOOR, s.living_min - s.off_margin), 1)
    running = bool(evaluation.heat_pump_running)

    def on(base: float, ceiling: float) -> float:
        """Setpoint for a run: just above the room, never lower than the setpoint that is
        already asking for heat (it follows the room up, not down), at most `ceiling`."""
        value = living_temp + gap(base)
        if current_setpoint is not None and current_setpoint > living_temp:
            # never below the setpoint already asking for heat, minus the raise it carried
            value = max(value, current_setpoint - prev_raised + (gap(base) - base))
        return round(min(value, ceiling, s.comfort_max), 1)

    start, hold = on(s.start_offset, top), on(s.hold_offset, top)
    # starts that protect the house (comfort minimum, floor, bedrooms) always ask for heat,
    # also when the room is at the top: a setpoint equal to the room would leave the
    # switch point to tado
    start_safe, hold_safe = on(s.start_offset, s.comfort_max), on(s.hold_offset, s.comfort_max)
    start_pv, hold_pv = on(s.start_offset, s.comfort_max), on(s.hold_offset, s.comfort_max)
    at_top = living_temp >= top
    room_to_climb = living_temp <= top - s.start_band + 1e-9

    def result(setpoint: float, source: str, reason: str) -> Target:
        if source == "idle":
            return Target(setpoint, source, reason)
        raised = round(gap(s.hold_offset) - s.hold_offset, 2) if why else 0.0
        return Target(setpoint, source, reason, why, raised)

    if evaluation.advice == "run" and evaluation.cause == "comfort_min":
        return result(start_safe, "comfort_min", evaluation.reason)
    if evaluation.advice == "run" and evaluation.cause == "floor":
        return result(start_safe, "floor", evaluation.reason)
    if evaluation.advice == "keep_running":
        # also past the top: stopping before the minimum run time is worse
        return result(hold_safe, "keep_running",
                      "hold the run until the minimum run time")
    if bedroom_call:
        # warm water only reaches the bedrooms while the living room asks for heat
        if running:
            return result(hold_safe, "bedroom", "bedroom below target before bedtime: keep the run going")
        return result(start_safe, "bedroom", "bedroom below target before bedtime: short run")
    if not running and living_temp <= s.living_min + s.min_lead + 1e-9:
        return result(start, "minimum", "living room at its minimum: the EMS starts a run")
    if evaluation.advice == "run" and evaluation.cause == "pv":
        return result(start_pv, "pv", evaluation.reason)
    if running and evaluation.pv_surplus_w:
        # still exporting while the heat pump runs: a long run on solar is the cheapest heat
        return result(hold_pv, "pv", "run on solar surplus: keep it going")
    if running and at_top:
        return result(off, "idle", f"room reached the top ({top:.1f}): stop the run")
    if plan_run_now:
        if running:
            return result(hold, "planner", "planned run in progress")
        if room_to_climb:
            return result(start, "planner", "planned run starts")
        return result(off, "idle", f"planned run, but the room is close to the top ({top:.1f}): wait")
    if running and not evaluation.in_peak:
        return result(hold, "to_top", f"run on until the room reaches the top ({top:.1f})")
    if running:
        return result(off, "idle", "price peak: let the run stop")
    return result(off, "idle", evaluation.reason)
