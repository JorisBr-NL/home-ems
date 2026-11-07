"""Arbiter: one owner of the living room setpoint (pure logic, no Home Assistant imports).

The advice, the anti short-cycling rule and the day planner all want something from
the same thermostat. The arbiter turns them into one target setpoint per moment, in a
fixed order of priority:

    switched off > comfort minimum > floor > keep running (minimum run time)
    > bedroom below target before bedtime > planner > PV surplus (start, or hold while still exporting) > idle

Runs stay below a ceiling (comfort setpoint + headroom), except on solar surplus.

Runs are started and held with a setpoint just above the room, because tado modulates
on that gap (small gap = low compressor speed = higher COP). Under a low price the gap is
the largest gap. On solar the gap follows the grid meter (`PvBoost`): one step up while
power still flows out, one step down while power comes in, so the heat pump settles where
it uses the solar power and no grid power. Otherwise the gap stays small and the heat pump
runs gently. During a run the setpoint follows the room up and down (room + hold offset),
so the room may also cool down slowly while one heat pump runs at its lowest power.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    # far above the wanted value (0 = the setpoint follows the room up and down, so the
    # gap stays small and the heat pump runs at low power)
    keep_band: float = 0.0
    # Highest setpoint for a run that is not on solar (comfort setpoint + headroom). The
    # setpoint follows the room up during a run; without a ceiling a long run would
    # push the room up to the comfort maximum. Runs on solar may go higher (storage).
    run_ceiling: float = 20.0
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
    source: str              # off, unknown, comfort_min, floor, keep_running, bedroom, planner, pv, idle
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
    up one step; above the margin coming in, one step down; within the margin it stays.
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
        if s.boost_margin_w <= 0 or grid_w is None or top == 0:
            return self.reset()
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
            self.extra = round(max(0.0, self.extra - s.boost_step), 2)
        return self.extra


def boost(pv_extra: float, price_now: float | None,
          s: ArbiterSettings) -> tuple[str | None, float]:
    """Why the gap is raised and the extra gap on solar: a low price wins (largest gap)."""
    if s.low_price > 0 and price_now is not None and price_now <= s.low_price:
        return "low price", 0.0
    if pv_extra > 0:
        return "solar surplus", pv_extra
    return None, 0.0


def target_setpoint(evaluation: Evaluation, living_temp: float | None, plan_run_now: bool,
                    enabled: bool = True, settings: ArbiterSettings | None = None,
                    current_setpoint: float | None = None, bedroom_call: bool = False,
                    price_now: float | None = None, pv_extra: float = 0.0) -> Target:
    s = settings or ArbiterSettings()
    if not enabled:
        return Target(None, "off", "EMS switched off: the thermostat schedule leads")
    # advice values compare as plain strings (StrEnum)
    if evaluation.advice == "unknown" or living_temp is None:
        return Target(None, "unknown", "no heat pump power or living room temperature")

    def clamp(value: float, top: float | None = None) -> float:
        top = s.comfort_max if top is None else min(top, s.comfort_max)
        return round(min(top, max(s.comfort_min, value)), 1)

    def keep(value: float, top: float) -> float:
        # the current setpoint still asks for heat with a small gap: leave it as it is
        if (current_setpoint is not None and living_temp < current_setpoint
                and current_setpoint <= min(value + s.keep_band, top, s.comfort_max)):
            return current_setpoint
        return value

    why, extra = boost(pv_extra, price_now, s)

    def gap(base: float) -> float:
        if why == "low price":
            return max(base, s.gap_max)
        return max(base, min(s.gap_max, base + extra))

    # with a boost the heat may go up to the comfort maximum (stored in the house)
    top = s.comfort_max if why else s.run_ceiling
    start = keep(clamp(living_temp + gap(s.start_offset), top), top)
    hold = keep(clamp(living_temp + gap(s.hold_offset), top), top)
    # on solar surplus up to the comfort maximum: heat stored in the house
    start_pv = keep(clamp(living_temp + gap(s.start_offset)), s.comfort_max)
    hold_pv = keep(clamp(living_temp + gap(s.hold_offset)), s.comfort_max)
    running = evaluation.heat_pump_running

    def result(setpoint: float, source: str, reason: str) -> Target:
        if source == "idle":
            return Target(setpoint, source, reason)
        raised = round(gap(s.hold_offset) - s.hold_offset, 2) if why else 0.0
        return Target(setpoint, source, reason, why, raised)

    if evaluation.advice == "run" and evaluation.cause == "comfort_min":
        return result(start, "comfort_min", evaluation.reason)
    if evaluation.advice == "run" and evaluation.cause == "floor":
        return result(start, "floor", evaluation.reason)
    if evaluation.advice == "keep_running":
        return result(hold, "keep_running", "hold the run until the minimum run time")
    if bedroom_call:
        # warm water only reaches the bedrooms while the living room asks for heat
        if running:
            return result(hold, "bedroom", "bedroom below target before bedtime: keep the run going")
        return result(start, "bedroom", "bedroom below target before bedtime: short run")
    if plan_run_now:
        if running:
            return result(hold, "planner", "planned run in progress")
        return result(start, "planner", "planned run starts")
    if evaluation.advice == "run" and evaluation.cause == "pv":
        return result(start_pv, "pv", evaluation.reason)
    if running and evaluation.pv_surplus_w:
        # still exporting while the heat pump runs: a long run on solar is the cheapest heat
        return result(hold_pv, "pv", "run on solar surplus: keep it going")
    idle = clamp(s.idle_setpoint)
    if running:
        return result(idle, "idle", "run not needed any more: let it stop")
    return result(idle, "idle", evaluation.reason)
