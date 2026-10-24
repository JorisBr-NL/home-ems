"""Pure EMS logic for Home EMS (no Home Assistant imports, so it is easy to test).

Observation mode: this module only computes state and advice. Nothing here writes
to devices.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum


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


@dataclass(frozen=True)
class Inputs:
    """One snapshot of the sensors the EMS reads. None = unavailable."""

    heat_pump_power_w: float | None
    flow_temp: float | None
    living_temp: float | None
    grid_power_w: float | None  # positive = import, negative = export


@dataclass
class RunTracker:
    """Tracks heat pump runs per day from successive snapshots."""

    running: bool = False
    run_start: datetime | None = None
    last_run_end: datetime | None = None
    day: date | None = None
    runs_today: int = 0
    short_runs_today: int = 0
    settings: Settings = field(default_factory=Settings)

    def update(self, running: bool, now: datetime) -> None:
        if self.day != now.date():
            self.day = now.date()
            self.runs_today = 0
            self.short_runs_today = 0
        if running and not self.running:
            self.run_start = now
            self.runs_today += 1
        elif not running and self.running:
            if self.run_start is not None and now - self.run_start < self.settings.min_run:
                self.short_runs_today += 1
            self.last_run_end = now
            self.run_start = None
        self.running = running

    def run_duration(self, now: datetime) -> timedelta | None:
        if not self.running or self.run_start is None:
            return None
        return now - self.run_start


@dataclass(frozen=True)
class Evaluation:
    """Result of one evaluation, exposed as sensors."""

    advice: Advice
    reason: str
    heat_pump_running: bool | None
    floor_margin: float | None
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
        if inputs.flow_temp is not None and inputs.living_temp is not None
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

    tracker.update(running, now)

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
