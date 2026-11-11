"""Learning the heat pump from its own measurements (v0.11.0, pure logic).

The planner needs two numbers per outdoor temperature: the COP (heat / electricity) and the
heat a run delivers per hour. Fixed formulas were a first guess; the Quatt in this house
turned out to deliver far more heat than the house needs, and around 0-5 C it defrosts,
which costs heat and electricity. Measured while the heat pump runs, both include defrosts,
start-up and whatever the heat pump decides itself.

Per bucket of outdoor temperature (2 C wide) the heat, electricity and run time are summed
with a slow decay (half-life `half_life`), so the numbers follow changes (a new heating
curve from Quatt support, the season) without forgetting a cold spell at once. A bucket is
only used once it has `min_hours` of running.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

WIDTH = 2.0


def bucket(temp: float) -> int:
    return math.floor(temp / WIDTH)


@dataclass
class Bucket:
    heat_kwh: float = 0.0
    el_kwh: float = 0.0
    hours: float = 0.0


@dataclass
class HeatPumpLearner:
    half_life: timedelta = timedelta(days=14)
    min_hours: float = 3.0
    buckets: dict[int, Bucket] = field(default_factory=dict)
    decayed: datetime | None = None

    def _decay(self, now: datetime) -> None:
        if self.decayed is None:
            self.decayed = now
            return
        days = (now - self.decayed).total_seconds() / 86400
        if days < 1 / 24:   # once an hour is enough
            return
        factor = 0.5 ** (days / (self.half_life.total_seconds() / 86400))
        for b in self.buckets.values():
            b.heat_kwh *= factor
            b.el_kwh *= factor
            b.hours *= factor
        self.decayed = now

    def add(self, outside: float | None, heat_w: float | None, el_w: float | None,
            running: bool | None, minutes: float, now: datetime) -> None:
        """One step of `minutes` while the heat pump runs (other steps are ignored)."""
        self._decay(now)
        if not running or outside is None or heat_w is None or el_w is None or el_w <= 50:
            return
        hours = minutes / 60
        b = self.buckets.setdefault(bucket(outside), Bucket())
        b.heat_kwh += max(0.0, heat_w) / 1000 * hours
        b.el_kwh += el_w / 1000 * hours
        b.hours += hours

    def points(self) -> tuple[tuple[float, float, float], ...]:
        """(outdoor temperature, COP, heat kW while running) for buckets with enough data,
        at the middle of each bucket, coldest first."""
        out = []
        for key in sorted(self.buckets):
            b = self.buckets[key]
            if b.hours >= self.min_hours and b.el_kwh > 0:
                out.append((key * WIDTH + WIDTH / 2, round(b.heat_kwh / b.el_kwh, 2),
                            round(b.heat_kwh / b.hours, 2)))
        return tuple(out)

    def to_dict(self) -> dict:
        return {"decayed": self.decayed.isoformat() if self.decayed else None,
                "buckets": {str(k): [round(b.heat_kwh, 3), round(b.el_kwh, 3), round(b.hours, 3)]
                            for k, b in self.buckets.items()}}

    @classmethod
    def from_dict(cls, data: dict | None) -> HeatPumpLearner:
        data = data or {}
        learner = cls()
        if data.get("decayed"):
            learner.decayed = datetime.fromisoformat(data["decayed"])
        for key, values in (data.get("buckets") or {}).items():
            try:
                learner.buckets[int(key)] = Bucket(*map(float, values))
            except (TypeError, ValueError):
                continue
        return learner

