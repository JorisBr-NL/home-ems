"""Appliances (dishwasher, washing machine): best start time before a deadline (pure logic).

The machine is loaded and the plug is off; it resumes its program when power comes
back. The EMS picks the start quarter with the lowest cost before the deadline: the
price, or the market price for the part covered by solar surplus. Shadow mode: only
advice, the plug is not switched.
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
    """From the YAML option: a list of {naam, kwh, duur (hours), klaar_voor}."""
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
        out.append(Appliance(str(item["naam"]), float(item.get("kwh", 1.0)),
                             float(item.get("duur", 2.0)), time.fromisoformat(str(ready))))
    return out


def next_deadline(now: datetime, ready_by: time) -> datetime:
    deadline = now.replace(hour=ready_by.hour, minute=ready_by.minute, second=0, microsecond=0)
    return deadline if deadline > now else deadline + timedelta(days=1)


def _cost(slots: dict[datetime, Any], start: datetime, n: int, kw: float,
          base_load_kw: float) -> tuple[float, float] | None:
    cost = pv = 0.0
    hours = QUARTER.total_seconds() / 3600
    for q in range(n):
        slot = slots.get(start + q * QUARTER)
        if slot is None:
            return None
        cover = min(kw, max(0.0, slot.pv_kw - base_load_kw))
        market = slot.market if slot.market is not None else 0.0
        cost += (cover * market + (kw - cover) * slot.price) * hours
        pv += cover * hours
    return cost, pv


def best_start(slots: list[Any], now: datetime, appliance: Appliance,
               base_load_kw: float = 0.4) -> StartAdvice:
    """`slots` are planner slots (start, price, market, pv_kw) per quarter."""
    by_start = {s.start: s for s in slots}
    deadline = next_deadline(now, appliance.ready_by)
    n = max(1, math.ceil(appliance.hours * 4 - 1e-9))
    kw = appliance.kwh / appliance.hours if appliance.hours else 0.0
    first = min((s.start for s in slots if s.start + QUARTER > now), default=None)
    best = None
    if first is not None:
        start = first
        while start + n * QUARTER <= deadline:
            c = _cost(by_start, start, n, kw, base_load_kw)
            if c is not None and (best is None or c[0] < best[1] - 1e-9):
                best = (start, c[0], c[1])
            start += QUARTER
    now_cost = _cost(by_start, first, n, kw, base_load_kw) if first is not None else None
    if best is None:
        return StartAdvice(appliance.name, None, None, deadline, None,
                           round(now_cost[0], 3) if now_cost else None, None)
    start, cost, pv = best
    return StartAdvice(appliance.name, start, start + n * QUARTER, deadline, round(cost, 3),
                       round(now_cost[0], 3) if now_cost else None,
                       round(pv / appliance.kwh, 2) if appliance.kwh else None)
