"""Arbiter: one owner of the living room setpoint (pure logic, no Home Assistant imports).

The advice, the anti short-cycling rule and the day planner all want something from
the same thermostat. The arbiter turns them into one target setpoint per moment, in a
fixed order of priority:

    switched off > comfort minimum > floor > keep running (minimum run time)
    > bedroom below target before bedtime > planner > PV surplus (start, or hold while still exporting) > idle

Runs stay below a ceiling (comfort setpoint + headroom), except on solar surplus.

Runs are started and held with a setpoint just above the room, because tado modulates
on that gap (small gap = low compressor speed = higher COP). With solar surplus or a low
price the gap grows, so the heat pump runs harder and stores the cheap energy as heat in
the house; otherwise it stays small and the heat pump runs gently. During a run the
setpoint follows the room up and down (room + hold offset), so the room may also cool down
slowly while one heat pump runs at its lowest power.
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
    # far above the wanted value (0 = the setpoint follows the room up and down, so the
    # gap stays small and the heat pump runs at low power)
    keep_band: float = 0.0
    # Highest setpoint for a run that is not on solar (comfort setpoint + headroom). The
    # setpoint follows the room up during a run; without a ceiling a long run would
    # push the room up to the comfort maximum. Runs on solar may go higher (storage).
    run_ceiling: float = 20.0
    # Variable gap: from the offsets above up to gap_max, in proportion to the solar
    # surplus (full at full_surplus_w) or fully at a price at or below low_price (all-in
    # EUR/kWh, 0 = off). With a boost the ceiling is the comfort maximum (heat stored).
    gap_max: float = 0.6
    full_surplus_w: float = 1500.0
    low_price: float = 0.20


@dataclass(frozen=True)
class Target:
    setpoint: float | None   # None = the EMS has no target (off or no data)
    source: str              # off, unknown, comfort_min, floor, keep_running, bedroom, planner, pv, idle
    reason: str
    boost: str | None = None  # "solar surplus" or "low price" when the gap was raised
    boost_factor: float = 0.0

    def differs_from(self, current: float | None) -> bool:
        """True when writing the target would change the thermostat (0.1 C steps)."""
        return self.setpoint is not None and (current is None or abs(self.setpoint - current) >= 0.05)


def boost(surplus_w: float | None, price_now: float | None,
          s: ArbiterSettings) -> tuple[float, str | None]:
    """How far to raise the gap (0..1) and why: solar surplus scales, a low price is full."""
    pv = (min(1.0, max(0.0, (surplus_w or 0.0) / s.full_surplus_w))
          if s.full_surplus_w > 0 else 0.0)
    cheap = 1.0 if s.low_price > 0 and price_now is not None and price_now <= s.low_price else 0.0
    if cheap > 0 and cheap >= pv:
        return cheap, "low price"
    if pv > 0:
        return pv, "solar surplus"
    return 0.0, None


def target_setpoint(evaluation: Evaluation, living_temp: float | None, plan_run_now: bool,
                    enabled: bool = True, settings: ArbiterSettings | None = None,
                    current_setpoint: float | None = None, bedroom_call: bool = False,
                    price_now: float | None = None) -> Target:
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

    factor, why = boost(evaluation.pv_surplus_w, price_now, s)

    def gap(base: float) -> float:
        return base + factor * max(0.0, s.gap_max - base)

    # with a boost the heat may go up to the comfort maximum (stored in the house)
    top = s.comfort_max if factor > 0 else s.run_ceiling
    start = keep(clamp(living_temp + gap(s.start_offset), top), top)
    hold = keep(clamp(living_temp + gap(s.hold_offset), top), top)
    # on solar surplus up to the comfort maximum: heat stored in the house
    start_pv = keep(clamp(living_temp + gap(s.start_offset)), s.comfort_max)
    hold_pv = keep(clamp(living_temp + gap(s.hold_offset)), s.comfort_max)
    running = evaluation.heat_pump_running

    def result(setpoint: float, source: str, reason: str) -> Target:
        if source == "idle":
            return Target(setpoint, source, reason)
        return Target(setpoint, source, reason, why, round(factor, 2))

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
