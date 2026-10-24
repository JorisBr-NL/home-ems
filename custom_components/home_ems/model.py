"""Pure EMS logic for Home EMS (no Home Assistant imports, so it is easy to test).

Observation mode: this module only computes state and advice. Nothing here writes
to devices.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any


class Advice(StrEnum):
    """What the EMS would do right now if it were allowed to act."""

    UNKNOWN = "unknown"
    RUN = "run"            # start a heat pump run
    KEEP_RUNNING = "keep_running"  # run is shorter than the minimum: hold it (anti short-cycling)
    RUNNING = "running"    # run in progress, nothing to do
    WAIT = "wait"          # no run needed now


@dataclass(frozen=True)
class Settings:
    """Tunable thresholds. Defaults follow the analysis of 14-23 Oct 2026."""

    running_threshold_w: float = 100.0
    min_run: timedelta = timedelta(minutes=30)
    # Floor water (flowmeter) minus living room temperature. The current automation
    # starts below 20 C flowmeter at ~19.4 C living room, i.e. a margin of ~0.5 K.
    floor_min_margin: float = 0.5
    floor_full_margin: float = 3.0
    pv_run_threshold_w: float = 700.0
    peak_start: time = time(17, 30)
    peak_end: time = time(20, 0)
    # Gaps between evaluations longer than this (HA restart, outage) are not counted.
    max_step: timedelta = timedelta(minutes=5)
    # After a restart shorter than this, an ongoing run keeps its stored start time.
    resume_window: timedelta = timedelta(minutes=15)


@dataclass(frozen=True)
class Inputs:
    """One snapshot of the sensors the EMS reads. None = unavailable."""

    heat_pump_power_w: float | None
    flow_temp: float | None
    living_temp: float | None
    grid_power_w: float | None  # positive = import, negative = export


@dataclass
class DayStats:
    """Counters per day, used to compare the advice with what really happened."""

    day: date | None = None
    runs: int = 0
    short_runs: int = 0
    unadvised_starts: int = 0          # real starts while the EMS advised to wait
    advised_run_minutes: float = 0.0   # EMS advised a run while the heat pump was off
    running_minutes: float = 0.0
    pv_running_minutes: float = 0.0    # heat pump running while the house exports


@dataclass
class RunTracker:
    """Tracks heat pump runs and comparison counters from successive snapshots."""

    running: bool = False
    run_start: datetime | None = None  # None while running = start not observed
    last_run_end: datetime | None = None
    last_eval: datetime | None = None
    last_advice: Advice | None = None
    initialized: bool = False
    stats: DayStats = field(default_factory=DayStats)
    settings: Settings = field(default_factory=Settings)

    def _roll_day(self, now: datetime) -> None:
        if self.stats.day != now.date():
            self.stats = DayStats(day=now.date())

    def observe(self, running: bool, now: datetime) -> bool:
        """Update the run state. Returns True on a real (observed) start."""
        self._roll_day(now)
        started = False
        if not self.initialized:
            # First snapshot after setup: a run that is already going is not a new start.
            self.initialized = True
            self.run_start = None
        elif running and not self.running:
            self.run_start = now
            self.stats.runs += 1
            started = True
        elif not running and self.running:
            if self.run_start is not None and now - self.run_start < self.settings.min_run:
                self.stats.short_runs += 1
            self.last_run_end = now
            self.run_start = None
        self.running = running
        return started

    def run_duration(self, now: datetime) -> timedelta | None:
        if not self.running or self.run_start is None:
            return None
        return now - self.run_start

    def account(self, advice: Advice, running: bool, grid_power_w: float | None,
                started: bool, now: datetime) -> None:
        """Add time-based counters for the step since the previous evaluation."""
        if started and self.last_advice is Advice.WAIT:
            self.stats.unadvised_starts += 1
        if self.last_eval is not None:
            step = now - self.last_eval
            if timedelta(0) < step <= self.settings.max_step:
                minutes = step.total_seconds() / 60
                if running:
                    self.stats.running_minutes += minutes
                    if grid_power_w is not None and grid_power_w < 0:
                        self.stats.pv_running_minutes += minutes
                elif advice is Advice.RUN:
                    self.stats.advised_run_minutes += minutes
        self.last_eval = now
        self.last_advice = advice

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        stats = asdict(self.stats)
        stats["day"] = self.stats.day.isoformat() if self.stats.day else None
        return {
            "running": self.running,
            "run_start": iso(self.run_start),
            "last_run_end": iso(self.last_run_end),
            "last_eval": iso(self.last_eval),
            "last_advice": self.last_advice.value if self.last_advice else None,
            "stats": stats,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], now: datetime,
                  settings: Settings | None = None) -> RunTracker:
        """Restore after a restart. Counters survive within the same day; an ongoing
        run keeps its start time only if the restart was short."""
        settings = settings or Settings()

        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        tracker = cls(settings=settings)
        raw = dict(data.get("stats") or {})
        day = date.fromisoformat(raw["day"]) if raw.get("day") else None
        if day == now.date():
            raw["day"] = day
            tracker.stats = DayStats(**raw)
        last_eval = parse(data.get("last_eval"))
        tracker.last_run_end = parse(data.get("last_run_end"))
        if last_eval is not None and timedelta(0) <= now - last_eval <= settings.resume_window:
            tracker.initialized = True
            tracker.running = bool(data.get("running"))
            tracker.run_start = parse(data.get("run_start"))
            tracker.last_eval = last_eval
            advice = data.get("last_advice")
            tracker.last_advice = Advice(advice) if advice else None
        return tracker


@dataclass(frozen=True)
class Evaluation:
    """Result of one evaluation, exposed as sensors."""

    advice: Advice
    reason: str
    heat_pump_running: bool | None
    floor_margin: float | None   # only at rest; None while running
    pv_surplus_w: float | None
    in_peak: bool


def is_running(inputs: Inputs, settings: Settings) -> bool | None:
    if inputs.heat_pump_power_w is None:
        return None
    return inputs.heat_pump_power_w > settings.running_threshold_w


def in_peak_window(now: datetime, settings: Settings) -> bool:
    return settings.peak_start <= now.time() < settings.peak_end


def evaluate(inputs: Inputs, tracker: RunTracker, now: datetime,
             settings: Settings | None = None) -> Evaluation:
    """Update the run tracker and decide what the EMS would do now."""
    settings = settings or tracker.settings
    running = is_running(inputs, settings)
    floor_margin = (
        round(inputs.flow_temp - inputs.living_temp, 2)
        if inputs.flow_temp is not None and inputs.living_temp is not None and running is False
        else None
    )
    surplus = (
        max(0.0, -inputs.grid_power_w) if inputs.grid_power_w is not None else None
    )
    peak = in_peak_window(now, settings)

    def result(advice: Advice, reason: str) -> Evaluation:
        return Evaluation(advice, reason, running, floor_margin, surplus, peak)

    if running is None:
        return result(Advice.UNKNOWN, "heat pump power unavailable")

    started = tracker.observe(running, now)
    evaluation = _decide(running, floor_margin, surplus, peak, tracker, now, settings, result)
    tracker.account(evaluation.advice, running, inputs.grid_power_w, started, now)
    return evaluation


def _decide(running, floor_margin, surplus, peak, tracker, now, settings, result) -> Evaluation:
    if running:
        duration = tracker.run_duration(now)
        if duration is not None and duration < settings.min_run:
            return result(Advice.KEEP_RUNNING, "run shorter than minimum run time")
        return result(Advice.RUNNING, "run in progress")

    if peak:
        if floor_margin is not None and floor_margin < settings.floor_min_margin - 1.0:
            return result(Advice.RUN, "floor far below limit during peak")
        return result(Advice.WAIT, "evening peak: avoid starting")

    if floor_margin is not None and floor_margin < settings.floor_min_margin:
        return result(Advice.RUN, "floor below minimum margin")

    if (
        surplus is not None
        and surplus >= settings.pv_run_threshold_w
        and (floor_margin is None or floor_margin < settings.floor_full_margin)
    ):
        return result(Advice.RUN, "PV surplus available")

    return result(Advice.WAIT, "no run needed")
