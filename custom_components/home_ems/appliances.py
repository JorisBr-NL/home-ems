"""Appliances (dishwasher, washing machine): best start time before a deadline (pure logic).

The machine is loaded and the plug is off; it resumes its program when power comes
back. The EMS picks the start quarter with the lowest cost before the deadline: the
price, or the market price for the part covered by solar surplus; between equal costs
(a fixed tariff) the window with the most solar, and then the sunniest one. From v0.13 the
EMS also switches the plug of an appliance that waits (see appliance_control).

Solar cover (v0.13.1): a machine does not draw its average power. It heats water first
(about 2 kW for half an hour) and then runs at a low power, so the solar surplus is
counted against that profile. The surplus is the forecast solar minus the base load of
the house and the planned heat pump runs. With the average and the base load alone, a
weak morning sun already "covered" a dishwasher, every window from then on looked equally
good, and the earliest won (25 Nov: 09:00 instead of the sunny midday).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

QUARTER = timedelta(minutes=15)


@dataclass(frozen=True)
class Appliance:
    name: str
    kwh: float = 1.0              # energy per program
    hours: float = 2.0            # program length
    ready_by: time = time(7, 0)   # finished before this time (next occurrence)
    plug: str | None = None       # v0.13: switch entity of the smart plug
    max_wait: float | None = None  # v0.13: hours of waiting at most (None = only the deadline)
    peak_kw: float = 2.0          # v0.13.1: power while heating water at the start
    peak_hours: float = 0.5       # v0.13.1: how long the heating takes

    def profile(self) -> list[float]:
        """Power per quarter (kW) over the program: the heating peak first, then the rest
        of the energy spread over the remaining quarters."""
        n = max(1, math.ceil(self.hours * 4 - 1e-9))
        q = QUARTER.total_seconds() / 3600
        peak_q = min(n, max(0, round(self.peak_hours / q)))
        peak = min(self.peak_kw, self.kwh / (peak_q * q)) if peak_q else 0.0
        rest = max(0.0, self.kwh - peak * peak_q * q)
        low = rest / ((n - peak_q) * q) if n > peak_q else 0.0
        if n == peak_q and peak_q:
            peak = self.kwh / (n * q)
        return [peak] * peak_q + [low] * (n - peak_q)

    @property
    def power_w(self) -> float:
        """Average power while the program runs."""
        return self.kwh / self.hours * 1000 if self.hours else 0.0


@dataclass(frozen=True)
class StartAdvice:
    name: str
    start: datetime | None
    end: datetime | None
    deadline: datetime
    cost: float | None            # EUR at the best start
    cost_now: float | None        # EUR when started now
    pv_share: float | None

    def as_dict(self) -> dict[str, Any]:
        def iso(v):
            return v.isoformat() if v else None
        return {"name": self.name, "start": iso(self.start), "end": iso(self.end),
                "deadline": iso(self.deadline), "cost": self.cost, "cost_now": self.cost_now,
                "saving": round(self.cost_now - self.cost, 3)
                if self.cost is not None and self.cost_now is not None else None,
                "pv_share": self.pv_share}


def parse_appliances(data: Any) -> list[Appliance]:
    """From the YAML option: a list of {naam, kwh, duur (hours), klaar_voor, and from v0.13
    stekker (switch entity of the plug) and max_wachten (hours), from v0.13.1 piek_kw (power
    while heating water at the start, default 2 kW)}."""
    if not data:
        return []
    if not isinstance(data, list):
        raise ValueError("expected a list of appliances")
    out = []
    for item in data:
        if not isinstance(item, dict) or not item.get("naam"):
            raise ValueError("every appliance needs 'naam'")
        ready = item.get("klaar_voor", "07:00")
        if isinstance(ready, int):  # YAML reads 7:00 without quotes as minutes
            ready = f"{ready // 60:02d}:{ready % 60:02d}"
        plug = item.get("stekker")
        if plug is not None and not str(plug).startswith("switch."):
            raise ValueError("'stekker' must be a switch entity")
        out.append(Appliance(str(item["naam"]), float(item.get("kwh", 1.0)),
                             float(item.get("duur", 2.0)), time.fromisoformat(str(ready)),
                             str(plug) if plug else None,
                             float(item["max_wachten"]) if item.get("max_wachten") else None,
                             float(item.get("piek_kw", 2.0))))
    return out


def next_deadline(now: datetime, ready_by: time) -> datetime:
    deadline = now.replace(hour=ready_by.hour, minute=ready_by.minute, second=0, microsecond=0)
    return deadline if deadline > now else deadline + timedelta(days=1)


def best_start(slots: list[Any], now: datetime, appliance: Appliance,
               base_load_kw: float = 0.4, deadline: datetime | None = None,
               extra_load_kw: dict[datetime, float] | None = None) -> StartAdvice:
    """`slots` are planner slots (start, price, market, pv_kw) per quarter. `deadline`:
    finished before this moment (default: the next `ready_by`). `extra_load_kw`: other
    planned use per quarter start (the heat pump runs of the plan)."""
    by_start = {s.start: s for s in slots}
    deadline = deadline or next_deadline(now, appliance.ready_by)
    profile = appliance.profile()
    n = len(profile)
    extra = extra_load_kw or {}
    first = min((s.start for s in slots if s.start + QUARTER > now), default=None)
    best = None
    if first is not None:
        start = first
        while start + n * QUARTER <= deadline:
            c = _cost(by_start, start, profile, base_load_kw, extra)
            # lowest cost; between equal costs (a fixed tariff) the most solar for the
            # machine, and then the sunniest window (the most surplus left over)
            if c is not None:
                key = (round(c[0], 6), -round(c[1], 6), -round(c[2], 6))
                if best is None or key < best[0]:
                    best = (key, start, c[0], c[1])
            start += QUARTER
    now_cost = _cost(by_start, first, profile, base_load_kw, extra) if first is not None else None
    if best is None:
        return StartAdvice(appliance.name, None, None, deadline, None,
                           round(now_cost[0], 3) if now_cost else None, None)
    _, start, cost, pv = best
    return StartAdvice(appliance.name, start, start + n * QUARTER, deadline, round(cost, 3),
                       round(now_cost[0], 3) if now_cost else None,
                       round(pv / appliance.kwh, 2) if appliance.kwh else None)


def _cost(slots: dict[datetime, Any], start: datetime, profile: list[float],
          base_load_kw: float, extra: dict[datetime, float]) -> tuple[float, float, float] | None:
    """(cost EUR, solar kWh used by the machine, solar surplus kWh in the window)."""
    cost = pv = margin = 0.0
    hours = QUARTER.total_seconds() / 3600
    for q, kw in enumerate(profile):
        moment = start + q * QUARTER
        slot = slots.get(moment)
        if slot is None:
            return None
        surplus = max(0.0, slot.pv_kw - base_load_kw - extra.get(moment, 0.0))
        cover = min(kw, surplus)
        market = slot.market if slot.market is not None else 0.0
        cost += (cover * market + (kw - cover) * slot.price) * hours
        pv += cover * hours
        margin += surplus * hours
    return cost, pv, margin
