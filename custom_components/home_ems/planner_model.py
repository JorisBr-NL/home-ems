"""Planner with a house model (v0.11.0, pure logic, no Home Assistant imports).

The day planner in `planner.py` spreads a heat budget over the cheapest quarters. It does
not know the room temperature over time, so it plans runs the control will not start (the
room is still near the top) and it cannot start early enough for a limit later on. This
planner predicts the living room temperature per quarter and plans runs the way the control
runs them: a run starts with room to climb, goes on to the top of that moment and lasts at
least the minimum run time. It keeps the room at or above the lower limit of every quarter
(day: comfort setpoint, night: night minimum) and picks, for every run that is needed, the
start and top with the lowest cost per hour the run keeps the room above its limits (price /
COP, solar surplus at the export value). A run may store heat up to a higher top (the
comfort maximum, by day) when that is cheaper per hour, for instance in a cheap window
before a price peak: the house then coasts through the peak.

House model (one room, one capacity):

    C dT/dt = Q_heat - UA (T - T_out) + G

with UA and G taken from the planner's degree-hour model (heat per degree-hour; the base
temperature and the daily intercept give the free heat), C the heat capacity of the house,
and Q_heat the heat pump heat at that outdoor temperature (heat per run-hour), reaching the
room with a lag (water and floor warm up first). The model is rough; the plan is made again
every few minutes from the measured room temperature, so errors do not add up.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .planner import (
    QUARTER,
    Plan,
    PlannedRun,
    PlannerSettings,
    Slot,
    SlotCost,
    heat_rate_at,
    slot_cost,
)

HOURS = QUARTER.total_seconds() / 3600
Limits = Callable[[datetime], tuple[float, float, float]]   # lower limit, top, storage top


@dataclass(frozen=True)
class ModelSettings:
    """House and control settings for the model planner."""

    min_run: timedelta = timedelta(minutes=60)
    start_band: float = 0.3      # a run only starts this far below the top
    lead: float = 0.05           # keep this far above the lower limit in the prediction
    lag_minutes: float = 20.0    # heat reaches the room with this time constant
    reference_temp: float = 19.0  # room temperature the degree-hour model stands for


@dataclass(frozen=True)
class HouseModel:
    capacity_kwh_k: float        # C
    loss_kw_k: float             # UA
    gains_kw: float              # G (sun through windows, people, appliances)

    @classmethod
    def from_planner(cls, ps: PlannerSettings, reference_temp: float) -> HouseModel:
        """UA = heat per degree-hour; G so that the room holds `reference_temp` exactly when
        the degree-hour model says the heat need is zero."""
        ua = ps.heat_per_dh
        gains = ua * (reference_temp - ps.base_temp) - ps.heat_base_kwh_day / 24
        return cls(ps.capacity_kwh_k, ua, max(0.0, gains))

    def step(self, room: float, outside: float, heat_kw: float, hours: float = HOURS) -> float:
        net = heat_kw - self.loss_kw_k * (room - outside) + self.gains_kw
        return room + net * hours / self.capacity_kwh_k


class _Sim:
    """One quarter at a time: room temperature and the share of the heat that arrives."""

    def __init__(self, model: HouseModel, slots: list[Slot], ps: PlannerSettings,
                 s: ModelSettings) -> None:
        self.model, self.slots, self.ps = model, slots, ps
        self.keep = math.exp(-15 / s.lag_minutes)
        self.fallback = next((x.temp for x in slots if x.temp is not None), 8.0)

    def step(self, q: int, temp: float, water: float, heating: bool) -> tuple[float, float]:
        water = water * self.keep + (1.0 - self.keep) * (1.0 if heating else 0.0)
        slot = self.slots[q]
        outside = slot.temp if slot.temp is not None else self.fallback
        return self.model.step(temp, outside, heat_rate_at(slot.temp, self.ps) * water), water


def plan_with_model(slots: list[Slot], now: datetime, room: float | None, limits: Limits,
                    ps: PlannerSettings, s: ModelSettings | None = None,
                    running: bool = False) -> Plan | None:
    """Runs from now to the end of the slots. `limits(moment)` gives the lower limit, the top
    and the storage top (comfort maximum by day, the top at night) for that moment: the same
    limits the control uses. `running`: a run is in progress and goes on to the top first."""
    s = s or ModelSettings()
    slots = sorted((x for x in slots if x.start + QUARTER > now), key=lambda x: x.start)
    if not slots or room is None:
        return None
    sim = _Sim(HouseModel.from_planner(ps, s.reference_temp), slots, ps, s)
    costs: list[SlotCost] = [slot_cost(x, ps) for x in slots]
    lows, tops, stores = zip(*(limits(x.start) for x in slots))
    n = len(slots)
    min_len = max(1, math.ceil(s.min_run / QUARTER))

    def run_from(i: int, temp: float, water: float, store: bool = False
                 ) -> tuple[int, float, float, float]:
        """Heat from quarter i until the top (or the storage top), at least the minimum run
        time. Returns (end index, temp, water, lowest temp during the run)."""
        j, low = i, temp
        while j < n:
            temp, water = sim.step(j, temp, water, True)
            low = min(low, temp)
            j += 1
            top = (stores if store else tops)[min(j, n - 1)]
            if j - i >= min_len and (j >= n or temp >= top):
                break
        return j, temp, water, low

    def run_variants(i: int, temp: float, water: float, store: bool
                     ) -> list[tuple[int, float, float, float, bool]]:
        """Ways to run from quarter i: to the top; with storage also on towards the storage
        top, stopping where the price steps up (the end of a cheap window) or at the storage
        top. Each is (end index, temp, water, lowest temp during the run, stored)."""
        out, j, low, at_top = [], i, temp, False
        limit = stores if store else tops
        while j < n:
            temp, water = sim.step(j, temp, water, True)
            low = min(low, temp)
            j += 1
            if j - i < min_len:
                continue
            nxt = min(j, n - 1)
            if j >= n or temp >= limit[nxt]:
                out.append((j, temp, water, low, store and at_top))
                break
            if temp >= tops[nxt]:
                if not at_top:
                    at_top = True
                    out.append((j, temp, water, low, False))   # the normal end
                    if not store:
                        break
                elif costs[nxt].cost_per_kwh_heat > costs[j - 1].cost_per_kwh_heat * 1.1:
                    out.append((j, temp, water, low, True))    # stop as the price steps up
        return out

    def next_violation(j: int, temp: float, water: float) -> int:
        """First quarter from j where the coasting room drops below its limit (n if none)."""
        for q in range(j, n):
            if temp < lows[q] + s.lead:
                return q
            temp, water = sim.step(q, temp, water, False)
        return n

    on = [False] * n
    runs: list[tuple[int, int]] = []
    stored: set[int] = set()
    i, temp, water = 0, room, (1.0 if running else 0.0)
    if running:
        j, temp, water, _ = run_from(0, temp, water)
        runs.append((0, j))
        i = j
    while i < n:
        # coast from i: where would the room drop below its lower limit?
        coast, t, w, violation = [(temp, water)], temp, water, None
        for q in range(i, n):
            if t < lows[q] + s.lead:
                violation = q
                break
            t, w = sim.step(q, t, w, False)
            coast.append((t, w))
        if violation is None:
            break
        best = None
        for start in range(i, violation + 1):
            t0, w0 = coast[start - i]
            if start < violation and t0 > tops[start] - s.start_band + 1e-9:
                continue   # the control would not start here: no room to climb
            can_store = stores[start] > tops[start] + 0.05
            for end, t1, w1, low, store in run_variants(start, t0, w0, can_store):
                if low < lows[start] - 0.2:
                    continue   # starts too late: the room drops too far before the heat arrives
                cost = sum(costs[q].cost for q in range(start, end))
                # cost per hour the room stays above its limits, from now until a run is
                # needed again: more stored heat covers longer, a cheap run costs less
                covered = (next_violation(end, t1, w1) - i) * HOURS
                key = (round(cost / covered, 5) if covered > 0 else math.inf, -start, store)
                if best is None or key < best[0]:
                    best = (key, start, end, t1, w1, store)
        if best is None:   # nothing fits: start at the violation (the control would too)
            start, store = violation, False
            end, t1, w1, _ = run_from(start, *coast[start - i])
        else:
            _, start, end, t1, w1, store = best
        if store:
            stored.add(start)
        runs.append((start, end))
        i, temp, water = end, t1, w1

    for a, b in runs:
        for q in range(a, b):
            on[q] = True
    temps, t, w = [room], room, (1.0 if running else 0.0)
    for q in range(n):
        t, w = sim.step(q, t, w, on[q])
        temps.append(t)
    planned: list[PlannedRun] = []
    for a, b in runs:
        quarters = costs[a:b]
        el = sum(c.el_kw for c in quarters) * HOURS
        planned.append(PlannedRun(
            start=slots[a].start, end=slots[b - 1].start + QUARTER,
            heat_kwh=sum(c.heat_kw for c in quarters) * HOURS, el_kwh=el,
            cost=sum(c.cost for c in quarters),
            pv_share=sum(c.pv_share * c.el_kw for c in quarters) / max(el / HOURS, 1e-9),
            forecast_price=any(c.slot.forecast for c in quarters),
            reason=("in_progress" if running and a == 0
                    else "store" if a in stored else "model")))
    heat = sum(r.heat_kwh for r in planned)
    avg = sum(c.cost_per_kwh_heat for c in costs) / n
    return Plan(made=now, horizon_end=slots[-1].start + QUARTER, heat_need_kwh=round(heat, 2),
                runs=tuple(planned), expected_cost=round(sum(r.cost for r in planned), 3),
                baseline_cost=round(avg * heat, 3),
                predicted=tuple((slots[q].start, round(temps[q], 2)) for q in range(n)),
                model=True)
