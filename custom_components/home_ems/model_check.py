"""Checks on the planner with the house model before it steers (v0.12.2, pure logic).

The model planner runs in the background while its switch is off. Before it is switched
on, three things should hold:

1. its prediction of the living room temperature is close to what happens a few hours
   later (the house model is rough; the plan is made again every few minutes);
2. with a fixed tariff it plans no runs at night (every quarter costs the same, so the
   warm afternoon hours with a higher COP are the cheapest; the night is for cooling);
3. it has learned the heat pump at the outdoor temperatures of now (COP and heat per
   2 C bucket, at least a few hours of running).

`PredictionLog` keeps, for every new plan, the temperature it predicted `lead` ahead, and
compares it with the measured room once that moment has come. `readiness` sums the checks
up in one state, so they can be followed on a dashboard and in the daily export.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable

QUARTER = timedelta(minutes=15)

READY = "ready"
NOT_READY = "not_ready"
TOO_LITTLE_DATA = "too_little_data"
READINESS_STATES = [READY, NOT_READY, TOO_LITTLE_DATA]


@dataclass
class PredictionLog:
    """Predictions `lead` ahead, and the error once the moment has come."""

    lead: timedelta = timedelta(hours=3)
    window: timedelta = timedelta(hours=24)
    max_error_k: float = 0.3      # mean absolute error that counts as good enough
    min_hours: float = 12.0       # hours of compared predictions before it counts
    pending: list[tuple[datetime, float]] = field(default_factory=list)  # (moment, temp)
    errors: list[tuple[datetime, float]] = field(default_factory=list)   # (moment, measured - predicted)
    current: tuple[datetime, float] | None = None   # the prediction for about now
    last_error: float | None = None
    last_predicted: float | None = None

    def add_plan(self, made: datetime, predicted: Iterable[tuple[datetime, float]]) -> None:
        """Keep the temperature the plan made at `made` predicts for `made + lead`."""
        target = made + self.lead
        value = next((temp for start, temp in predicted if start <= target < start + QUARTER), None)
        if value is not None:
            self.pending.append((target, value))

    def compare(self, now: datetime, living: float | None) -> float | None:
        """Measured minus predicted for now: the latest prediction whose moment came within
        the last quarter (None without one). Each prediction counts once in the mean over
        `window`, at the moment it falls due."""
        due = [(t, v) for t, v in self.pending if t <= now]
        self.pending = [(t, v) for t, v in self.pending if t > now]
        if due:
            self.current = max(due)
        self.errors = [(t, e) for t, e in self.errors if now - t <= self.window]
        if self.current is None or now - self.current[0] >= QUARTER:
            self.current = None
            self.last_error = self.last_predicted = None
            return None
        self.last_predicted = self.current[1]
        if living is None:
            self.last_error = None
            return None
        self.last_error = round(living - self.last_predicted, 2)
        if due:
            self.errors.append((now, self.last_error))
        return self.last_error

    def mean_abs_error(self) -> float | None:
        if not self.errors:
            return None
        return round(sum(abs(e) for _, e in self.errors) / len(self.errors), 2)

    def hours_compared(self) -> float:
        """Hours covered by the comparisons in the window (one per quarter at most)."""
        quarters = {(t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0))
                    for t, _ in self.errors}
        return len(quarters) / 4


def night_run_hours(runs: Iterable, night: Callable[[datetime], bool]) -> float:
    """Hours of the planned runs that fall in the night window (quarter by quarter)."""
    quarters = 0
    for run in runs:
        moment = run.start
        while moment < run.end:
            if night(moment):
                quarters += 1
            moment += QUARTER
    return quarters / 4


@dataclass(frozen=True)
class Readiness:
    state: str
    reasons: tuple[str, ...]


def readiness(log: PredictionLog, night_hours: float | None, fixed_tariff: bool,
              learned_hours: float | None, min_learned_hours: float) -> Readiness:
    """Sum the three checks up: ready, not ready (with the reasons) or too little data."""
    reasons: list[str] = []
    short: list[str] = []
    mae = log.mean_abs_error()
    if mae is None or log.hours_compared() < log.min_hours:
        short.append(f"fewer than {log.min_hours:.0f} hours of predictions compared")
    elif mae > log.max_error_k:
        reasons.append(f"prediction {log.lead.total_seconds() / 3600:.0f} h ahead is off by "
                       f"{mae:.2f} K on average (at most {log.max_error_k:.1f} K)")
    if night_hours is None:
        short.append("no plan with the house model")
    elif fixed_tariff and night_hours > 0:
        reasons.append(f"{night_hours:.2f} h of runs planned at night with a fixed tariff")
    if learned_hours is None:
        short.append("no outdoor temperature or heat output sensor")
    elif learned_hours < min_learned_hours:
        short.append(f"{learned_hours:.1f} h learned at this outdoor temperature "
                     f"(at least {min_learned_hours:.0f} h)")
    if reasons:
        return Readiness(NOT_READY, tuple(reasons + short))
    if short:
        return Readiness(TOO_LITTLE_DATA, tuple(short))
    return Readiness(READY, ())
